"""The contract every KielSync payment gateway adapter implements.

An adapter is a translator and nothing more. It converts a
framework-independent request into whatever shape a particular gateway's
HTTP API expects, and converts that gateway's response back into the
normalised dataclasses defined here. Adapters hold no persistence, make
no orchestration decisions, and never accept or return Django model
instances — the boundary is deliberately narrow so that the orchestration
layer can be tested against fakes and so that a second gateway can be
added without touching any caller.

Every amount crossing this boundary is an integer in the currency's
minor unit (kobo for NGN, cents for USD, and the whole unit for
zero-exponent currencies such as XOF). Floats are never used for money,
and adapters perform no conversion arithmetic of their own: the value the
caller supplies is the value that reaches the gateway, and the value the
gateway reports is the value that comes back.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "Gateway",
    "InitializeRequest",
    "InitializeResult",
    "PaymentStatus",
    "RefundResult",
    "VerificationResult",
    "WebhookParseResult",
]


class PaymentStatus(StrEnum):
    """The normalised outcome vocabulary shared by all gateways.

    Each adapter maps its gateway's own status strings onto these three
    values. The vocabulary is deliberately smaller than the persisted
    state machine in :mod:`kielsync.core.states`: an adapter reports what
    the gateway currently believes, and the orchestration layer decides
    what that means for a stored transaction.

    ``PENDING`` is the safe default for any status an adapter recognises
    as "not yet resolved". A status the adapter does not recognise at all
    must never be reported as ``SUCCESS``.
    """

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    PENDING = "PENDING"


@dataclass(frozen=True, slots=True)
class InitializeRequest:
    """A request to start a payment at a gateway.

    ``amount`` is an integer in the currency's minor unit and must be
    positive. ``reference`` is the caller's own idempotency handle for
    this attempt; the gateway echoes it back and it is the key by which
    later verification and reconciliation find the attempt again.
    ``callback_url`` is where the payer's browser is returned after the
    hosted checkout completes, and may be omitted when the gateway's
    dashboard-configured default should apply. ``metadata`` is passed
    through opaquely and must not be relied on for correctness — some
    gateways truncate or drop it.
    """

    amount: int
    currency: str
    email: str
    reference: str
    callback_url: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.amount, bool) or not isinstance(self.amount, int):
            raise TypeError(
                "InitializeRequest.amount must be an int in minor units, "
                f"got {type(self.amount).__name__}."
            )
        if self.amount <= 0:
            raise ValueError("InitializeRequest.amount must be positive.")
        if not self.currency:
            raise ValueError("InitializeRequest.currency is required.")
        if not self.reference:
            raise ValueError("InitializeRequest.reference is required.")


@dataclass(frozen=True, slots=True)
class InitializeResult:
    """What a gateway returns when a payment has been successfully started.

    ``gateway_reference`` is the gateway's own handle for the attempt.
    For gateways that simply echo the caller's reference it equals
    :attr:`InitializeRequest.reference`; callers must not assume this.
    ``authorization_url`` is the hosted checkout page the payer is sent
    to. ``raw`` is the decoded response body, retained verbatim so that
    reconciliation and support can inspect exactly what the gateway said.
    """

    gateway_reference: str
    authorization_url: str
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """The gateway's authoritative answer about one payment.

    This is the only evidence on which a transaction may be marked
    successful. ``amount`` and ``currency`` are what the *gateway*
    reports, not what was requested — an adapter never reconciles the two.
    Comparing them against the originating transaction is the caller's
    responsibility, and a mismatch is a reconciliation event rather than
    an adapter error.

    ``paid_at`` is the gateway's timestamp for settlement and is ``None``
    whenever the payment has not succeeded, or when the gateway declines
    to supply one.
    """

    gateway_reference: str
    status: PaymentStatus
    amount: int
    currency: str
    paid_at: datetime | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RefundResult:
    """The outcome of a refund request.

    ``status`` reflects the state of the *refund*, not of the original
    payment. Most gateways accept a refund asynchronously, so ``PENDING``
    is the common and correct answer to a successful call; the refund's
    eventual resolution arrives by webhook. ``amount`` is the amount the
    gateway recorded as being refunded, in minor units.
    """

    gateway_reference: str
    status: PaymentStatus
    amount: int
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WebhookParseResult:
    """A webhook payload, after its signature has been checked.

    ``signature_valid`` is the field that governs how every other field
    may be used. When it is ``False`` the payload was not proven to come
    from the gateway, and an adapter must return the result built by
    :meth:`rejected` — every other field empty — rather than reporting
    values read from an unauthenticated body. Callers must therefore
    branch on ``signature_valid`` before touching anything else.

    ``event_id`` is the gateway's identifier for this delivery and is the
    deduplication key: gateways retry, and the same event may arrive many
    times. ``event_type`` is the gateway's own event name, retained
    unnormalised because the set of event types is gateway-specific and
    grows over time.
    """

    signature_valid: bool
    event_id: str | None = None
    event_type: str | None = None
    gateway_reference: str | None = None
    status: PaymentStatus | None = None
    amount: int | None = None
    currency: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def rejected(cls) -> WebhookParseResult:
        """Build the result for a webhook that failed signature verification.

        Every field other than ``signature_valid`` is left empty. This is
        the single constructor adapters use on rejection, so that no code
        path can accidentally populate a field from an untrusted body.
        """
        return cls(signature_valid=False)


@runtime_checkable
class Gateway(Protocol):
    """The four operations KielSync requires of a payment gateway.

    This is a :class:`typing.Protocol` rather than an abstract base class:
    adapters are structurally typed, so an implementation need not import
    or inherit from anything in KielSync, and test doubles need only
    provide the four methods. Adapters are expected to be safe to reuse
    across calls and to carry their configuration — credentials, base
    URL, timeouts — as constructor arguments injected by the caller.

    Every method raises
    :exc:`~kielsync.core.errors.RetryableGatewayError` when the failure
    may plausibly succeed on a later attempt against the same or another
    gateway, and :exc:`~kielsync.core.errors.TerminalGatewayError` when it
    will not. Implementations classify failures through
    :func:`kielsync.core.errors.classify`, which defaults to terminal, so
    an unrecognised failure is never retried. No method raises a bare
    transport exception, and no exception message, log record, or
    ``repr`` may contain the adapter's credentials.
    """

    def initialize(self, request: InitializeRequest) -> InitializeResult:
        """Start a payment and obtain a checkout URL for the payer.

        The amount in ``request`` is transmitted unchanged, in minor
        units. A successful return means only that the gateway accepted
        the request and is willing to collect the payment; it is not
        evidence that any money moved. Only :meth:`verify` provides that.

        Raises a terminal error when the gateway rejects the request
        itself — bad credentials, an unsupported currency, a duplicate
        reference — and a retryable error when the request could not be
        delivered or the gateway failed to serve it.
        """
        ...

    def verify(self, gateway_reference: str) -> VerificationResult:
        """Ask the gateway for the authoritative state of one payment.

        This is the only source of truth for success. It is safe to call
        repeatedly and at any time — including for payments that were
        never completed — and callers are expected to do so, both after a
        payer returns from checkout and from reconciliation sweeps that
        do not trust webhook delivery.

        The returned amount and currency are the gateway's own figures
        and may differ from what was requested; returning that difference
        faithfully is part of the contract, and it is the caller that
        decides a mismatch has occurred.

        Raises a terminal error when the reference is unknown to the
        gateway or the credentials are rejected, and a retryable error
        when the gateway could not be reached or did not answer.
        """
        ...

    def refund(
        self, gateway_reference: str, amount: int | None = None
    ) -> RefundResult:
        """Refund a settled payment, in full or in part.

        ``amount`` is in minor units and refunds that portion of the
        payment. When it is ``None`` the entire captured amount is
        refunded; adapters pass this through as "no amount specified"
        rather than computing a figure themselves.

        A successful return normally means the gateway has *accepted* the
        refund, not that it has completed — expect
        :attr:`PaymentStatus.PENDING` and a later webhook. This method is
        not idempotent at every gateway, so callers must guard against
        duplicate submission rather than relying on the adapter.

        Raises a terminal error when the payment cannot be refunded —
        unknown reference, already refunded, amount exceeding the
        captured total — and a retryable error on transport or
        server-side failure.
        """
        ...

    def parse_webhook(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> WebhookParseResult:
        """Authenticate and decode one webhook delivery.

        ``raw_body`` must be the exact bytes received on the wire.
        Signatures are computed over those bytes, so any decoding,
        re-encoding, or JSON round-trip before this call will invalidate
        an otherwise genuine signature. ``headers`` is looked up
        case-insensitively, since HTTP header casing is not preserved
        uniformly across servers and proxies.

        The signature is verified before the body is parsed, and the
        comparison is constant-time. When verification fails the method
        returns :meth:`WebhookParseResult.rejected` and does not parse
        the body at all — an unauthenticated payload is never inspected,
        logged in full, or partially trusted. Verification failure is not
        an exception: a forged or misdirected webhook is an expected
        condition that the caller records and ignores, not an error to
        retry.

        This method performs no I/O and therefore raises neither
        retryable nor terminal gateway errors. A body that passes the
        signature check but cannot be decoded is a malformed delivery
        from an authenticated sender, and is reported as a terminal
        error.
        """
        ...
