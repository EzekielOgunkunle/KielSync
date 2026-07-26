"""The webhook receiver.

One endpoint per gateway. The order of operations below is not
incidental — each step exists because doing it later, or not at all, is a
known way to lose money or take on unbounded writes:

1. Read the raw request body as bytes, before anything parses it.
   Signatures are computed over bytes, and a JSON round trip on the way
   in invalidates a genuine signature.
2. Authenticate. Nothing is stored, parsed, or trusted until this passes.
3. Deduplicate on ``(gateway, event_id)``. Gateways retry, sometimes for
   days, and a handler that processes every delivery double-credits.
4. **Call verify() independently.** The webhook is a notification. It is
   never the source of truth, no matter how well it authenticated. This
   is the step that defeats payload tampering, and for Flutterwave —
   whose ``verif-hash`` says nothing about the body — it is the only
   thing standing between a forged payload and a state change.
5. Resolve under a row lock, through the same function the sweeper uses.
6. Return 200 once the event is durably stored, even if resolution
   failed. The event is on disk with ``processed=False`` and the sweeper
   will retry it; a non-200 here just asks the gateway to send it again,
   and a gateway retrying a request that fails deterministically is a
   redelivery storm.
"""

from __future__ import annotations

import logging

from django.db import transaction as db_transaction
from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from kielsync.core.exceptions import ConfigurationError, KielSyncError
from kielsync.django.models import PaymentAttempt, Transaction, WebhookEvent
from kielsync.django.services import resolve_transaction
from kielsync.django.settings import get_gateway

logger = logging.getLogger(__name__)

__all__ = ["webhook"]


def _source_ip(request) -> str:
    """The peer address, for logging rejected deliveries.

    Deliberately ``REMOTE_ADDR`` and not ``X-Forwarded-For``: the latter
    is attacker-controlled unless a trusted proxy overwrites it, and a
    log line that can be forged is worse than one that is merely
    imprecise behind a load balancer.
    """
    return request.META.get("REMOTE_ADDR", "unknown")


@csrf_exempt
def webhook(request, gateway: str):
    """Receive one webhook delivery for ``gateway``.

    CSRF exempt because the caller is a payment gateway with no session
    and no cookie to protect; authentication comes from the signature
    check instead, which is strictly stronger than a CSRF token here.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    try:
        adapter = get_gateway(gateway)
    except ConfigurationError:
        # Either an unknown gateway name in the URL or a missing
        # credential. Both are ours to fix, and neither should tell an
        # unauthenticated caller which.
        logger.exception("Webhook received for unusable gateway %r", gateway)
        return HttpResponse(status=404)

    gateway_name = getattr(adapter, "name", gateway.upper())

    # 1 & 2. Raw bytes, then authenticate. Nothing before this point
    # touches the payload.
    raw_body = request.body
    try:
        parsed = adapter.parse_webhook(raw_body, request.headers)
    except KielSyncError:
        # Authenticated sender, undecodable body. Persisting it would
        # store something we cannot deduplicate or act on.
        logger.exception(
            "Webhook from %s (%s) authenticated but could not be decoded.",
            gateway_name,
            _source_ip(request),
        )
        return HttpResponse(status=400)

    # 3. Reject before any write. Storing unauthenticated payloads would
    # let anyone who can reach this URL fill the table.
    if not parsed.signature_valid:
        logger.warning(
            "Rejected unauthenticated webhook for %s from %s; nothing stored.",
            gateway_name,
            _source_ip(request),
        )
        return HttpResponse(status=401)

    if not parsed.event_id:
        # Authentic, but with nothing stable to deduplicate on. Storing
        # it would create a fresh row on every redelivery, which is the
        # unbounded write this endpoint is trying to avoid.
        logger.error(
            "Authenticated %s webhook carries no usable event id; refusing to "
            "store an undeduplicable event. event_type=%r reference=%r",
            gateway_name,
            parsed.event_type,
            parsed.gateway_reference,
        )
        return HttpResponse(status=400)

    event, created = WebhookEvent.objects.get_or_create(
        gateway=gateway_name,
        event_id=parsed.event_id,
        defaults={
            "payload": parsed.raw,
            "signature_valid": True,
            "gateway_reference": parsed.gateway_reference or "",
        },
    )

    # 4. The idempotency guarantee. A redelivery of something already
    # dealt with stops here without a second verify() call or a second
    # state change.
    if not created and event.processed:
        logger.info(
            "Duplicate %s webhook %s already processed; acknowledging.",
            gateway_name,
            parsed.event_id,
        )
        return JsonResponse({"status": "duplicate"}, status=200)

    try:
        _process(adapter, event, parsed, gateway_name)
    except Exception:
        # 6. The event is durably stored with processed=False. Reporting
        # failure to the gateway would only buy a redelivery of something
        # that just failed; the sweeper will pick this up instead.
        logger.exception(
            "Failed to process %s webhook %s; left for the sweeper.",
            gateway_name,
            parsed.event_id,
        )
        return JsonResponse({"status": "accepted"}, status=200)

    return JsonResponse({"status": "processed"}, status=200)


def _process(adapter, event: WebhookEvent, parsed, gateway_name: str) -> None:
    """Verify independently, then resolve under a lock."""
    reference = parsed.gateway_reference
    if not reference:
        logger.error(
            "Authenticated %s webhook %s carries no transaction reference.",
            gateway_name,
            event.event_id,
        )
        return

    # 5. The webhook told us something happened. The gateway's own
    # verification endpoint tells us what. Only the second one is
    # allowed to move money in the ledger.
    verification = adapter.verify(reference)

    attempt = (
        PaymentAttempt.objects.filter(gateway_reference=reference)
        .select_related("transaction")
        .first()
    )
    if attempt is None:
        logger.warning(
            "No payment attempt found for %s reference %r; event stored but "
            "not resolved.",
            gateway_name,
            reference,
        )
        return

    with db_transaction.atomic():
        transaction = (
            Transaction.objects.select_for_update()
            .get(pk=attempt.transaction_id)
        )
        resolve_transaction(transaction, verification)

        # Record the gateway's numeric id and latest body on the attempt.
        # Flutterwave's id in particular arrives only this way.
        attempt.raw_response = dict(verification.raw or {})
        attempt.save(update_fields=["raw_response", "updated_at"])

        event.processed = True
        event.processed_at = timezone.now()
        event.gateway_reference = reference
        event.save(
            update_fields=["processed", "processed_at", "gateway_reference"]
        )
