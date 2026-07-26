"""Tests for the webhook receiver.

The endpoint's job is to be safe under the conditions gateways actually
produce: the same delivery arriving twice, a forged payload, a payload
that authenticates but lies about the amount, and a resolution that
fails halfway. Each of those is a test here.

The adapter is stubbed rather than mocked at the HTTP layer, because what
is under test is the handler's order of operations — authenticate,
deduplicate, verify independently, resolve, acknowledge — not the
adapter, which has its own suite.
"""

import hashlib
import hmac
import json
import uuid

import pytest
from django.urls import reverse

from kielsync.core.errors import RetryableGatewayError
from kielsync.core.gateways.base import (
    PaymentStatus,
    VerificationResult,
    WebhookParseResult,
)
from kielsync.django.models import PaymentAttempt, Transaction, WebhookEvent

pytestmark = pytest.mark.django_db

PAYSTACK_KEY = "sk_test_kielsync_endpoint"
REFERENCE = "kiel_txn_endpoint_0001"
AMOUNT = 500_000


def webhook_url(gateway="PAYSTACK"):
    return reverse("kielsync:webhook", kwargs={"gateway": gateway})


def paystack_body(amount=AMOUNT, status="success", event_id=302961):
    return json.dumps(
        {
            "event": "charge.success",
            "data": {
                "id": event_id,
                "reference": REFERENCE,
                "status": status,
                "amount": amount,
                "currency": "NGN",
            },
        }
    ).encode()


def sign(body, key=PAYSTACK_KEY):
    return hmac.new(key.encode(), body, hashlib.sha512).hexdigest()


@pytest.fixture
def paystack_env(monkeypatch):
    monkeypatch.setenv("KIELSYNC_PAYSTACK_SECRET_KEY", PAYSTACK_KEY)


@pytest.fixture
def transaction():
    txn = Transaction.objects.create(
        idempotency_key=str(uuid.uuid4()),
        amount=AMOUNT,
        currency="NGN",
    )
    Transaction.objects.filter(pk=txn.pk).update(status=Transaction.Status.PENDING)
    txn.refresh_from_db()
    PaymentAttempt.objects.create(
        transaction=txn,
        gateway=PaymentAttempt.Gateway.PAYSTACK,
        gateway_reference=REFERENCE,
    )
    return txn


class StubGateway:
    """A gateway whose verify() the test controls.

    The point of the endpoint is that it trusts verify() and not the
    payload, so the tests need to be able to make the two disagree.
    """

    name = "PAYSTACK"

    def __init__(self, verification=None, *, verify_error=None, key=PAYSTACK_KEY):
        self._verification = verification
        self._verify_error = verify_error
        self._key = key
        self.verify_calls = []

    def initialize(self, request):  # pragma: no cover - not exercised here
        raise NotImplementedError

    def refund(self, gateway_reference, amount=None):  # pragma: no cover
        raise NotImplementedError

    def verify(self, gateway_reference):
        self.verify_calls.append(gateway_reference)
        if self._verify_error is not None:
            raise self._verify_error
        if self._verification is not None:
            return self._verification
        return VerificationResult(
            gateway_reference=gateway_reference,
            status=PaymentStatus.SUCCESS,
            amount=AMOUNT,
            currency="NGN",
            raw={"id": 302961},
        )

    def parse_webhook(self, raw_body, headers):
        supplied = None
        for name, value in headers.items():
            if name.lower() == "x-paystack-signature":
                supplied = value
        expected = hmac.new(self._key.encode(), raw_body, hashlib.sha512).hexdigest()
        if not supplied or not hmac.compare_digest(expected, supplied.lower()):
            return WebhookParseResult.rejected()

        payload = json.loads(raw_body)
        data = payload.get("data", {})
        return WebhookParseResult(
            signature_valid=True,
            event_id=f"{payload.get('event')}:{data.get('id')}",
            event_type=payload.get("event"),
            gateway_reference=data.get("reference"),
            status=PaymentStatus.SUCCESS
            if data.get("status") == "success"
            else PaymentStatus.FAILED,
            amount=data.get("amount"),
            currency=data.get("currency"),
            raw=payload,
        )


@pytest.fixture
def stub(monkeypatch):
    """Install a StubGateway as what get_gateway() returns."""
    holder = {}

    def install(gateway):
        holder["gateway"] = gateway
        monkeypatch.setattr(
            "kielsync.django.views.get_gateway", lambda name: gateway
        )
        return gateway

    return install


class TestMethodAndRouting:
    def test_get_is_rejected(self, client, paystack_env):
        assert client.get(webhook_url()).status_code == 405

    @pytest.mark.parametrize("method", ["put", "delete", "patch"])
    def test_other_methods_are_rejected(self, client, paystack_env, method):
        assert getattr(client, method)(webhook_url()).status_code == 405

    def test_unknown_gateway_is_404(self, client, paystack_env):
        response = client.post(
            webhook_url("stripe"), data=b"{}", content_type="application/json"
        )
        assert response.status_code == 404

    def test_no_csrf_token_is_required(self, paystack_env, stub, transaction):
        """The caller is a gateway with no session to protect; the
        signature check is what authenticates it.

        Uses a client with CSRF enforcement switched on, because the
        default test client skips CSRF entirely and would pass this test
        even if the view were not exempt.
        """
        from django.test import Client

        stub(StubGateway())
        csrf_client = Client(enforce_csrf_checks=True)
        body = paystack_body()
        response = csrf_client.post(
            webhook_url(),
            data=body,
            content_type="application/json",
            headers={"x-paystack-signature": sign(body)},
        )
        assert response.status_code == 200


class TestAuthentication:
    def test_bad_signature_returns_401(self, client, paystack_env, stub, transaction):
        stub(StubGateway())
        response = client.post(
            webhook_url(),
            data=paystack_body(),
            content_type="application/json",
            headers={"x-paystack-signature": "0" * 128},
        )
        assert response.status_code == 401

    def test_bad_signature_persists_nothing(
        self, client, paystack_env, stub, transaction
    ):
        """Storing unauthenticated payloads would let anyone who can reach
        this URL fill the table."""
        stub(StubGateway())
        client.post(
            webhook_url(),
            data=paystack_body(),
            content_type="application/json",
            headers={"x-paystack-signature": "0" * 128},
        )
        assert WebhookEvent.objects.count() == 0

    def test_missing_signature_header_returns_401(
        self, client, paystack_env, stub, transaction
    ):
        stub(StubGateway())
        response = client.post(
            webhook_url(), data=paystack_body(), content_type="application/json"
        )
        assert response.status_code == 401
        assert WebhookEvent.objects.count() == 0

    def test_rejection_does_not_call_verify(
        self, client, paystack_env, stub, transaction
    ):
        gateway = stub(StubGateway())
        client.post(
            webhook_url(),
            data=paystack_body(),
            content_type="application/json",
            headers={"x-paystack-signature": "0" * 128},
        )
        assert gateway.verify_calls == []

    def test_rejection_logs_gateway_and_source_ip(
        self, client, paystack_env, stub, transaction, caplog
    ):
        stub(StubGateway())
        with caplog.at_level("WARNING", logger="kielsync.django.views"):
            client.post(
                webhook_url(),
                data=paystack_body(),
                content_type="application/json",
                headers={"x-paystack-signature": "0" * 128},
            )
        assert "PAYSTACK" in caplog.text
        assert "127.0.0.1" in caplog.text

    def test_tampered_body_fails_authentication(
        self, client, paystack_env, stub, transaction
    ):
        """Signature computed over the honest body, sent with a forged one."""
        stub(StubGateway())
        honest = paystack_body(amount=AMOUNT)
        forged = paystack_body(amount=99_999_999)
        response = client.post(
            webhook_url(),
            data=forged,
            content_type="application/json",
            headers={"x-paystack-signature": sign(honest)},
        )
        assert response.status_code == 401
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.PENDING


class TestHappyPath:
    def _post(self, client, body=None):
        body = body or paystack_body()
        return client.post(
            webhook_url(),
            data=body,
            content_type="application/json",
            headers={"x-paystack-signature": sign(body)},
        )

    def test_returns_200(self, client, paystack_env, stub, transaction):
        stub(StubGateway())
        assert self._post(client).status_code == 200

    def test_stores_the_event(self, client, paystack_env, stub, transaction):
        stub(StubGateway())
        self._post(client)
        event = WebhookEvent.objects.get()
        assert event.gateway == "PAYSTACK"
        assert event.signature_valid is True
        assert event.processed is True
        assert event.processed_at is not None
        assert event.gateway_reference == REFERENCE

    def test_resolves_the_transaction(self, client, paystack_env, stub, transaction):
        stub(StubGateway())
        self._post(client)
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.SUCCESS

    def test_calls_verify_independently(
        self, client, paystack_env, stub, transaction
    ):
        """The webhook is a notification; verify() is the truth."""
        gateway = stub(StubGateway())
        self._post(client)
        assert gateway.verify_calls == [REFERENCE]

    def test_stores_the_verification_body_on_the_attempt(
        self, client, paystack_env, stub, transaction
    ):
        """Flutterwave's numeric transaction id only ever arrives this way."""
        stub(StubGateway())
        self._post(client)
        attempt = PaymentAttempt.objects.get(gateway_reference=REFERENCE)
        assert attempt.raw_response == {"id": 302961}


class TestIdempotence:
    def _post(self, client, body=None):
        body = body or paystack_body()
        return client.post(
            webhook_url(),
            data=body,
            content_type="application/json",
            headers={"x-paystack-signature": sign(body)},
        )

    def test_identical_payload_twice_creates_one_event(
        self, client, paystack_env, stub, transaction
    ):
        stub(StubGateway())
        assert self._post(client).status_code == 200
        assert self._post(client).status_code == 200
        assert WebhookEvent.objects.count() == 1

    def test_identical_payload_twice_causes_one_transition(
        self, client, paystack_env, stub, transaction
    ):
        stub(StubGateway())
        self._post(client)
        transaction.refresh_from_db()
        first_updated = transaction.updated_at

        self._post(client)
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.SUCCESS
        assert transaction.updated_at == first_updated

    def test_the_duplicate_does_not_call_verify_again(
        self, client, paystack_env, stub, transaction
    ):
        """Short-circuiting a processed duplicate saves an API call and,
        more importantly, cannot re-resolve."""
        gateway = stub(StubGateway())
        self._post(client)
        self._post(client)
        assert gateway.verify_calls == [REFERENCE]

    def test_ten_redeliveries_are_harmless(
        self, client, paystack_env, stub, transaction
    ):
        stub(StubGateway())
        for _ in range(10):
            assert self._post(client).status_code == 200
        assert WebhookEvent.objects.count() == 1
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.SUCCESS

    def test_a_distinct_event_is_not_deduplicated(
        self, client, paystack_env, stub, transaction
    ):
        stub(StubGateway())
        self._post(client)
        self._post(client, paystack_body(event_id=999999))
        assert WebhookEvent.objects.count() == 2


class TestVerifyIsTheSourceOfTruth:
    def test_payload_claiming_success_loses_to_a_mismatching_verify(
        self, client, paystack_env, stub, transaction
    ):
        """The defining test for this endpoint. The payload authenticates
        and claims a successful payment; verify() reports a different
        amount. The transaction must not be marked successful."""
        stub(
            StubGateway(
                verification=VerificationResult(
                    gateway_reference=REFERENCE,
                    status=PaymentStatus.SUCCESS,
                    amount=1,
                    currency="NGN",
                    raw={},
                )
            )
        )
        body = paystack_body(amount=AMOUNT)
        response = client.post(
            webhook_url(),
            data=body,
            content_type="application/json",
            headers={"x-paystack-signature": sign(body)},
        )

        assert response.status_code == 200
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.PENDING
        assert transaction.reconciliation_status == (
            Transaction.ReconciliationStatus.MISMATCHED
        )

    def test_payload_claiming_success_loses_to_a_failed_verify(
        self, client, paystack_env, stub, transaction
    ):
        stub(
            StubGateway(
                verification=VerificationResult(
                    gateway_reference=REFERENCE,
                    status=PaymentStatus.FAILED,
                    amount=AMOUNT,
                    currency="NGN",
                    raw={},
                )
            )
        )
        body = paystack_body(amount=AMOUNT)
        client.post(
            webhook_url(),
            data=body,
            content_type="application/json",
            headers={"x-paystack-signature": sign(body)},
        )
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.FAILED


class TestFailureHandling:
    def _post(self, client):
        body = paystack_body()
        return client.post(
            webhook_url(),
            data=body,
            content_type="application/json",
            headers={"x-paystack-signature": sign(body)},
        )

    def test_a_gateway_error_still_returns_200(
        self, client, paystack_env, stub, transaction
    ):
        """Returning non-200 asks the gateway to send it again, and a
        request that fails deterministically becomes a redelivery storm."""
        stub(StubGateway(verify_error=RetryableGatewayError("gateway down")))
        assert self._post(client).status_code == 200

    def test_a_gateway_error_leaves_the_event_for_the_sweeper(
        self, client, paystack_env, stub, transaction
    ):
        stub(StubGateway(verify_error=RetryableGatewayError("gateway down")))
        self._post(client)
        event = WebhookEvent.objects.get()
        assert event.processed is False
        assert event.gateway_reference == REFERENCE
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.PENDING

    def test_an_unprocessed_duplicate_is_retried_rather_than_short_circuited(
        self, client, paystack_env, stub, transaction, monkeypatch
    ):
        """Deduplication skips only events already *processed*. One that
        failed must be retried when the gateway redelivers it."""
        stub(StubGateway(verify_error=RetryableGatewayError("down")))
        self._post(client)
        assert WebhookEvent.objects.get().processed is False

        working = StubGateway()
        monkeypatch.setattr(
            "kielsync.django.views.get_gateway", lambda name: working
        )
        self._post(client)

        assert WebhookEvent.objects.count() == 1
        assert WebhookEvent.objects.get().processed is True
        transaction.refresh_from_db()
        assert transaction.status == Transaction.Status.SUCCESS

    def test_an_unknown_reference_stores_the_event_unprocessed(
        self, client, paystack_env, stub
    ):
        """No attempt row: a webhook for a payment this install did not
        start. Worth keeping, not worth acting on."""
        stub(StubGateway())
        body = paystack_body()
        response = client.post(
            webhook_url(),
            data=body,
            content_type="application/json",
            headers={"x-paystack-signature": sign(body)},
        )
        assert response.status_code == 200
        assert WebhookEvent.objects.get().processed is False

    def test_an_event_without_an_id_is_refused(
        self, client, paystack_env, stub, transaction, monkeypatch
    ):
        """Nothing stable to deduplicate on means every redelivery would
        create a row, which is the unbounded write this endpoint avoids."""

        class NoIdGateway(StubGateway):
            def parse_webhook(self, raw_body, headers):
                result = super().parse_webhook(raw_body, headers)
                if not result.signature_valid:
                    return result
                return WebhookParseResult(
                    signature_valid=True,
                    event_id=None,
                    gateway_reference=REFERENCE,
                    raw=result.raw,
                )

        stub(NoIdGateway())
        assert self._post(client).status_code == 400
        assert WebhookEvent.objects.count() == 0

    def test_authenticated_but_undecodable_body_is_400(
        self, client, paystack_env, stub, transaction
    ):
        from kielsync.core.errors import TerminalGatewayError

        class BadBodyGateway(StubGateway):
            def parse_webhook(self, raw_body, headers):
                raise TerminalGatewayError("not JSON")

        stub(BadBodyGateway())
        assert self._post(client).status_code == 400
        assert WebhookEvent.objects.count() == 0
