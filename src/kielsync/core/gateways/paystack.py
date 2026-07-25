"""Paystack implementation of the :class:`~kielsync.core.gateways.base.Gateway` protocol.

This module is pure Python. It reads no environment variables and no
Django settings: the secret key and every other setting arrive through
the constructor, injected by whichever layer knows how this process is
configured. That is what makes the adapter testable without a framework
and safe to instantiate more than once with different credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from kielsync.core.errors import gateway_error
from kielsync.core.gateways.base import (
    InitializeRequest,
    InitializeResult,
    PaymentStatus,
    RefundResult,
    VerificationResult,
    WebhookParseResult,
)
from kielsync.core.logging import redact

__all__ = ["PaystackGateway"]

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "x-paystack-signature"

# Paystack transaction states, mapped onto the normalised vocabulary.
#
# "abandoned" is deliberately PENDING rather than FAILED. Paystack marks a
# checkout abandoned as soon as the payer leaves the page, but the
# authorization URL stays live until it expires, so the payment can still
# arrive afterwards. Declaring it failed here would let the orchestration
# layer close a transaction that is about to be paid. Deciding that an
# abandoned checkout has been abandoned for good needs the transaction's
# age, which the adapter does not have and the caller does; the caller
# reads the exact Paystack word from `raw` when it wants to make that call.
#
# "reversed" is FAILED because the money has already gone back.
_TRANSACTION_STATUSES = {
    "success": PaymentStatus.SUCCESS,
    "failed": PaymentStatus.FAILED,
    "reversed": PaymentStatus.FAILED,
    "abandoned": PaymentStatus.PENDING,
    "ongoing": PaymentStatus.PENDING,
    "pending": PaymentStatus.PENDING,
    "processing": PaymentStatus.PENDING,
    "queued": PaymentStatus.PENDING,
    "send_otp": PaymentStatus.PENDING,
    "send_pin": PaymentStatus.PENDING,
}

# Refund states. Paystack accepts refunds asynchronously, so "pending" is
# the normal answer to a successful refund call.
_REFUND_STATUSES = {
    "processed": PaymentStatus.SUCCESS,
    "failed": PaymentStatus.FAILED,
    "pending": PaymentStatus.PENDING,
    "processing": PaymentStatus.PENDING,
}


def _map_status(
    raw_status: object, table: Mapping[str, PaymentStatus]
) -> PaymentStatus:
    """Fold a Paystack status word into the normalised vocabulary.

    An unrecognised word maps to ``PENDING``, never to ``SUCCESS`` or
    ``FAILED``. Paystack adds states over time, and the two outcomes an
    adapter must never invent are "the money arrived" and "the money
    never will".
    """
    if not isinstance(raw_status, str):
        return PaymentStatus.PENDING
    return table.get(raw_status.strip().lower(), PaymentStatus.PENDING)


def _parse_timestamp(value: object) -> datetime | None:
    """Parse a Paystack ISO 8601 timestamp, tolerating absence and junk.

    A timestamp is decoration on a verification result: the status and
    the amount carry the meaning. An unparseable one is reported as
    ``None`` rather than failing the whole call.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        logger.warning("Paystack returned an unparseable timestamp")
        return None


def _as_int(value: object) -> int | None:
    """Coerce a Paystack amount to an int without ever inventing one.

    Paystack sends minor-unit amounts as integers but has been known to
    send numeric strings. Both are accepted; anything else, including a
    float, yields ``None`` so that the caller sees a missing amount
    rather than a silently rounded one.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


class PaystackGateway:
    """Adapter for Paystack's REST API.

    The instance owns an :class:`httpx.Client`, so it is worth keeping
    one per credential for the life of the process rather than building
    one per request. It can be used as a context manager, or closed
    explicitly with :meth:`close`.

    Timeouts are set explicitly and are not optional. httpx's default is
    five seconds for everything, which is too short for a card
    authorisation that is waiting on an issuing bank; a request with no
    timeout at all is worse still, since it can pin a worker until the
    process is restarted. Connection setup gets five seconds because a
    connection that has not been established quickly is not going to be,
    and the response gets thirty.

    TLS verification is always on. There is no constructor argument to
    disable it, and none should be added: this client carries a live
    secret key in an ``Authorization`` header on every request.
    """

    name = "PAYSTACK"
    BASE_URL = "https://api.paystack.co"
    CONNECT_TIMEOUT = 5.0
    READ_TIMEOUT = 30.0

    def __init__(
        self,
        secret_key: str,
        *,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Build an adapter bound to one Paystack secret key.

        ``secret_key`` is required and is never read from the
        environment; see :mod:`kielsync.django.settings` for the one
        place in the project that does read it. ``transport`` exists so
        that tests can supply an :class:`httpx.MockTransport`. It cannot
        be used to weaken TLS, which is configured here and nowhere else.
        """
        if not secret_key or not secret_key.strip():
            raise ValueError("PaystackGateway requires a non-empty secret_key.")

        self._secret_key = secret_key
        self.base_url = base_url
        self._client = httpx.Client(
            base_url=base_url,
            verify=True,
            timeout=httpx.Timeout(
                connect=self.CONNECT_TIMEOUT,
                read=self.READ_TIMEOUT,
                write=self.READ_TIMEOUT,
                pool=self.CONNECT_TIMEOUT,
            ),
            headers={
                "Authorization": f"Bearer {secret_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            transport=transport,
        )

    def __repr__(self) -> str:
        """Render the adapter without its credential.

        The default dataclass-style repr would put a live secret key into
        every traceback, debugger frame, and log record that touches the
        instance. This one never does, at any verbosity.
        """
        return f"PaystackGateway(secret_key='[REDACTED]', base_url={self.base_url!r})"

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> PaystackGateway:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- Gateway protocol -------------------------------------------------

    def initialize(self, request: InitializeRequest) -> InitializeResult:
        """Create a Paystack transaction and return its checkout URL.

        The amount is sent exactly as supplied, as an integer in minor
        units. No arithmetic happens here: Paystack's ``amount`` field is
        already in kobo for NGN and cents for USD, which is the same unit
        KielSync speaks internally, so any conversion would be a bug
        waiting to double or hundredth a charge.
        """
        body: dict[str, Any] = {
            "email": request.email,
            "amount": request.amount,
            "currency": request.currency,
            "reference": request.reference,
        }
        if request.callback_url:
            body["callback_url"] = request.callback_url
        if request.metadata:
            body["metadata"] = dict(request.metadata)

        data = self._call("POST", "/transaction/initialize", json_body=body)
        authorization_url = data.get("authorization_url")
        if not isinstance(authorization_url, str) or not authorization_url:
            raise gateway_error(
                "Paystack accepted the initialization but returned no "
                "authorization_url.",
                gateway=self.name,
            )

        reference = data.get("reference")
        return InitializeResult(
            gateway_reference=(
                reference if isinstance(reference, str) and reference
                else request.reference
            ),
            authorization_url=authorization_url,
            raw=data,
        )

    def verify(self, gateway_reference: str) -> VerificationResult:
        """Fetch Paystack's authoritative view of one transaction.

        The amount and currency in the result are Paystack's figures,
        reported unchanged. This method does not compare them against
        whatever was requested and does not raise when they differ —
        detecting a short payment or a currency switch is reconciliation
        work, and it needs the originating transaction that only the
        caller has.

        A transaction Paystack reports as failed is a successful call
        that returns :attr:`PaymentStatus.FAILED`. Only a failure to get
        an answer at all raises.
        """
        # The reference is percent-encoded with no safe characters: it
        # originates from a caller and is interpolated into a path, so
        # a slash or a "?" in it must not be able to redirect the call
        # to a different Paystack endpoint.
        data = self._call(
            "GET", f"/transaction/verify/{quote(gateway_reference, safe='')}"
        )
        reference = data.get("reference")
        currency = data.get("currency")
        return VerificationResult(
            gateway_reference=(
                reference if isinstance(reference, str) and reference
                else gateway_reference
            ),
            status=_map_status(data.get("status"), _TRANSACTION_STATUSES),
            amount=_as_int(data.get("amount")) or 0,
            currency=currency if isinstance(currency, str) else "",
            paid_at=_parse_timestamp(data.get("paid_at") or data.get("paidAt")),
            raw=data,
        )

    def refund(
        self, gateway_reference: str, amount: int | None = None
    ) -> RefundResult:
        """Queue a full or partial refund of a settled Paystack transaction.

        When ``amount`` is ``None`` the ``amount`` field is omitted from
        the request entirely, which is how Paystack is told to refund the
        full captured value. The adapter does not look up the original
        amount to fill it in — doing so would turn one round trip into
        two and would refund a figure that might already be stale.

        Paystack queues refunds, so the usual successful answer is
        :attr:`PaymentStatus.PENDING` with the outcome arriving later by
        webhook.
        """
        body: dict[str, Any] = {"transaction": gateway_reference}
        if amount is not None:
            body["amount"] = amount

        data = self._call("POST", "/refund", json_body=body)
        transaction = data.get("transaction")
        reference = (
            transaction.get("reference") if isinstance(transaction, Mapping) else None
        )
        return RefundResult(
            gateway_reference=(
                reference if isinstance(reference, str) and reference
                else gateway_reference
            ),
            status=_map_status(data.get("status"), _REFUND_STATUSES),
            amount=_as_int(data.get("amount")) or 0,
            raw=data,
        )

    def parse_webhook(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> WebhookParseResult:
        """Verify a Paystack webhook signature, then decode the body.

        Paystack signs the raw request body with HMAC-SHA512 keyed on the
        secret key and sends the hex digest in ``x-paystack-signature``.
        The digest is computed over the bytes as received, so the body
        must not have been decoded and re-encoded on the way here — JSON
        round-tripping reorders keys and rewrites whitespace, and the
        signature will not survive it.

        The comparison uses :func:`hmac.compare_digest`. A plain ``==``
        on digests short-circuits at the first differing byte, and the
        timing difference is enough for an attacker who can submit
        repeated webhooks to recover a valid signature one byte at a
        time, which is equivalent to being able to forge payment
        notifications.

        Nothing is parsed, inspected, or logged from the body until the
        signature verifies. A missing header, a malformed header, or a
        mismatched digest all return
        :meth:`WebhookParseResult.rejected` — an empty result carrying
        only ``signature_valid=False``, so there is no path by which an
        unauthenticated value reaches a caller.
        """
        signature = _lookup_header(headers, SIGNATURE_HEADER)
        if not signature:
            logger.warning("Paystack webhook rejected: signature header missing")
            return WebhookParseResult.rejected()

        try:
            provided = signature.strip().lower().encode("ascii")
        except UnicodeEncodeError:
            logger.warning("Paystack webhook rejected: signature header not ASCII")
            return WebhookParseResult.rejected()

        expected = hmac.new(
            self._secret_key.encode("utf-8"), raw_body, hashlib.sha512
        ).hexdigest().encode("ascii")

        if not hmac.compare_digest(expected, provided):
            logger.warning("Paystack webhook rejected: signature mismatch")
            return WebhookParseResult.rejected()

        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise gateway_error(
                "Paystack webhook passed signature verification but its body "
                "is not valid JSON.",
                gateway=self.name,
                exception=exc,
            ) from exc

        if not isinstance(payload, dict):
            raise gateway_error(
                "Paystack webhook passed signature verification but its body "
                "is not a JSON object.",
                gateway=self.name,
            )

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Paystack webhook accepted: %s", redact(payload))

        event_type = payload.get("event")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            data = {}

        reference = data.get("reference")
        currency = data.get("currency")
        return WebhookParseResult(
            signature_valid=True,
            event_id=_webhook_event_id(event_type, data),
            event_type=event_type if isinstance(event_type, str) else None,
            gateway_reference=reference if isinstance(reference, str) else None,
            status=_map_status(data.get("status"), _TRANSACTION_STATUSES),
            amount=_as_int(data.get("amount")),
            currency=currency if isinstance(currency, str) else None,
            raw=payload,
        )

    # --- HTTP plumbing ----------------------------------------------------

    def _call(
        self, method: str, path: str, *, json_body: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Perform one Paystack call and return the ``data`` object from it.

        Every failure mode below is funnelled through
        :func:`kielsync.core.errors.gateway_error` so that one table
        decides what may be retried. Messages are built from the status
        code and Paystack's own text only, never from the request, so a
        secret key cannot reach a traceback.
        """
        # redact() copies the whole payload, so it is only paid for when
        # something is actually going to read the result.
        if json_body is not None and logger.isEnabledFor(logging.DEBUG):
            logger.debug("Paystack %s %s request: %s", method, path, redact(json_body))

        try:
            response = self._client.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            raise gateway_error(
                f"Paystack {method} {path} failed at the transport layer: "
                f"{type(exc).__name__}.",
                gateway=self.name,
                exception=exc,
            ) from exc

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if not isinstance(payload, dict):
            # A non-JSON body is typical of an edge proxy answering
            # instead of Paystack, so let the status code classify it.
            raise gateway_error(
                f"Paystack {method} {path} returned HTTP "
                f"{response.status_code} with a non-JSON body.",
                gateway=self.name,
                status_code=response.status_code,
            )

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Paystack %s %s response %s: %s",
                method,
                path,
                response.status_code,
                redact(payload),
            )

        message = payload.get("message")
        message = message if isinstance(message, str) else ""
        data = payload.get("data")
        data = data if isinstance(data, dict) else {}

        # Paystack reports application-level refusals as HTTP 200 with
        # `status: false`, so the envelope has to be checked even when the
        # status code is fine. The decline reason in `gateway_response` is
        # more specific than the human message, so it is preferred.
        if response.is_error or payload.get("status") is not True:
            gateway_response = data.get("gateway_response")
            raise gateway_error(
                f"Paystack {method} {path} returned HTTP "
                f"{response.status_code}: {message or 'no message supplied'}.",
                gateway=self.name,
                status_code=response.status_code,
                gateway_code=(
                    gateway_response if isinstance(gateway_response, str) else message
                ),
            )

        return data


def _lookup_header(headers: Mapping[str, str], name: str) -> str | None:
    """Read a header case-insensitively from any mapping.

    HTTP header names are case-insensitive, but a plain ``dict`` is not,
    and webhook bodies arrive through WSGI, ASGI, and test harnesses that
    each normalise casing differently. Looking the name up case-blind
    means a genuine webhook is never rejected over capitalisation.
    """
    direct = headers.get(name)
    if direct is not None:
        return direct
    target = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == target:
            return value
    return None


def _webhook_event_id(event_type: object, data: Mapping[str, Any]) -> str | None:
    """Derive a stable deduplication key for a Paystack webhook.

    Paystack does not send a delivery identifier, in a header or in the
    body, but it does retry, so KielSync has to construct one. The event
    name is combined with the transaction identifier because the same
    transaction legitimately produces several distinct events —
    ``charge.success`` and later ``refund.processed`` — and keying on the
    transaction alone would drop the second as a duplicate of the first.

    Returns ``None`` when the payload carries neither an id nor a
    reference; the caller then has nothing to deduplicate on and must
    decide for itself whether to store the event.
    """
    if not isinstance(event_type, str) or not event_type:
        return None
    identifier = data.get("id") or data.get("reference")
    if identifier is None:
        return None
    return f"{event_type}:{identifier}"
