import uuid

import pytest

from kielsync.core.exceptions import InvalidTransition
from kielsync.django.models import PaymentAttempt, Transaction

pytestmark = pytest.mark.django_db

TRANSACTION_STATUSES = [choice.value for choice in Transaction.Status]
ATTEMPT_STATUSES = [choice.value for choice in PaymentAttempt.Status]

TRANSACTION_LEGAL_MOVES = {
    ("CREATED", "PENDING"),
    ("PENDING", "SUCCESS"),
    ("PENDING", "FAILED"),
    ("PENDING", "ABANDONED"),
}

ATTEMPT_LEGAL_MOVES = {
    ("INITIATED", "REDIRECTED"),
    ("INITIATED", "FAILED"),
    ("INITIATED", "EXPIRED"),
    ("REDIRECTED", "SUCCESS"),
    ("REDIRECTED", "FAILED"),
    ("REDIRECTED", "EXPIRED"),
}


def make_transaction(status=Transaction.Status.CREATED):
    return Transaction.objects.create(
        idempotency_key=str(uuid.uuid4()),
        amount=1000,
        currency="NGN",
        status=status,
    )


def make_attempt(status=PaymentAttempt.Status.INITIATED):
    transaction = make_transaction(status=Transaction.Status.PENDING)
    return PaymentAttempt.objects.create(
        transaction=transaction,
        gateway=PaymentAttempt.Gateway.PAYSTACK,
        gateway_reference=str(uuid.uuid4()),
        status=status,
    )


@pytest.mark.parametrize("from_status", TRANSACTION_STATUSES)
@pytest.mark.parametrize("to_status", TRANSACTION_STATUSES)
def test_transaction_transition(from_status, to_status):
    txn = make_transaction(status=from_status)
    if (from_status, to_status) in TRANSACTION_LEGAL_MOVES:
        txn.transition(to_status)
        txn.refresh_from_db()
        assert txn.status == to_status
    else:
        with pytest.raises(InvalidTransition):
            txn.transition(to_status)
        txn.refresh_from_db()
        assert txn.status == from_status


@pytest.mark.parametrize("from_status", ATTEMPT_STATUSES)
@pytest.mark.parametrize("to_status", ATTEMPT_STATUSES)
def test_payment_attempt_transition(from_status, to_status):
    attempt = make_attempt(status=from_status)
    if (from_status, to_status) in ATTEMPT_LEGAL_MOVES:
        attempt.transition(to_status)
        attempt.refresh_from_db()
        assert attempt.status == to_status
    else:
        with pytest.raises(InvalidTransition):
            attempt.transition(to_status)
        attempt.refresh_from_db()
        assert attempt.status == from_status


def test_mark_success_without_verified_kwarg_raises():
    txn = make_transaction(status=Transaction.Status.PENDING)
    with pytest.raises(InvalidTransition):
        txn.mark_success()
    txn.refresh_from_db()
    assert txn.status == Transaction.Status.PENDING


def test_mark_success_verified_false_raises():
    txn = make_transaction(status=Transaction.Status.PENDING)
    with pytest.raises(InvalidTransition):
        txn.mark_success(verified=False)
    txn.refresh_from_db()
    assert txn.status == Transaction.Status.PENDING


def test_mark_success_rejects_positional_argument():
    txn = make_transaction(status=Transaction.Status.PENDING)
    with pytest.raises(TypeError):
        txn.mark_success(True)


def test_mark_success_verified_true_succeeds():
    txn = make_transaction(status=Transaction.Status.PENDING)
    txn.mark_success(verified=True)
    txn.refresh_from_db()
    assert txn.status == Transaction.Status.SUCCESS


def test_mark_success_from_created_still_raises_invalid_transition():
    txn = make_transaction(status=Transaction.Status.CREATED)
    with pytest.raises(InvalidTransition):
        txn.mark_success(verified=True)
