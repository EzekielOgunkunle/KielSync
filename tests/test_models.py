import uuid

import pytest
from django.db import IntegrityError
from django.db import transaction as db_transaction

from kielsync.django.models import PaymentAttempt, Transaction, WebhookEvent

pytestmark = pytest.mark.django_db


def test_duplicate_idempotency_key_raises_integrity_error():
    key = str(uuid.uuid4())
    Transaction.objects.create(idempotency_key=key, amount=1000, currency="NGN")
    with pytest.raises(IntegrityError):
        with db_transaction.atomic():
            Transaction.objects.create(idempotency_key=key, amount=2000, currency="NGN")


def test_duplicate_webhook_event_gateway_event_id_raises_integrity_error():
    WebhookEvent.objects.create(gateway="PAYSTACK", event_id="evt_1", payload={})
    with pytest.raises(IntegrityError):
        with db_transaction.atomic():
            WebhookEvent.objects.create(gateway="PAYSTACK", event_id="evt_1", payload={})


def test_different_gateway_same_event_id_is_allowed():
    WebhookEvent.objects.create(gateway="PAYSTACK", event_id="evt_1", payload={})
    WebhookEvent.objects.create(gateway="FLUTTERWAVE", event_id="evt_1", payload={})
    assert WebhookEvent.objects.count() == 2


def test_duplicate_gateway_reference_raises_integrity_error():
    txn = Transaction.objects.create(
        idempotency_key=str(uuid.uuid4()), amount=1000, currency="NGN"
    )
    PaymentAttempt.objects.create(
        transaction=txn, gateway="PAYSTACK", gateway_reference="ref_1"
    )
    with pytest.raises(IntegrityError):
        with db_transaction.atomic():
            PaymentAttempt.objects.create(
                transaction=txn, gateway="FLUTTERWAVE", gateway_reference="ref_1"
            )


def test_zero_amount_violates_check_constraint():
    with pytest.raises(IntegrityError):
        with db_transaction.atomic():
            Transaction.objects.create(
                idempotency_key=str(uuid.uuid4()), amount=0, currency="NGN"
            )


def test_negative_amount_violates_check_constraint():
    with pytest.raises(IntegrityError):
        with db_transaction.atomic():
            Transaction.objects.create(
                idempotency_key=str(uuid.uuid4()), amount=-500, currency="NGN"
            )


def test_transaction_default_statuses():
    txn = Transaction.objects.create(
        idempotency_key=str(uuid.uuid4()), amount=1000, currency="NGN"
    )
    assert txn.status == Transaction.Status.CREATED
    assert txn.reconciliation_status == Transaction.ReconciliationStatus.UNRECONCILED
