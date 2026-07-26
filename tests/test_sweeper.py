"""Tests for the kielsync_sweep management command.

The sweeper exists for the case where everything else already failed, so
the tests are mostly about resilience: it must survive a gateway that is
down, overlapping cron runs, and being run twice in a row over the same
data.
"""

import uuid
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from kielsync.core.errors import RetryableGatewayError, TerminalGatewayError
from kielsync.core.gateways.base import PaymentStatus, VerificationResult
from kielsync.django.models import PaymentAttempt, Transaction, WebhookEvent

pytestmark = pytest.mark.django_db

AMOUNT = 500_000


def make_transaction(*, minutes_old=60, amount=AMOUNT, currency="NGN",
                     status=Transaction.Status.PENDING, reference=None,
                     gateway=PaymentAttempt.Gateway.PAYSTACK, with_attempt=True):
    reference = reference or f"kiel_sweep_{uuid.uuid4().hex[:12]}"
    transaction = Transaction.objects.create(
        idempotency_key=str(uuid.uuid4()), amount=amount, currency=currency
    )
    stamp = timezone.now() - timedelta(minutes=minutes_old)
    Transaction.objects.filter(pk=transaction.pk).update(
        status=status, updated_at=stamp, created_at=stamp
    )
    transaction.refresh_from_db()

    if with_attempt:
        attempt = PaymentAttempt.objects.create(
            transaction=transaction, gateway=gateway, gateway_reference=reference
        )
        PaymentAttempt.objects.filter(pk=attempt.pk).update(created_at=stamp)
    return transaction, reference


class SweepGateway:
    """Adapter stand-in whose verify() the test scripts."""

    name = "PAYSTACK"

    def __init__(self, *, status=PaymentStatus.SUCCESS, amount=AMOUNT,
                 currency="NGN", error=None, errors_for=None):
        self.status = status
        self.amount = amount
        self.currency = currency
        self.error = error
        self.errors_for = errors_for or {}
        self.calls = []
        self.closed = 0

    def verify(self, gateway_reference):
        self.calls.append(gateway_reference)
        if gateway_reference in self.errors_for:
            raise self.errors_for[gateway_reference]
        if self.error is not None:
            raise self.error
        return VerificationResult(
            gateway_reference=gateway_reference,
            status=self.status,
            amount=self.amount,
            currency=self.currency,
            raw={"id": 999, "reference": gateway_reference},
        )

    def initialize(self, request):  # pragma: no cover
        raise NotImplementedError

    def refund(self, gateway_reference, amount=None):  # pragma: no cover
        raise NotImplementedError

    def parse_webhook(self, raw_body, headers):  # pragma: no cover
        raise NotImplementedError

    def close(self):
        self.closed += 1


@pytest.fixture
def install_gateway(monkeypatch):
    def install(gateway):
        monkeypatch.setattr(
            "kielsync.django.management.commands.kielsync_sweep.get_gateway",
            lambda name: gateway,
        )
        return gateway

    return install


def sweep(**kwargs):
    out = StringIO()
    call_command("kielsync_sweep", stdout=out, **kwargs)
    return out.getvalue()


class TestRescue:
    def test_rescues_a_pending_transaction_whose_webhook_never_arrived(
        self, install_gateway
    ):
        """The reason this command exists."""
        transaction, reference = make_transaction(minutes_old=60)
        gateway = install_gateway(SweepGateway())

        output = sweep()

        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.SUCCESS
        assert gateway.calls == [reference]
        assert "resolved=1" in output

    def test_marks_the_attempt_and_stores_the_verification(self, install_gateway):
        transaction, reference = make_transaction()
        install_gateway(SweepGateway())
        sweep()
        attempt = PaymentAttempt.objects.get(gateway_reference=reference)
        assert attempt.status == PaymentAttempt.Status.SUCCESS
        assert attempt.raw_response["id"] == 999

    def test_resolves_a_failed_payment(self, install_gateway):
        transaction, _ = make_transaction()
        install_gateway(SweepGateway(status=PaymentStatus.FAILED))
        sweep()
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.FAILED

    def test_leaves_a_still_pending_payment_alone(self, install_gateway):
        transaction, _ = make_transaction(minutes_old=30)
        install_gateway(SweepGateway(status=PaymentStatus.PENDING))
        output = sweep()
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.PENDING
        assert "resolved=0" in output

    def test_ignores_transactions_inside_the_grace_period(self, install_gateway):
        """A payment two minutes old is probably mid-checkout, and asking
        the gateway about it wastes a call and answers PENDING."""
        transaction, _ = make_transaction(minutes_old=2)
        gateway = install_gateway(SweepGateway())
        sweep()
        assert gateway.calls == []
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.PENDING

    def test_ignores_already_terminal_transactions(self, install_gateway):
        make_transaction(status=Transaction.Status.SUCCESS)
        make_transaction(status=Transaction.Status.FAILED)
        gateway = install_gateway(SweepGateway())
        sweep()
        assert gateway.calls == []

    def test_picks_up_an_unprocessed_webhook_event(self, install_gateway):
        """The receiver stores events it could not process. They are the
        sweeper's second work source."""
        transaction, reference = make_transaction(minutes_old=1)
        WebhookEvent.objects.create(
            gateway="PAYSTACK",
            event_id="charge.success:1",
            gateway_reference=reference,
            payload={},
            signature_valid=True,
            processed=False,
        )
        gateway = install_gateway(SweepGateway())

        sweep()

        assert gateway.calls == [reference]
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.SUCCESS
        assert WebhookEvent.objects.get().processed is True


class TestMismatch:
    def test_a_wrong_amount_is_counted_and_flagged(self, install_gateway):
        transaction, _ = make_transaction(amount=AMOUNT)
        install_gateway(SweepGateway(amount=1))

        output = sweep()

        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.PENDING
        assert transaction.reconciliation_status == (
            Transaction.ReconciliationStatus.MISMATCHED
        )
        assert "mismatched=1" in output

    def test_mismatch_does_not_count_as_resolved(self, install_gateway):
        make_transaction()
        install_gateway(SweepGateway(amount=1))
        output = sweep()
        assert "resolved=0" in output


class TestAbandonment:
    def test_abandons_past_the_threshold(self, install_gateway):
        transaction, _ = make_transaction(minutes_old=60 * 30)
        install_gateway(SweepGateway(status=PaymentStatus.PENDING))

        output = sweep()

        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.ABANDONED
        assert "abandoned=1" in output

    def test_does_not_abandon_before_the_threshold(self, install_gateway):
        transaction, _ = make_transaction(minutes_old=60 * 20)
        install_gateway(SweepGateway(status=PaymentStatus.PENDING))
        sweep()
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.PENDING

    def test_verifies_before_abandoning(self, install_gateway):
        """A payment that settled at hour 25 must be rescued, not written
        off, so the gateway is asked before the clock is applied."""
        transaction, reference = make_transaction(minutes_old=60 * 30)
        gateway = install_gateway(SweepGateway(status=PaymentStatus.SUCCESS))

        sweep()

        assert gateway.calls == [reference]
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.SUCCESS

    def test_threshold_is_configurable(self, install_gateway, settings):
        settings.KIELSYNC_ABANDON_AFTER_HOURS = 1
        transaction, _ = make_transaction(minutes_old=120)
        install_gateway(SweepGateway(status=PaymentStatus.PENDING))
        sweep()
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.ABANDONED

    def test_grace_period_is_configurable(self, install_gateway, settings):
        settings.KIELSYNC_SWEEP_AFTER_MINUTES = 120
        transaction, _ = make_transaction(minutes_old=60)
        gateway = install_gateway(SweepGateway())
        sweep()
        assert gateway.calls == []


class TestErrorIsolation:
    def test_a_gateway_error_does_not_abort_the_batch(self, install_gateway):
        """The sweeper runs precisely when things are already going wrong,
        so one failing item must not cost the others their sweep."""
        _, bad_reference = make_transaction(minutes_old=90)
        good, good_reference = make_transaction(minutes_old=60)

        install_gateway(
            SweepGateway(
                errors_for={bad_reference: RetryableGatewayError("gateway down")}
            )
        )

        output = sweep()

        good.refresh_from_db()
        assert good.status == Transaction.Status.SUCCESS
        assert "errored=1" in output
        assert "resolved=1" in output

    @pytest.mark.parametrize(
        "error",
        [
            RetryableGatewayError("down"),
            TerminalGatewayError("bad key"),
            RuntimeError("something unexpected"),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_every_error_type_is_contained(self, install_gateway, error):
        transaction, _ = make_transaction()
        install_gateway(SweepGateway(error=error))
        output = sweep()
        assert "errored=1" in output
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.PENDING

    def test_errors_are_logged_without_the_payload(self, install_gateway, caplog):
        make_transaction()
        install_gateway(SweepGateway(error=RetryableGatewayError("down")))
        logger = "kielsync.django.management.commands.kielsync_sweep"
        with caplog.at_level("WARNING", logger=logger):
            sweep()
        assert "retryable=True" in caplog.text


class TestIdempotence:
    def test_two_consecutive_runs_produce_one_transition(self, install_gateway):
        transaction, _ = make_transaction()
        install_gateway(SweepGateway())

        sweep()
        transaction.refresh_from_db()
        first_updated = transaction.updated_at

        second = sweep()
        transaction.refresh_from_db()

        assert transaction.status == Transaction.Status.SUCCESS
        assert transaction.updated_at == first_updated
        # Already terminal, so it is not a candidate any more.
        assert "examined=0" in second

    def test_running_five_times_is_harmless(self, install_gateway):
        transaction, _ = make_transaction()
        install_gateway(SweepGateway())
        for _ in range(5):
            sweep()
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.SUCCESS

    def test_repeated_runs_over_a_mismatch_do_not_pile_up(self, install_gateway):
        transaction, _ = make_transaction()
        install_gateway(SweepGateway(amount=1))
        sweep()
        transaction.refresh_from_db()
        after_first = transaction.updated_at
        sweep()
        transaction.refresh_from_db()
        assert transaction.updated_at == after_first


class TestFlags:
    def test_dry_run_writes_nothing(self, install_gateway):
        transaction, _ = make_transaction()
        install_gateway(SweepGateway())

        output = sweep(dry_run=True)

        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.PENDING
        assert "[dry-run]" in output
        assert "resolved=1" in output

    def test_dry_run_still_reports_mismatches(self, install_gateway):
        make_transaction()
        install_gateway(SweepGateway(amount=1))
        assert "mismatched=1" in sweep(dry_run=True)

    def test_dry_run_reports_pending_abandonment(self, install_gateway):
        make_transaction(minutes_old=60 * 30)
        install_gateway(SweepGateway(status=PaymentStatus.PENDING))
        assert "abandoned=1" in sweep(dry_run=True)

    def test_dry_run_leaves_webhook_events_unprocessed(self, install_gateway):
        _, reference = make_transaction()
        WebhookEvent.objects.create(
            gateway="PAYSTACK", event_id="e1", gateway_reference=reference,
            payload={}, signature_valid=True, processed=False,
        )
        install_gateway(SweepGateway())
        sweep(dry_run=True)
        assert WebhookEvent.objects.get().processed is False

    def test_limit_bounds_the_batch(self, install_gateway):
        for _ in range(5):
            make_transaction()
        gateway = install_gateway(SweepGateway())
        output = sweep(limit=2)
        assert len(gateway.calls) == 2
        assert "examined=2" in output

    def test_oldest_first(self, install_gateway):
        """Under a batch limit, ordering makes the sweep a queue rather
        than a lottery, so nothing starves."""
        _, oldest = make_transaction(minutes_old=300)
        _, middle = make_transaction(minutes_old=200)
        make_transaction(minutes_old=100)
        gateway = install_gateway(SweepGateway())
        sweep(limit=2)
        assert gateway.calls == [oldest, middle]

    def test_gateway_flag_restricts_the_sweep(self, install_gateway):
        _, paystack_reference = make_transaction(
            gateway=PaymentAttempt.Gateway.PAYSTACK
        )
        make_transaction(gateway=PaymentAttempt.Gateway.FLUTTERWAVE)
        gateway = install_gateway(SweepGateway())

        sweep(gateway="PAYSTACK")

        assert gateway.calls == [paystack_reference]

    def test_gateway_flag_is_case_insensitive(self, install_gateway):
        _, reference = make_transaction()
        gateway = install_gateway(SweepGateway())
        sweep(gateway="paystack")
        assert gateway.calls == [reference]

    def test_batch_limit_default_is_configurable(self, install_gateway, settings):
        settings.KIELSYNC_SWEEP_BATCH_LIMIT = 1
        for _ in range(3):
            make_transaction()
        gateway = install_gateway(SweepGateway())
        sweep()
        assert len(gateway.calls) == 1


class TestConnectionReuse:
    def test_one_adapter_serves_the_whole_batch(self, install_gateway):
        """Each adapter owns a connection pool. Building one per item
        would mean a TLS handshake per verification against a host the
        run is about to call a hundred times."""
        for _ in range(5):
            make_transaction()
        gateway = install_gateway(SweepGateway())

        sweep()

        assert len(gateway.calls) == 5
        assert gateway.closed == 1

    def test_adapters_are_closed_even_when_an_item_fails(self, install_gateway):
        make_transaction()
        gateway = install_gateway(SweepGateway(error=RuntimeError("boom")))
        sweep()
        assert gateway.closed == 1


class TestSummary:
    def test_reports_all_five_counters(self, install_gateway):
        install_gateway(SweepGateway())
        output = sweep()
        for field in ("examined", "resolved", "mismatched", "abandoned", "errored"):
            assert field in output

    def test_empty_sweep_is_not_an_error(self, install_gateway):
        install_gateway(SweepGateway())
        assert "examined=0" in sweep()

    def test_a_transaction_without_an_attempt_does_not_crash(self, install_gateway):
        transaction, _ = make_transaction(minutes_old=60 * 30, with_attempt=False)
        install_gateway(SweepGateway())
        output = sweep()
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.ABANDONED
        assert "errored=0" in output
