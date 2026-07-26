"""Tests for the shared resolution function.

This is the only code permitted to mark a transaction successful, and it
is reached from both the webhook handler and the sweeper. The properties
that matter are that it is idempotent, that an amount mismatch never
becomes a success, and that it cannot be talked into downgrading a
payment that already settled.
"""

import uuid
from datetime import datetime, timezone

import pytest

from kielsync.core.gateways.base import PaymentStatus, VerificationResult
from kielsync.django.models import PaymentAttempt, Transaction
from kielsync.django.services import Resolution, resolve_transaction

pytestmark = pytest.mark.django_db

REFERENCE = "kiel_txn_resolve"


def make_transaction(status=Transaction.Status.PENDING, amount=500_000,
                     currency="NGN", reconciliation=None):
    transaction = Transaction.objects.create(
        idempotency_key=str(uuid.uuid4()),
        amount=amount,
        currency=currency,
        status=Transaction.Status.CREATED,
    )
    if status != Transaction.Status.CREATED:
        Transaction.objects.filter(pk=transaction.pk).update(status=status)
        transaction.refresh_from_db()
    if reconciliation is not None:
        Transaction.objects.filter(pk=transaction.pk).update(
            reconciliation_status=reconciliation
        )
        transaction.refresh_from_db()
    return transaction


def make_attempt(transaction, status=PaymentAttempt.Status.INITIATED,
                 reference=REFERENCE):
    attempt = PaymentAttempt.objects.create(
        transaction=transaction,
        gateway=PaymentAttempt.Gateway.PAYSTACK,
        gateway_reference=reference,
    )
    if status != PaymentAttempt.Status.INITIATED:
        PaymentAttempt.objects.filter(pk=attempt.pk).update(status=status)
        attempt.refresh_from_db()
    return attempt


def verification(status=PaymentStatus.SUCCESS, amount=500_000, currency="NGN",
                 reference=REFERENCE):
    return VerificationResult(
        gateway_reference=reference,
        status=status,
        amount=amount,
        currency=currency,
        paid_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
        raw={},
    )


class TestSuccess:
    def test_matching_success_marks_the_transaction(self):
        transaction = make_transaction()
        make_attempt(transaction)

        assert resolve_transaction(transaction, verification()) is (
            Resolution.RESOLVED_SUCCESS
        )
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.SUCCESS

    def test_success_marks_reconciliation_matched(self):
        transaction = make_transaction()
        make_attempt(transaction)
        resolve_transaction(transaction, verification())
        transaction.refresh_from_db()
        assert transaction.reconciliation_status == (
            Transaction.ReconciliationStatus.MATCHED
        )

    def test_the_winning_attempt_is_marked_success(self):
        transaction = make_transaction()
        attempt = make_attempt(transaction)
        resolve_transaction(transaction, verification())
        attempt.refresh_from_db()
        assert attempt.status == PaymentAttempt.Status.SUCCESS

    def test_an_attempt_still_in_initiated_can_reach_success(self):
        """Nothing necessarily recorded the redirect: the payer is sent to
        the checkout by code that is not KielSync's, and the next thing
        heard is a webhook. That must not strand the attempt."""
        transaction = make_transaction()
        attempt = make_attempt(transaction, status=PaymentAttempt.Status.INITIATED)
        resolve_transaction(transaction, verification())
        attempt.refresh_from_db()
        assert attempt.status == PaymentAttempt.Status.SUCCESS

    def test_the_right_attempt_is_selected_by_reference(self):
        transaction = make_transaction()
        loser = make_attempt(transaction, reference="kiel_other_attempt")
        winner = make_attempt(transaction, reference=REFERENCE)
        resolve_transaction(transaction, verification(reference=REFERENCE))
        loser.refresh_from_db()
        winner.refresh_from_db()
        assert winner.status == PaymentAttempt.Status.SUCCESS
        assert loser.status == PaymentAttempt.Status.INITIATED

    def test_resolution_works_without_any_attempt(self):
        """The sweeper can meet a transaction whose attempt row was never
        written. That should still resolve rather than crash."""
        transaction = make_transaction()
        assert resolve_transaction(transaction, verification()) is (
            Resolution.RESOLVED_SUCCESS
        )


class TestMismatch:
    def test_amount_mismatch_does_not_mark_success(self):
        transaction = make_transaction(amount=500_000)
        make_attempt(transaction)

        assert resolve_transaction(
            transaction, verification(amount=499_900)
        ) is Resolution.MISMATCHED

        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.PENDING
        assert transaction.reconciliation_status == (
            Transaction.ReconciliationStatus.MISMATCHED
        )

    def test_underpayment_and_overpayment_both_mismatch(self):
        for reported in (499_900, 500_100):
            transaction = make_transaction(amount=500_000)
            assert resolve_transaction(
                transaction, verification(amount=reported)
            ) is Resolution.MISMATCHED

    def test_currency_mismatch_does_not_mark_success(self):
        transaction = make_transaction(currency="NGN")
        assert resolve_transaction(
            transaction, verification(currency="USD")
        ) is Resolution.MISMATCHED
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.PENDING

    def test_currency_comparison_ignores_case_and_padding(self):
        transaction = make_transaction(currency="NGN")
        assert resolve_transaction(
            transaction, verification(currency=" ngn ")
        ) is Resolution.RESOLVED_SUCCESS

    def test_missing_amount_is_a_mismatch_not_a_pass(self):
        """A gateway amount that could not be converted arrives as 0. That
        must fail the comparison rather than slip through it."""
        transaction = make_transaction(amount=500_000)
        assert resolve_transaction(
            transaction, verification(amount=0)
        ) is Resolution.MISMATCHED

    def test_the_attempt_is_not_advanced_on_mismatch(self):
        transaction = make_transaction()
        attempt = make_attempt(transaction)
        resolve_transaction(transaction, verification(amount=1))
        attempt.refresh_from_db()
        assert attempt.status == PaymentAttempt.Status.INITIATED

    def test_mismatch_is_logged_at_error_level(self, caplog):
        transaction = make_transaction(amount=500_000)
        with caplog.at_level("ERROR", logger="kielsync.django.services"):
            resolve_transaction(transaction, verification(amount=1))
        assert "MISMATCH" in caplog.text

    def test_mismatch_is_idempotent(self):
        transaction = make_transaction(amount=500_000)
        first = resolve_transaction(transaction, verification(amount=1))
        second = resolve_transaction(transaction, verification(amount=1))
        assert first is second is Resolution.MISMATCHED
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.PENDING


class TestFailure:
    def test_failed_verification_transitions_the_transaction(self):
        transaction = make_transaction()
        make_attempt(transaction)
        assert resolve_transaction(
            transaction, verification(status=PaymentStatus.FAILED)
        ) is Resolution.RESOLVED_FAILED
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.FAILED

    def test_the_attempt_is_marked_failed(self):
        transaction = make_transaction()
        attempt = make_attempt(transaction)
        resolve_transaction(transaction, verification(status=PaymentStatus.FAILED))
        attempt.refresh_from_db()
        assert attempt.status == PaymentAttempt.Status.FAILED

    def test_a_zero_amount_on_failure_is_not_treated_as_a_mismatch(self):
        """Gateways routinely report no amount on a failed payment.
        Flagging that would leave failed transactions open forever and
        bury the real mismatches in noise."""
        transaction = make_transaction(amount=500_000)
        assert resolve_transaction(
            transaction, verification(status=PaymentStatus.FAILED, amount=0)
        ) is Resolution.RESOLVED_FAILED
        transaction.refresh_from_db()
        assert transaction.reconciliation_status == (
            Transaction.ReconciliationStatus.UNRECONCILED
        )

    def test_failure_reported_for_an_already_successful_payment_is_a_mismatch(self):
        """Silently downgrading would destroy the record of a payment that
        may really have settled."""
        transaction = make_transaction(status=Transaction.Status.SUCCESS)
        assert resolve_transaction(
            transaction, verification(status=PaymentStatus.FAILED)
        ) is Resolution.MISMATCHED
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.SUCCESS


class TestPending:
    def test_pending_changes_nothing(self):
        transaction = make_transaction()
        attempt = make_attempt(transaction)
        assert resolve_transaction(
            transaction, verification(status=PaymentStatus.PENDING)
        ) is Resolution.PENDING
        transaction.refresh_from_db()
        attempt.refresh_from_db()
        assert transaction.status == Transaction.Status.PENDING
        assert attempt.status == PaymentAttempt.Status.INITIATED
        assert transaction.reconciliation_status == (
            Transaction.ReconciliationStatus.UNRECONCILED
        )

    def test_pending_does_not_mismatch_even_on_a_wrong_amount(self):
        """Nothing has been decided yet, so there is nothing to reconcile."""
        transaction = make_transaction(amount=500_000)
        assert resolve_transaction(
            transaction, verification(status=PaymentStatus.PENDING, amount=1)
        ) is Resolution.PENDING


class TestIdempotence:
    """The property both callers depend on: the webhook handler runs on
    every redelivery and the sweeper runs every ten minutes."""

    def test_repeated_success_produces_no_second_change(self):
        transaction = make_transaction()
        attempt = make_attempt(transaction)

        first = resolve_transaction(transaction, verification())
        transaction.refresh_from_db()
        updated_at = transaction.updated_at

        second = resolve_transaction(transaction, verification())
        third = resolve_transaction(transaction, verification())

        assert first is Resolution.RESOLVED_SUCCESS
        assert second is third is Resolution.NO_CHANGE
        transaction.refresh_from_db()
        attempt.refresh_from_db()
        assert transaction.status == Transaction.Status.SUCCESS
        assert transaction.updated_at == updated_at
        assert attempt.status == PaymentAttempt.Status.SUCCESS

    def test_repeated_failure_produces_no_second_change(self):
        transaction = make_transaction()
        make_attempt(transaction)
        assert resolve_transaction(
            transaction, verification(status=PaymentStatus.FAILED)
        ) is Resolution.RESOLVED_FAILED
        assert resolve_transaction(
            transaction, verification(status=PaymentStatus.FAILED)
        ) is Resolution.NO_CHANGE

    def test_repeated_calls_never_raise(self):
        transaction = make_transaction()
        make_attempt(transaction)
        for _ in range(5):
            resolve_transaction(transaction, verification())

    def test_success_after_failure_flags_rather_than_resurrecting(self):
        """A transaction already recorded FAILED is terminal. A gateway
        later reporting success means money arrived after KielSync gave
        up — a contradiction for a human, not an exception and not a
        silent overwrite."""
        transaction = make_transaction()
        make_attempt(transaction)
        resolve_transaction(transaction, verification(status=PaymentStatus.FAILED))
        transaction.refresh_from_db()

        assert resolve_transaction(
            transaction, verification(status=PaymentStatus.SUCCESS)
        ) is Resolution.MISMATCHED
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.FAILED
        assert transaction.reconciliation_status == (
            Transaction.ReconciliationStatus.MISMATCHED
        )

    def test_success_on_an_abandoned_transaction_flags_rather_than_raising(self):
        """The sweeper abandons at 24 hours; a straggler settling at 25 is
        exactly the case a reconciliation queue exists for."""
        transaction = make_transaction(status=Transaction.Status.ABANDONED)
        make_attempt(transaction)
        assert resolve_transaction(transaction, verification()) is (
            Resolution.MISMATCHED
        )
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.ABANDONED

    def test_success_on_a_transaction_still_in_created_flags(self):
        transaction = make_transaction(status=Transaction.Status.CREATED)
        assert resolve_transaction(transaction, verification()) is (
            Resolution.MISMATCHED
        )

    @pytest.mark.parametrize(
        "transaction_status",
        [
            Transaction.Status.CREATED,
            Transaction.Status.PENDING,
            Transaction.Status.SUCCESS,
            Transaction.Status.FAILED,
            Transaction.Status.ABANDONED,
        ],
    )
    @pytest.mark.parametrize(
        "reported",
        [PaymentStatus.SUCCESS, PaymentStatus.FAILED, PaymentStatus.PENDING],
    )
    def test_resolution_never_raises_from_any_starting_state(
        self, transaction_status, reported
    ):
        """Both callers treat resolution as safe to retry. If it can raise
        for some combination of stored state and reported state, the
        webhook handler returns 500 and the gateway redelivers forever."""
        transaction = make_transaction(status=transaction_status)
        make_attempt(transaction)
        resolve_transaction(transaction, verification(status=reported))
        resolve_transaction(transaction, verification(status=reported))

    def test_resolution_on_an_abandoned_transaction_does_not_raise(self):
        transaction = make_transaction(status=Transaction.Status.ABANDONED)
        make_attempt(transaction)
        resolve_transaction(transaction, verification(status=PaymentStatus.FAILED))
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.ABANDONED
