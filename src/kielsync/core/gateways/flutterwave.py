"""Flutterwave implementation of the :class:`~kielsync.core.gateways.base.Gateway` protocol.

Like the Paystack adapter, this module is pure Python and reads no
environment variables or Django settings; the secret key arrives through
the constructor.

Flutterwave differs from Paystack in four ways that are each a standing
source of bugs. They are handled explicitly here and documented on the
methods where they bite:

1. **Amounts are major units.** Flutterwave's v3 API takes ``5000`` to
   mean ₦5,000, where Paystack takes ``500000``. KielSync stores minor
   units everywhere, so this adapter converts on the way out and back on
   the way in, through :mod:`kielsync.core.currency`. See
   :meth:`FlutterwaveGateway.initialize`.

2. **Two identifiers.** ``tx_ref`` is ours; the numeric ``id`` is
   Flutterwave's, and we only learn it from a webhook or a redirect. See
   :meth:`FlutterwaveGateway.verify`.

3. **Webhook authentication is weaker.** The ``verif-hash`` header is a
   static shared secret, not a signature over the body. See
   :meth:`FlutterwaveGateway.parse_webhook`.

4. **No delivery identifier.** Deduplication ids are derived. See
   :func:`kielsync.core.webhooks.derive_event_id`.
"""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from kielsync.core.currency import from_major_units, to_major_units
from kielsync.core.errors import gateway_error
from kielsync.core.exceptions import UnknownCurrency
from kielsync.core.gateways.base import (
    InitializeRequest,
    InitializeResult,
    PaymentStatus,
    RefundResult,
    VerificationResult,
    WebhookParseResult,
)
from kielsync.core.logging import redact
from kielsync.core.webhooks import derive_event_id

__all__ = ["FlutterwaveGateway"]

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "verif-hash"

# Flutterwave transaction states, mapped onto the normalised vocabulary
# through an explicit table rather than string matching. "successful"
# and "success" both appear in the wild depending on the endpoint.
_TRANSACTION_STATUSES = {
    "successful": PaymentStatus.SUCCESS,
    "success": PaymentStatus.SUCCESS,
    "completed": PaymentStatus.SUCCESS,
    "failed": PaymentStatus.FAILED,
    "cancelled": PaymentStatus.FAILED,
    "canceled": PaymentStatus.FAILED,
    "pending": PaymentStatus.PENDING,
    "processing": PaymentStatus.PENDING,
    "new": PaymentStatus.PENDING,
}

# Refund states. Flutterwave reuses the `status` field with a different
# vocabulary for refunds: a finished refund says "completed", where a
# finished charge says "successful".
#
# Reading a refund through the transaction table is exactly the Night 2
# Paystack bug, which reported a completed refund as still pending. The
# tables are kept separate here and selected by event type, and
# test_refund_events_do_not_use_transaction_semantics pins the behaviour
# so the mistake cannot be reintroduced silently.
_REFUND_STATUSES = {
    "completed": PaymentStatus.SUCCESS,
    "successful": PaymentStatus.SUCCESS,
    "failed": PaymentStatus.FAILED,
    "pending": PaymentStatus.PENDING,
    "processing": PaymentStatus.PENDING,
}


def _status_table_for(event_type: object) -> Mapping[str, PaymentStatus]:
    """Select the status vocabulary that applies to a webhook event."""
    if isinstance(event_type, str) and "refund" in event_type.strip().lower():
        return _REFUND_STATUSES
    return _TRANSACTION_STATUSES


def _map_status(
    raw_status: object, table: Mapping[str, PaymentStatus]
) -> PaymentStatus:
    """Fold a Flutterwave status word into the normalised vocabulary.

    An unrecognised word maps to ``PENDING``, never ``SUCCESS`` or
    ``FAILED``. The two outcomes an adapter must never invent are "the
    money arrived" and "the money never will".
    """
    if not isinstance(raw_status, str):
        return PaymentStatus.PENDING
    return table.get(raw_status.strip().lower(), PaymentStatus.PENDING)


def _parse_timestamp(value: object) -> datetime | None:
    """Parse a Flutterwave timestamp, tolerating absence and junk."""
    if not isinstance(value, str) or not value:
        return None
    candidate = value.strip().replace(" ", "T") if " " in value else value.strip()
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        logger.warning("Flutterwave returned an unparseable timestamp")
        return None


class FlutterwaveGateway:
    """Adapter for Flutterwave's v3 REST API.

    Configuration, timeouts, TLS, redaction, and error classification all
    follow the same rules as
    :class:`~kielsync.core.gateways.paystack.PaystackGateway`: the secret
    key is injected and never read from the environment, connect and read
    timeouts are explicit, TLS verification has no off switch, payloads
    are redacted before logging, and every failure is classified through
    :func:`kielsync.core.errors.classify`.
    """

    name = "FLUTTERWAVE"
    BASE_URL = "https://api.flutterwave.com/v3"
    CONNECT_TIMEOUT = 5.0
    READ_TIMEOUT = 30.0

    def __init__(
        self,
        secret_key: str,
        *,
        webhook_secret_hash: str | None = None,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Build an adapter bound to one Flutterwave secret key.

        ``webhook_secret_hash`` is the separate shared secret configured
        in Flutterwave's dashboard and sent back in the ``verif-hash``
        header. It is a *different* value from the API secret key, and
        conflating the two is a common misconfiguration that makes every
        webhook fail authentication. When omitted, webhook verification
        always rejects — refusing everything is the safe failure mode for
        a missing credential, and it is loud enough to be noticed.
        """
        if not secret_key or not secret_key.strip():
            raise ValueError("FlutterwaveGateway requires a non-empty secret_key.")

        self._secret_key = secret_key
        self._webhook_secret_hash = webhook_secret_hash
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
        """Render the adapter without either of its credentials."""
        return (
            "FlutterwaveGateway(secret_key='[REDACTED]', "
            f"webhook_secret_hash='[REDACTED]', base_url={self.base_url!r})"
        )

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> FlutterwaveGateway:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- Gateway protocol -------------------------------------------------

    def initialize(self, request: InitializeRequest) -> InitializeResult:
        """Create a Flutterwave payment and return its checkout URL.

        **Units.** ``request.amount`` is in minor units, as everywhere
        else in KielSync, and Flutterwave expects major units. The
        conversion goes through
        :func:`kielsync.core.currency.to_major_units`, which consults the
        currency's exponent rather than dividing by a hundred — ₦5,000 is
        500000 minor units and is sent as ``5000``, while 5,000 XOF is
        5000 minor units and is sent as ``5000`` unchanged, because XOF
        has no subunit. Getting this wrong in either direction is a
        hundredfold error in a live charge.

        The converted amount is serialised as a decimal string rather
        than a JSON float. A float cannot represent every two-decimal
        value exactly, and the failure mode is an amount that differs
        from the stored one by a fraction of a kobo, which then fails the
        reconciliation comparison for reasons nobody can reproduce.

        ``tx_ref`` carries our reference. Flutterwave will also mint its
        own numeric id for the transaction, but not until the payer
        interacts with the checkout, so it is not available here.
        """
        body: dict[str, Any] = {
            "tx_ref": request.reference,
            "amount": str(self._to_wire_amount(request.amount, request.currency)),
            "currency": request.currency,
            "customer": {"email": request.email},
        }
        if request.callback_url:
            body["redirect_url"] = request.callback_url
        if request.metadata:
            body["meta"] = dict(request.metadata)

        data = self._call("POST", "/payments", json_body=body)
        link = data.get("link")
        if not isinstance(link, str) or not link:
            raise gateway_error(
                "Flutterwave accepted the payment but returned no checkout link.",
                gateway=self.name,
            )

        return InitializeResult(
            gateway_reference=request.reference,
            authorization_url=link,
            raw=data,
        )

    def verify(self, gateway_reference: str) -> VerificationResult:
        """Fetch Flutterwave's authoritative view of one transaction.

        **Two identifiers.** Flutterwave knows a transaction by both our
        ``tx_ref`` and its own numeric ``id``, and offers a verification
        endpoint for each. This adapter uses
        ``GET /transactions/verify_by_reference`` as the primary path,
        because ``tx_ref`` is the only identifier KielSync is guaranteed
        to hold: the numeric id arrives with a webhook or a redirect, and
        the sweeper — which exists precisely for payments where neither
        arrived — would have nothing to verify with if it depended on
        one. The numeric id is returned in ``raw`` under ``id`` for the
        caller to persist onto the attempt.

        **Units.** The amount Flutterwave reports is in major units and
        is converted back to minor units before it leaves this method, so
        the caller compares like with like. As with Paystack, the amount
        returned is the gateway's own figure and is not reconciled
        against what was requested; that comparison belongs to the
        caller, which holds the originating transaction.
        """
        data = self._call(
            "GET",
            f"/transactions/verify_by_reference?tx_ref={quote(gateway_reference, safe='')}",
        )
        return self._verification_from(data, fallback_reference=gateway_reference)

    def refund(
        self, gateway_reference: str, amount: int | None = None
    ) -> RefundResult:
        """Refund a settled Flutterwave transaction, in full or in part.

        **Two identifiers, again.** The refund endpoint is addressed by
        Flutterwave's numeric transaction id, not by ``tx_ref``. When
        given a reference that is not already numeric, this method first
        resolves it through :meth:`verify`, which costs an extra round
        trip. That is deliberate: the alternative is requiring callers to
        have stored the numeric id, and a refund that cannot be issued
        because an id was never captured is worse than a refund that
        takes two calls.

        ``amount`` is in minor units and is converted to major units for
        the wire. When it is ``None`` the field is omitted, which is how
        Flutterwave is told to refund the full amount.
        """
        transaction_id, currency = self._resolve_transaction_id(gateway_reference)

        body: dict[str, Any] = {}
        if amount is not None:
            if not currency:
                raise gateway_error(
                    "Flutterwave refund needs the transaction currency to "
                    "convert a partial amount, and none was available.",
                    gateway=self.name,
                )
            body["amount"] = str(self._to_wire_amount(amount, currency))

        data = self._call(
            "POST",
            f"/transactions/{quote(str(transaction_id), safe='')}/refund",
            json_body=body,
        )

        refunded = data.get("amount_refunded")
        if refunded is None:
            refunded = data.get("amount")
        refund_currency = data.get("currency")
        if not isinstance(refund_currency, str) or not refund_currency:
            refund_currency = currency

        return RefundResult(
            gateway_reference=gateway_reference,
            status=_map_status(data.get("status"), _REFUND_STATUSES),
            amount=self._from_wire_amount(refunded, refund_currency) or 0,
            raw=data,
        )

    def parse_webhook(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> WebhookParseResult:
        """Authenticate a Flutterwave webhook, then decode the body.

        **This scheme is materially weaker than Paystack's, and the
        difference changes what callers may do with the result.**
        Flutterwave sends a ``verif-hash`` header containing a static
        shared secret configured in its dashboard. It is the same value
        on every delivery. It is *not* an HMAC over the request body, and
        it therefore proves only that the sender knows the secret — it
        says nothing whatsoever about the payload's contents.

        The practical consequences:

        - Anyone who has ever seen a valid header can replay it against
          any body they like. A ``signature_valid=True`` result here does
          **not** mean the amount, status, or reference in the payload
          are what Flutterwave sent.
        - The secret does not rotate per message, so it cannot expire a
          captured header.

        This is why the webhook handler must call :meth:`verify`
        independently before acting on anything in the payload, and why
        the shared resolution path treats the verification result as the
        only source of truth. For Paystack that independent call is
        defence in depth; here it is the *only* real defence, and
        skipping it would let a forged body drive a state change.

        The comparison still uses :func:`hmac.compare_digest`, because
        leaking the shared secret one byte at a time through response
        timing would remove even the sender check.

        As elsewhere, nothing is parsed or logged from the body until the
        header check passes, and a failed check returns
        :meth:`WebhookParseResult.rejected` with every other field empty.
        """
        supplied = _lookup_header(headers, SIGNATURE_HEADER)
        if not supplied:
            logger.warning("Flutterwave webhook rejected: verif-hash header missing")
            return WebhookParseResult.rejected()

        if not self._webhook_secret_hash:
            logger.error(
                "Flutterwave webhook rejected: no webhook_secret_hash is "
                "configured, so no delivery can be authenticated."
            )
            return WebhookParseResult.rejected()

        if not hmac.compare_digest(
            self._webhook_secret_hash.encode("utf-8"), supplied.encode("utf-8")
        ):
            logger.warning("Flutterwave webhook rejected: verif-hash mismatch")
            return WebhookParseResult.rejected()

        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise gateway_error(
                "Flutterwave webhook passed the verif-hash check but its body "
                "is not valid JSON.",
                gateway=self.name,
                exception=exc,
            ) from exc

        if not isinstance(payload, dict):
            raise gateway_error(
                "Flutterwave webhook passed the verif-hash check but its body "
                "is not a JSON object.",
                gateway=self.name,
            )

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Flutterwave webhook accepted: %s", redact(payload))

        event_type = payload.get("event") or payload.get("event.type")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            data = {}

        reference = data.get("tx_ref") or data.get("txRef")
        currency = data.get("currency")
        raw_status = data.get("status")

        return WebhookParseResult(
            signature_valid=True,
            event_id=derive_event_id(event_type, data.get("id"), raw_status),
            event_type=event_type if isinstance(event_type, str) else None,
            gateway_reference=reference if isinstance(reference, str) else None,
            status=_map_status(raw_status, _status_table_for(event_type)),
            amount=self._from_wire_amount(data.get("amount"), currency),
            currency=currency if isinstance(currency, str) else None,
            raw=payload,
        )

    # --- Units ------------------------------------------------------------

    def _to_wire_amount(self, minor: int, currency: str):
        """Convert stored minor units to the major units Flutterwave wants."""
        try:
            return to_major_units(minor, currency)
        except UnknownCurrency as exc:
            raise gateway_error(
                f"Cannot send an amount in {currency!r}: KielSync does not "
                f"know its minor-unit exponent, so the conversion to "
                f"Flutterwave's major units would be a guess.",
                gateway=self.name,
                exception=exc,
            ) from exc

    def _from_wire_amount(self, major: object, currency: object) -> int | None:
        """Convert a Flutterwave major-unit amount back to minor units.

        Returns ``None`` rather than guessing when the amount is absent
        or the currency is unknown. A missing amount is visible to the
        caller and fails the reconciliation comparison; a guessed one is
        not and does not.
        """
        if major is None or not isinstance(currency, str) or not currency:
            return None
        try:
            return from_major_units(major, currency)
        except (UnknownCurrency, ValueError, TypeError):
            logger.warning(
                "Flutterwave reported an amount that could not be converted "
                "to %s minor units",
                currency,
            )
            return None

    # --- HTTP plumbing ----------------------------------------------------

    def _verification_from(
        self, data: Mapping[str, Any], *, fallback_reference: str
    ) -> VerificationResult:
        reference = data.get("tx_ref") or data.get("txRef")
        currency = data.get("currency")
        return VerificationResult(
            gateway_reference=(
                reference if isinstance(reference, str) and reference
                else fallback_reference
            ),
            status=_map_status(data.get("status"), _TRANSACTION_STATUSES),
            amount=self._from_wire_amount(data.get("amount"), currency) or 0,
            currency=currency if isinstance(currency, str) else "",
            paid_at=_parse_timestamp(
                data.get("created_at") or data.get("createdAt")
            ),
            raw=data,
        )

    def _resolve_transaction_id(self, gateway_reference: str) -> tuple[str, str]:
        """Find Flutterwave's numeric id for a reference, and its currency."""
        if gateway_reference.isdigit():
            return gateway_reference, ""

        verification = self.verify(gateway_reference)
        transaction_id = verification.raw.get("id")
        if transaction_id is None:
            raise gateway_error(
                "Flutterwave did not report a numeric transaction id for this "
                "reference, so the refund endpoint cannot be addressed.",
                gateway=self.name,
            )
        return str(transaction_id), verification.currency

    def _call(
        self, method: str, path: str, *, json_body: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Perform one Flutterwave call and return the ``data`` object.

        Flutterwave's envelope is ``{"status": "success"|"error",
        "message": ..., "data": ...}``. Note that ``status`` is a
        *string* here, where Paystack uses a boolean — a difference that
        makes a truthiness check silently accept every error response,
        since the non-empty string ``"error"`` is truthy.
        """
        if json_body is not None and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Flutterwave %s %s request: %s", method, path, redact(json_body)
            )

        try:
            response = self._client.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            raise gateway_error(
                f"Flutterwave {method} {path} failed at the transport layer: "
                f"{type(exc).__name__}.",
                gateway=self.name,
                exception=exc,
            ) from exc

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if not isinstance(payload, dict):
            raise gateway_error(
                f"Flutterwave {method} {path} returned HTTP "
                f"{response.status_code} with a non-JSON body.",
                gateway=self.name,
                status_code=response.status_code,
            )

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Flutterwave %s %s response %s: %s",
                method,
                path,
                response.status_code,
                redact(payload),
            )

        message = payload.get("message")
        message = message if isinstance(message, str) else ""
        data = payload.get("data")
        data = data if isinstance(data, dict) else {}

        envelope_status = payload.get("status")
        succeeded = (
            isinstance(envelope_status, str)
            and envelope_status.strip().lower() == "success"
        )
        if response.is_error or not succeeded:
            processor_response = data.get("processor_response")
            raise gateway_error(
                f"Flutterwave {method} {path} returned HTTP "
                f"{response.status_code}: {message or 'no message supplied'}.",
                gateway=self.name,
                status_code=response.status_code,
                gateway_code=(
                    processor_response
                    if isinstance(processor_response, str)
                    else message
                ),
            )

        return data


def _lookup_header(headers: Mapping[str, str], name: str) -> str | None:
    """Read a header case-insensitively from any mapping."""
    direct = headers.get(name)
    if direct is not None:
        return direct
    target = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == target:
            return value
    return None
