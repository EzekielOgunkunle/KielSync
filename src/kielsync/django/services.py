"""The single path from a verification result to a stored outcome.

Every way a payment can be resolved — a webhook arriving, the sweeper
picking up a transaction whose webhook never did, an operator retrying by
hand — ends in :func:`resolve_transaction`. There is deliberately only
one such function.

Two code paths that decide the same thing is how reconciliation bugs are
born. They start identical, then one gets a fix the other does not, and
the difference only shows up as a transaction that the webhook path would
have marked successful and the sweeper marks abandoned, six weeks later,
in a report nobody can reproduce. Everything that decides *whether money
arrived* lives here, and callers supply the verification and the row to
apply it to.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from kielsync.core.gateways.base import PaymentStatus, VerificationResult

logger = logging.getLogger(__name__)

__all__ = ["Resolution", "resolve_transaction"]


class Resolution(StrEnum):
    """What :func:`resolve_transaction` did, for callers that report."""

    RESOLVED_SUCCESS = "RESOLVED_SUCCESS"
    RESOLVED_FAILED = "RESOLVED_FAILED"
    MISMATCHED = "MISMATCHED"
    PENDING = "PENDING"
    NO_CHANGE = "NO_CHANGE"


def _matching_attempt(transaction, gateway_reference):
    """Find the attempt a verification is about.

    Falls back to the most recent attempt when the reference does not
    match a stored one, which happens for a transaction whose attempt was
    recorded under a gateway-assigned reference.
    """
    attempts = transaction.attempts.all()
    if gateway_reference:
        for attempt in attempts:
            if attempt.gateway_reference == gateway_reference:
                return attempt
    return attempts.order_by("-created_at").first()


def _advance_attempt(attempt, target) -> None:
    """Move an attempt to ``target``, walking a legal path to get there.

    The attempt state machine requires INITIATED -> REDIRECTED -> SUCCESS,
    but a payment can succeed without anything having recorded the
    redirect: the payer is sent to the checkout page by code that is not
    KielSync's, and the next thing KielSync hears is a webhook. Rather
    than relax the transition table — which exists to catch genuinely
    impossible sequences — this walks through REDIRECTED, which did in
    fact happen if the payer got far enough to pay.

    Does nothing when the attempt is already in the target state, which
    is what makes repeated resolution safe.
    """
    if attempt is None or attempt.status == target:
        return

    Status = attempt.Status
    if target == Status.SUCCESS and attempt.status == Status.INITIATED:
        attempt.transition(Status.REDIRECTED)

    try:
        attempt.transition(target)
    except Exception:
        # A terminal attempt cannot move again. That is not an error
        # here: it means another resolution already recorded an outcome,
        # and this call has nothing left to do.
        logger.info(
            "Attempt %s is already terminal at %s; leaving it alone.",
            attempt.pk,
            attempt.status,
        )


def resolve_transaction(transaction, verification: VerificationResult) -> Resolution:
    """Apply a gateway verification to a stored transaction.

    This is the only function permitted to mark a transaction successful,
    and it is idempotent: calling it twice with the same verification
    produces no second state change and raises nothing. Callers may
    therefore retry freely, which matters because both callers do — the
    webhook handler runs on every redelivery, and the sweeper runs every
    ten minutes over whatever is still open.

    The caller is responsible for locking. Both current callers hold a
    ``select_for_update`` on the transaction row for the duration, so two
    concurrent resolutions of the same payment serialise rather than
    interleave.

    **The amount check.** When the gateway reports success, the amount and
    currency it reports are compared against what the transaction
    expects. On any difference the transaction is *not* marked
    successful: ``reconciliation_status`` becomes ``MISMATCHED``, the
    payment status is left exactly as it was, and the discrepancy is
    logged at error level. A payment that succeeded for the wrong amount
    is not a successful payment, and it is more likely to be tampering or
    a currency-configuration fault than a coincidence. Deciding what to
    do about it needs a human, so this function's job is to stop and say
    so rather than to guess.

    The comparison deliberately gates only the success path. Gateways
    routinely report a zero or absent amount on a *failed* payment, and
    treating that as a mismatch would leave genuinely failed transactions
    stuck open forever while burying the real mismatches in noise. On the
    failure path there is no amount to get wrong: nothing is being
    credited.

    Returns a :class:`Resolution` describing what happened, so that the
    sweeper can count outcomes without re-deriving them.
    """
    Status = transaction.Status

    if verification.status is PaymentStatus.PENDING:
        return Resolution.PENDING

    if verification.status is PaymentStatus.SUCCESS:
        mismatch = _describe_mismatch(transaction, verification)
        if mismatch is not None:
            return _record_mismatch(transaction, verification, mismatch)

        attempt = _matching_attempt(transaction, verification.gateway_reference)
        if transaction.status == Status.SUCCESS:
            # Already resolved. Still make sure the attempt agrees, since
            # a previous run may have been interrupted between the two.
            _advance_attempt(attempt, PaymentStatus.SUCCESS.value)
            return Resolution.NO_CHANGE

        if transaction.status != Status.PENDING:
            # FAILED, ABANDONED, or never moved past CREATED. The gateway
            # says money arrived for a payment KielSync has already
            # written off, or never recorded as started. Forcing the
            # transition would either raise or paper over a real
            # contradiction, and money that arrived after we gave up is
            # exactly what a reconciliation queue is for.
            return _record_mismatch(
                transaction,
                verification,
                f"gateway reports SUCCESS for a transaction in "
                f"{transaction.status!r}, which cannot legally become SUCCESS",
            )

        transaction.mark_success(verified=True)
        transaction.reconciliation_status = (
            transaction.ReconciliationStatus.MATCHED
        )
        transaction.save(update_fields=["reconciliation_status", "updated_at"])
        _advance_attempt(attempt, PaymentStatus.SUCCESS.value)
        logger.info(
            "Transaction %s resolved SUCCESS for %s %s.",
            transaction.pk,
            verification.amount,
            verification.currency,
        )
        return Resolution.RESOLVED_SUCCESS

    # PaymentStatus.FAILED
    attempt = _matching_attempt(transaction, verification.gateway_reference)
    if transaction.status == Status.FAILED:
        _advance_attempt(attempt, PaymentStatus.FAILED.value)
        return Resolution.NO_CHANGE

    if transaction.status != Status.PENDING:
        # SUCCESS or ABANDONED. A gateway reporting failure for a payment
        # already recorded as successful is a genuine contradiction, and
        # silently downgrading it would destroy the record of a payment
        # that may really have settled.
        if transaction.status == Status.SUCCESS:
            logger.error(
                "Gateway reports FAILED for transaction %s, which is already "
                "SUCCESS. Flagging for review rather than overwriting.",
                transaction.pk,
            )
            return _record_mismatch(
                transaction,
                verification,
                "gateway reports FAILED for an already-successful transaction",
            )
        return Resolution.NO_CHANGE

    transaction.transition(Status.FAILED)
    _advance_attempt(attempt, PaymentStatus.FAILED.value)
    logger.info("Transaction %s resolved FAILED.", transaction.pk)
    return Resolution.RESOLVED_FAILED


def _describe_mismatch(transaction, verification: VerificationResult) -> str | None:
    """Return a description of the discrepancy, or ``None`` when they agree."""
    problems = []

    if verification.amount != transaction.amount:
        problems.append(
            f"amount {verification.amount!r} != expected {transaction.amount!r}"
        )

    expected_currency = (transaction.currency or "").strip().upper()
    reported_currency = (verification.currency or "").strip().upper()
    if reported_currency != expected_currency:
        problems.append(
            f"currency {reported_currency!r} != expected {expected_currency!r}"
        )

    return "; ".join(problems) if problems else None


def _record_mismatch(transaction, verification, description: str) -> Resolution:
    """Flag a transaction for human review without touching its status."""
    ReconciliationStatus = transaction.ReconciliationStatus

    logger.error(
        "MISMATCH on transaction %s (%s). Payment status left at %s; "
        "reconciliation_status set to MISMATCHED.",
        transaction.pk,
        description,
        transaction.status,
    )

    if transaction.reconciliation_status == ReconciliationStatus.MISMATCHED:
        return Resolution.MISMATCHED

    transaction.reconciliation_status = ReconciliationStatus.MISMATCHED
    transaction.save(update_fields=["reconciliation_status", "updated_at"])
    return Resolution.MISMATCHED
