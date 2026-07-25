"""Gateway failure classification.

Every failure KielSync sees from a payment gateway is sorted into exactly
one of two classes, and that single decision governs everything the
orchestration layer is later allowed to do:

``RETRYABLE``
    Nothing was decided. The request may not have reached the gateway, or
    the gateway could not answer. Re-sending it — to the same gateway or
    a different one — is safe and may succeed.

``TERMINAL``
    Something was decided, and the answer was no. The card was declined,
    the account is invalid, the credentials are wrong. Re-sending will
    produce the same answer, and failing over to another gateway will
    only annoy the payer and, in the decline cases, count against the
    card's fraud limits.

The asymmetry matters. Misclassifying a terminal failure as retryable
produces duplicate charge attempts against a payer who has already been
told no; misclassifying a retryable failure as terminal costs one
recoverable payment. The first is much worse than the second, so
:func:`classify` resolves every uncertainty toward ``TERMINAL``. A signal
this module does not recognise is never retried.
"""

from __future__ import annotations

from enum import StrEnum

from kielsync.core.exceptions import KielSyncError

__all__ = [
    "ErrorClass",
    "GatewayError",
    "RetryableGatewayError",
    "TerminalGatewayError",
    "classify",
    "gateway_error",
]


class ErrorClass(StrEnum):
    """The two-valued verdict returned by :func:`classify`."""

    RETRYABLE = "RETRYABLE"
    TERMINAL = "TERMINAL"


class GatewayError(KielSyncError):
    """Base class for any failure originating at a payment gateway.

    Instances carry the signals the failure was classified from so that
    the orchestration layer can persist them alongside the attempt
    without re-deriving anything. They deliberately do not carry the
    request that produced them: credentials must never reach an exception
    message, a traceback, or a log record.

    This class is not raised directly. :func:`gateway_error` constructs
    the correct concrete subclass from the available signals.
    """

    error_class: ErrorClass

    def __init__(
        self,
        message: str,
        *,
        gateway: str | None = None,
        status_code: int | None = None,
        gateway_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.gateway = gateway
        self.status_code = status_code
        self.gateway_code = gateway_code

    @property
    def retryable(self) -> bool:
        """Whether this failure may be re-attempted."""
        return self.error_class is ErrorClass.RETRYABLE


class RetryableGatewayError(GatewayError):
    """The gateway did not decide the payment; the request may be re-sent."""

    error_class = ErrorClass.RETRYABLE


class TerminalGatewayError(GatewayError):
    """The gateway decided against the payment; re-sending will not help."""

    error_class = ErrorClass.TERMINAL


# Transport failures that prove the request was not processed, and so may
# safely be re-sent. httpx.TimeoutException covers connect, read, write,
# and pool timeouts; httpx.NetworkError covers connection refused, reset,
# and similar failures to exchange bytes at all. The stdlib entries let
# adapters that do not use httpx be classified by the same function.
#
# Deliberately absent: httpx.RemoteProtocolError and httpx.ProxyError. A
# server that disconnects mid-response may well have processed the
# request, so re-sending it is not provably safe and it stays terminal.
_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
)

try:  # pragma: no cover - exercised implicitly wherever httpx is installed
    import httpx
except ImportError:  # pragma: no cover - httpx is a hard dependency in practice
    pass
else:
    _RETRYABLE_EXCEPTIONS += (httpx.TimeoutException, httpx.NetworkError)


# Gateway-declared conditions meaning "the payment processor, the issuing
# bank, or the card network was unreachable or unwell". These arrive with
# an HTTP 200 as often as not, which is why gateway codes are consulted
# before the HTTP status.
RETRYABLE_GATEWAY_CODES = frozenset(
    {
        "bank_unavailable",
        "gateway_timeout",
        "issuer_or_switch_inoperative",
        "issuer_unavailable",
        "network_error",
        "processor_unavailable",
        "rate_limited",
        "service_unavailable",
        "system_malfunction",
        "temporarily_unavailable",
        "timeout_waiting_for_response",
        "too_many_requests",
        "try_again",
    }
)

# Gateway-declared conditions meaning the request was understood and
# refused. These fall into three groups: the instrument was declined, the
# instrument or account details are wrong, or KielSync's own credentials
# and references are wrong. All three are settled answers.
TERMINAL_GATEWAY_CODES = frozenset(
    {
        # Declines
        "declined",
        "card_declined",
        "do_not_honor",
        "do_not_honour",
        "insufficient_funds",
        "risk_declined",
        "transaction_not_permitted_to_cardholder",
        # Bad instrument
        "expired_card",
        "exceeded_withdrawal_limit",
        "incorrect_pin",
        "invalid_card",
        "invalid_card_number",
        "invalid_cvv",
        "invalid_expiry",
        "invalid_pin",
        "lost_card",
        "restricted_card",
        "stolen_card",
        # Bad account or destination
        "invalid_account",
        "invalid_account_number",
        "invalid_bank_code",
        # Bad request from us
        "authentication_failed",
        "cancelled",
        "abandoned",
        "currency_not_supported",
        "duplicate_transaction_reference",
        "invalid_key",
        "invalid_signature",
        "transaction_not_found",
        "unauthorized",
    }
)

_HTTP_TOO_MANY_REQUESTS = 429


def normalise_gateway_code(gateway_code: str | None) -> str | None:
    """Fold a gateway's code or message into the lookup form used here.

    Gateways are inconsistent about casing and separators for what is
    otherwise the same condition: Paystack reports ``"Insufficient
    Funds"`` in one field and ``insufficient_funds`` in another. Folding
    to lowercase with underscore separators lets a single table cover
    both without the adapter having to guess which spelling it got.
    """
    if gateway_code is None:
        return None
    folded = gateway_code.strip().lower()
    for separator in (" ", "-", "."):
        folded = folded.replace(separator, "_")
    while "__" in folded:
        folded = folded.replace("__", "_")
    return folded.strip("_") or None


def classify(
    status_code: int | None = None,
    gateway_code: str | None = None,
    exception: Exception | None = None,
) -> ErrorClass:
    """Sort a gateway failure into ``RETRYABLE`` or ``TERMINAL``.

    The function is pure: it performs no I/O, reads no configuration, and
    depends on nothing but its three arguments. Any combination of them
    may be ``None``, including all three, which is the fully uninformed
    case and classifies as ``TERMINAL``.

    Signals are consulted in decreasing order of specificity:

    1. An exception that is already a :class:`GatewayError` has been
       classified once and is not reclassified.
    2. A transport exception proving the request never completed —
       a connect or read timeout, a refused or reset connection —
       is ``RETRYABLE``.
    3. A recognised gateway code decides on its own. Gateway codes
       outrank the HTTP status because gateways routinely report a
       decline, or an unreachable issuer, inside an HTTP 200 envelope.
    4. Failing that, the HTTP status decides: 429 and 5xx are
       ``RETRYABLE``, every other status is ``TERMINAL``.
    5. With nothing recognised, the verdict is ``TERMINAL``.

    Note that a recognised terminal gateway code beats a 5xx status. That
    combination should not occur, and when contradictory signals do
    arrive the safe reading is that the payment was decided.
    """
    if isinstance(exception, GatewayError):
        return exception.error_class

    if exception is not None and isinstance(exception, _RETRYABLE_EXCEPTIONS):
        return ErrorClass.RETRYABLE

    code = normalise_gateway_code(gateway_code)
    if code is not None:
        if code in TERMINAL_GATEWAY_CODES:
            return ErrorClass.TERMINAL
        if code in RETRYABLE_GATEWAY_CODES:
            return ErrorClass.RETRYABLE

    if status_code is not None:
        if status_code == _HTTP_TOO_MANY_REQUESTS or 500 <= status_code <= 599:
            return ErrorClass.RETRYABLE
        return ErrorClass.TERMINAL

    return ErrorClass.TERMINAL


def gateway_error(
    message: str,
    *,
    gateway: str | None = None,
    status_code: int | None = None,
    gateway_code: str | None = None,
    exception: Exception | None = None,
) -> GatewayError:
    """Build the correctly classified exception for a gateway failure.

    Adapters raise the result of this function rather than choosing a
    subclass themselves, so that every adapter classifies through the
    same table and a new gateway cannot quietly invent its own retry
    policy.

    ``message`` is included verbatim in the exception and so must be
    assembled from the gateway's own response and status only. It must
    never contain credentials, cardholder data, or the request body.
    """
    error_class = classify(
        status_code=status_code, gateway_code=gateway_code, exception=exception
    )
    cls = (
        RetryableGatewayError
        if error_class is ErrorClass.RETRYABLE
        else TerminalGatewayError
    )
    return cls(
        message,
        gateway=gateway,
        status_code=status_code,
        gateway_code=gateway_code,
    )
