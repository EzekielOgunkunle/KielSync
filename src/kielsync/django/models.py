import uuid

from django.db import models

from kielsync.core.exceptions import InvalidTransition
from kielsync.core.states import (
    PAYMENT_ATTEMPT_TRANSITIONS,
    TRANSACTION_TRANSITIONS,
    perform_transition,
)


class Transaction(models.Model):
    class Status(models.TextChoices):
        CREATED = "CREATED", "Created"
        PENDING = "PENDING", "Pending"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"
        ABANDONED = "ABANDONED", "Abandoned"

    class ReconciliationStatus(models.TextChoices):
        UNRECONCILED = "UNRECONCILED", "Unreconciled"
        MATCHED = "MATCHED", "Matched"
        MISMATCHED = "MISMATCHED", "Mismatched"
        MANUAL_REVIEW = "MANUAL_REVIEW", "Manual review"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=64, unique=True, db_index=True)
    amount = models.BigIntegerField()
    currency = models.CharField(max_length=3)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.CREATED
    )
    reconciliation_status = models.CharField(
        max_length=16,
        choices=ReconciliationStatus.choices,
        default=ReconciliationStatus.UNRECONCILED,
    )
    customer_email = models.EmailField(blank=True)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "kielsync"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="kielsync_transaction_amount_positive",
            ),
        ]

    def __str__(self):
        return f"Transaction({self.id}, {self.status})"

    def transition(self, new_status):
        perform_transition(self, new_status, TRANSACTION_TRANSITIONS)

    def mark_success(self, *, verified=False):
        if verified is not True:
            raise InvalidTransition(
                "Transaction.mark_success() requires verified=True; "
                "success may only be recorded after a gateway verify() call."
            )
        self.transition(self.Status.SUCCESS)


class PaymentAttempt(models.Model):
    class Gateway(models.TextChoices):
        PAYSTACK = "PAYSTACK", "Paystack"
        FLUTTERWAVE = "FLUTTERWAVE", "Flutterwave"

    class Status(models.TextChoices):
        INITIATED = "INITIATED", "Initiated"
        REDIRECTED = "REDIRECTED", "Redirected"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"
        EXPIRED = "EXPIRED", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transaction = models.ForeignKey(
        Transaction, related_name="attempts", on_delete=models.PROTECT
    )
    gateway = models.CharField(max_length=16, choices=Gateway.choices)
    gateway_reference = models.CharField(max_length=128, unique=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.INITIATED
    )
    error_code = models.CharField(max_length=64, blank=True)
    error_retryable = models.BooleanField(null=True)
    raw_response = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "kielsync"
        indexes = [
            models.Index(
                fields=["transaction", "created_at"],
                name="kielsync_attempt_txn_idx",
            ),
        ]

    def __str__(self):
        return f"PaymentAttempt({self.id}, {self.gateway}, {self.status})"

    def transition(self, new_status):
        perform_transition(self, new_status, PAYMENT_ATTEMPT_TRANSITIONS)


class WebhookEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    gateway = models.CharField(max_length=16, choices=PaymentAttempt.Gateway.choices)
    event_id = models.CharField(max_length=128)
    payload = models.JSONField()
    signature_valid = models.BooleanField(default=False)
    processed = models.BooleanField(default=False)
    processed_at = models.DateTimeField(null=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "kielsync"
        constraints = [
            models.UniqueConstraint(
                fields=["gateway", "event_id"],
                name="kielsync_webhookevent_gateway_event_id_uniq",
            ),
        ]

    def __str__(self):
        return f"WebhookEvent({self.gateway}, {self.event_id})"
