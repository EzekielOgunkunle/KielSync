"""The sweeper: the safety net for every payment a webhook did not settle.

Webhooks get lost. The gateway's retry gives up, the endpoint was down
during the window, a deploy dropped the request, the payer closed the tab
before the redirect fired. Any integration that treats webhooks as
reliable eventually has customers who paid and were never credited, and
no way to find them.

This command is the answer: it asks the gateway directly about every
payment still open, on a schedule, using the same resolution path the
webhook handler uses. It is designed for a cPanel cron entry every ten to
fifteen minutes::

    */10 * * * * /path/to/python manage.py kielsync_sweep

Two work sources feed it. Transactions that have sat in PENDING longer
than the grace period are the lost-webhook case. Webhook events stored
with ``processed=False`` are the arrived-but-failed case, left behind
deliberately by the receiver when verification or resolution raised.

Everything here is written to survive overlapping runs, because cron will
eventually start a second one before the first has finished: rows are
locked with ``skip_locked``, batches are bounded, and no item can abort
the batch.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction as db_transaction
from django.utils import timezone

from kielsync.core.errors import GatewayError
from kielsync.core.exceptions import KielSyncError
from kielsync.django.models import PaymentAttempt, Transaction, WebhookEvent
from kielsync.django.services import Resolution, resolve_transaction
from kielsync.django.settings import get_gateway

logger = logging.getLogger(__name__)

# Non-secret operational tuning, so these come from Django settings
# rather than the environment. Credentials do not: see
# kielsync.django.settings for why those are read from os.environ only.
DEFAULT_SWEEP_AFTER_MINUTES = 20
DEFAULT_ABANDON_AFTER_HOURS = 24
DEFAULT_BATCH_LIMIT = 100


def _setting(name, default):
    return getattr(settings, name, default)


class Command(BaseCommand):
    help = (
        "Verify payments that are still open and resolve them, rescuing "
        "transactions whose webhook never arrived."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Report what would happen without writing anything. Still "
                "calls the gateway, since verify() is read-only and a dry "
                "run that skipped it would report nothing useful."
            ),
        )
        parser.add_argument(
            "--gateway",
            default=None,
            help="Restrict the sweep to one gateway, e.g. PAYSTACK.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help=(
                f"Maximum transactions to examine in this run "
                f"(default {DEFAULT_BATCH_LIMIT})."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        gateway_filter = (options["gateway"] or "").strip().upper() or None
        limit = options["limit"] or _setting(
            "KIELSYNC_SWEEP_BATCH_LIMIT", DEFAULT_BATCH_LIMIT
        )

        sweep_after = timedelta(
            minutes=_setting(
                "KIELSYNC_SWEEP_AFTER_MINUTES", DEFAULT_SWEEP_AFTER_MINUTES
            )
        )
        abandon_after = timedelta(
            hours=_setting(
                "KIELSYNC_ABANDON_AFTER_HOURS", DEFAULT_ABANDON_AFTER_HOURS
            )
        )

        now = timezone.now()
        counts = {
            "examined": 0,
            "resolved": 0,
            "mismatched": 0,
            "abandoned": 0,
            "errored": 0,
        }

        candidates = self._candidate_ids(
            now - sweep_after, gateway_filter, limit
        )

        # One adapter per gateway for the whole run, rather than one per
        # transaction. Each adapter owns a connection pool, so building a
        # fresh one per item would mean a TLS handshake per verification
        # against a host we are about to call a hundred times.
        self._adapters = {}
        try:
            for transaction_id in candidates:
                counts["examined"] += 1
                try:
                    outcome = self._sweep_one(
                        transaction_id,
                        gateway_filter=gateway_filter,
                        abandon_before=now - abandon_after,
                        dry_run=dry_run,
                    )
                except Exception:
                    # One unlucky transaction must never cost the other
                    # ninety-nine their sweep. Gateway errors in
                    # particular are expected here: the sweeper runs
                    # precisely when things are already going wrong.
                    counts["errored"] += 1
                    logger.exception(
                        "Sweep failed for transaction %s; continuing.",
                        transaction_id,
                    )
                    continue

                if outcome in ("resolved_success", "resolved_failed"):
                    counts["resolved"] += 1
                elif outcome == "mismatched":
                    counts["mismatched"] += 1
                elif outcome == "abandoned":
                    counts["abandoned"] += 1
        finally:
            self._close_adapters()

        self._report(counts, dry_run)
        return None

    def _adapter_for(self, gateway_name):
        if gateway_name not in self._adapters:
            self._adapters[gateway_name] = get_gateway(gateway_name)
        return self._adapters[gateway_name]

    def _close_adapters(self):
        for adapter in self._adapters.values():
            close = getattr(adapter, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # pragma: no cover - defensive
                    logger.warning("Failed to close a gateway adapter cleanly.")
        self._adapters = {}

    # --- selection --------------------------------------------------------

    def _candidate_ids(self, pending_before, gateway_filter, limit):
        """Transaction ids worth examining, oldest first.

        Ordering oldest first matters under a batch limit: it makes the
        sweep a queue rather than a lottery, so a transaction cannot be
        starved by newer ones arriving faster than the batch drains.
        """
        stale = Transaction.objects.filter(
            status=Transaction.Status.PENDING,
            updated_at__lte=pending_before,
        )

        unprocessed_references = WebhookEvent.objects.filter(
            processed=False
        ).exclude(gateway_reference="")
        if gateway_filter:
            unprocessed_references = unprocessed_references.filter(
                gateway=gateway_filter
            )
            stale = stale.filter(attempts__gateway=gateway_filter)

        from_events = Transaction.objects.filter(
            attempts__gateway_reference__in=unprocessed_references.values_list(
                "gateway_reference", flat=True
            )
        ).exclude(
            status__in=[
                Transaction.Status.SUCCESS,
                Transaction.Status.FAILED,
            ]
        )

        ids = (
            stale.union(from_events)
            .order_by("created_at")
            .values_list("id", flat=True)[:limit]
        )
        return list(ids)

    # --- one transaction --------------------------------------------------

    def _sweep_one(self, transaction_id, *, gateway_filter, abandon_before, dry_run):
        attempt = self._latest_attempt(transaction_id, gateway_filter)

        if attempt is None:
            # Nothing to verify against. Abandonment is still on the
            # table for a transaction that has been open long enough.
            return self._maybe_abandon(transaction_id, abandon_before, dry_run)

        adapter = self._adapter_for(attempt.gateway)
        try:
            verification = adapter.verify(attempt.gateway_reference)
        except GatewayError as exc:
            # Classified failures are logged without the payload and
            # re-raised so the caller counts them and moves on.
            logger.warning(
                "Verify failed for %s reference on transaction %s: %s "
                "(retryable=%s)",
                attempt.gateway,
                transaction_id,
                type(exc).__name__,
                exc.retryable,
            )
            raise

        if dry_run:
            return self._dry_run_outcome(
                transaction_id, verification, abandon_before
            )

        with db_transaction.atomic():
            locked = (
                Transaction.objects.select_for_update(skip_locked=True)
                .filter(pk=transaction_id)
                .first()
            )
            if locked is None:
                # Another sweep run holds this row. Leaving it to that
                # run is the whole point of skip_locked.
                logger.info(
                    "Transaction %s is locked by another run; skipping.",
                    transaction_id,
                )
                return "skipped"

            resolution = resolve_transaction(locked, verification)

            attempt.raw_response = dict(verification.raw or {})
            attempt.save(update_fields=["raw_response", "updated_at"])

            self._mark_events_processed(attempt.gateway_reference, resolution)

            if resolution is Resolution.RESOLVED_SUCCESS:
                return "resolved_success"
            if resolution is Resolution.RESOLVED_FAILED:
                return "resolved_failed"
            if resolution is Resolution.MISMATCHED:
                return "mismatched"

            locked.refresh_from_db()
            if (
                locked.status == Transaction.Status.PENDING
                and locked.updated_at <= abandon_before
            ):
                locked.transition(Transaction.Status.ABANDONED)
                logger.info(
                    "Transaction %s abandoned after the threshold.", transaction_id
                )
                return "abandoned"

        return "unchanged"

    def _latest_attempt(self, transaction_id, gateway_filter):
        attempts = PaymentAttempt.objects.filter(transaction_id=transaction_id)
        if gateway_filter:
            attempts = attempts.filter(gateway=gateway_filter)
        return attempts.order_by("-created_at").first()

    def _maybe_abandon(self, transaction_id, abandon_before, dry_run):
        if dry_run:
            candidate = Transaction.objects.filter(
                pk=transaction_id,
                status=Transaction.Status.PENDING,
                updated_at__lte=abandon_before,
            ).exists()
            return "abandoned" if candidate else "unchanged"

        with db_transaction.atomic():
            locked = (
                Transaction.objects.select_for_update(skip_locked=True)
                .filter(
                    pk=transaction_id,
                    status=Transaction.Status.PENDING,
                    updated_at__lte=abandon_before,
                )
                .first()
            )
            if locked is None:
                return "unchanged"
            locked.transition(Transaction.Status.ABANDONED)
            return "abandoned"

    def _dry_run_outcome(self, transaction_id, verification, abandon_before):
        """Work out what would have happened, without writing anything."""
        transaction = Transaction.objects.get(pk=transaction_id)
        from kielsync.core.gateways.base import PaymentStatus

        if verification.status is PaymentStatus.SUCCESS:
            if (
                verification.amount != transaction.amount
                or (verification.currency or "").strip().upper()
                != (transaction.currency or "").strip().upper()
            ):
                return "mismatched"
            return "resolved_success"
        if verification.status is PaymentStatus.FAILED:
            return "resolved_failed"
        if transaction.updated_at <= abandon_before:
            return "abandoned"
        return "unchanged"

    def _mark_events_processed(self, gateway_reference, resolution):
        """Close out webhook events once their transaction is settled.

        Only terminal resolutions clear the backlog. A still-pending
        payment keeps its event queued so the next run picks it up again.
        """
        if resolution in (Resolution.PENDING,):
            return
        WebhookEvent.objects.filter(
            gateway_reference=gateway_reference, processed=False
        ).update(processed=True, processed_at=timezone.now())

    # --- output -----------------------------------------------------------

    def _report(self, counts, dry_run):
        prefix = "[dry-run] " if dry_run else ""
        summary = (
            f"{prefix}examined={counts['examined']} "
            f"resolved={counts['resolved']} "
            f"mismatched={counts['mismatched']} "
            f"abandoned={counts['abandoned']} "
            f"errored={counts['errored']}"
        )
        style = self.style.WARNING if counts["mismatched"] else self.style.SUCCESS
        self.stdout.write(style(summary))
        logger.info("kielsync_sweep %s", summary)
