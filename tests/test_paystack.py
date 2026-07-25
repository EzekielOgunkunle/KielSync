"""Tests for the Paystack adapter.

Every request is served by an httpx.MockTransport, so no live call is
made and no real key is needed. The transport sits below the client,
which means URL construction, headers, timeouts, and JSON encoding are
all the real thing.
"""

import inspect
import json
import logging
from datetime import datetime, timezone

import httpx
import pytest

from kielsync.core.errors import RetryableGatewayError, TerminalGatewayError
from kielsync.core.gateways.base import InitializeRequest, PaymentStatus
from kielsync.core.gateways.paystack import PaystackGateway


def initialize_request(**overrides):
    fields = {
        "amount": 500000,
        "currency": "NGN",
        "email": "payer@example.com",
        "reference": "kiel_txn_0001",
    }
    fields.update(overrides)
    return InitializeRequest(**fields)


INITIALIZE_OK = {
    "status": True,
    "message": "Authorization URL created",
    "data": {
        "authorization_url": "https://checkout.paystack.com/abc123",
        "access_code": "abc123",
        "reference": "kiel_txn_0001",
    },
}

VERIFY_OK = {
    "status": True,
    "message": "Verification successful",
    "data": {
        "id": 302961,
        "status": "success",
        "reference": "kiel_txn_0001",
        "amount": 500000,
        "currency": "NGN",
        "gateway_response": "Successful",
        "paid_at": "2026-07-25T10:15:32.000Z",
        "authorization": {"bin": "408408", "last4": "4081"},
    },
}

REFUND_OK = {
    "status": True,
    "message": "Refund has been queued for processing",
    "data": {
        "status": "pending",
        "amount": 500000,
        "currency": "NGN",
        "transaction": {"id": 302961, "reference": "kiel_txn_0001"},
    },
}


class TestConstruction:
    @pytest.mark.parametrize("key", ["", "   ", None])
    def test_rejects_a_missing_secret_key(self, key):
        with pytest.raises(ValueError):
            PaystackGateway(key)

    def test_timeouts_are_explicit(self, make_gateway, responder):
        """httpx defaults to five seconds for everything, which is too short
        for a card authorisation waiting on an issuing bank."""
        gateway = make_gateway(responder(json=INITIALIZE_OK))
        timeout = gateway._client.timeout
        assert timeout.connect == 5.0
        assert timeout.read == 30.0

    def test_tls_verification_cannot_be_disabled_from_outside(self):
        """There is no `verify` argument to pass False to. This client
        carries a live secret key on every request."""
        parameters = inspect.signature(PaystackGateway.__init__).parameters
        assert "verify" not in parameters

    def test_reads_no_environment_variables(self, monkeypatch):
        """Configuration is injected. kielsync.core never reads os.environ."""
        monkeypatch.setenv("KIELSYNC_PAYSTACK_SECRET_KEY", "sk_from_env")
        gateway = PaystackGateway("sk_explicit")
        try:
            assert gateway._secret_key == "sk_explicit"
        finally:
            gateway.close()

    def test_works_as_a_context_manager(self):
        with PaystackGateway("sk_test_x") as gateway:
            assert gateway.base_url == PaystackGateway.BASE_URL
        assert gateway._client.is_closed


class TestInitialize:
    def test_returns_a_populated_result(self, make_gateway, responder):
        gateway = make_gateway(responder(json=INITIALIZE_OK))
        result = gateway.initialize(initialize_request())
        assert result.gateway_reference == "kiel_txn_0001"
        assert result.authorization_url == "https://checkout.paystack.com/abc123"
        assert result.raw == INITIALIZE_OK["data"]

    def test_posts_to_the_right_endpoint(self, make_gateway, responder):
        seen = []
        gateway = make_gateway(responder(json=INITIALIZE_OK, record=seen))
        gateway.initialize(initialize_request())
        assert seen[0].method == "POST"
        assert str(seen[0].url) == "https://api.paystack.co/transaction/initialize"

    def test_sends_the_amount_as_an_unconverted_integer(
        self, make_gateway, responder
    ):
        """Paystack's amount field is already in kobo, which is the unit
        KielSync speaks. Any arithmetic here would be a bug that multiplies
        or divides a real charge by a hundred."""
        seen = []
        gateway = make_gateway(responder(json=INITIALIZE_OK, record=seen))
        gateway.initialize(initialize_request(amount=500000))
        body = json.loads(seen[0].content)
        assert body["amount"] == 500000
        assert isinstance(body["amount"], int)

    def test_authenticates_with_the_injected_key(
        self, make_gateway, responder, secret_key
    ):
        seen = []
        gateway = make_gateway(responder(json=INITIALIZE_OK, record=seen))
        gateway.initialize(initialize_request())
        assert seen[0].headers["authorization"] == f"Bearer {secret_key}"

    def test_omits_optional_fields_when_unset(self, make_gateway, responder):
        seen = []
        gateway = make_gateway(responder(json=INITIALIZE_OK, record=seen))
        gateway.initialize(initialize_request())
        body = json.loads(seen[0].content)
        assert "callback_url" not in body
        assert "metadata" not in body

    def test_forwards_callback_url_and_metadata(self, make_gateway, responder):
        seen = []
        gateway = make_gateway(responder(json=INITIALIZE_OK, record=seen))
        gateway.initialize(
            initialize_request(
                callback_url="https://merchant.test/return",
                metadata={"order_id": 42},
            )
        )
        body = json.loads(seen[0].content)
        assert body["callback_url"] == "https://merchant.test/return"
        assert body["metadata"] == {"order_id": 42}

    def test_falls_back_to_the_requested_reference(self, make_gateway, responder):
        payload = {
            "status": True,
            "data": {"authorization_url": "https://checkout.paystack.com/x"},
        }
        gateway = make_gateway(responder(json=payload))
        result = gateway.initialize(initialize_request(reference="kiel_txn_9"))
        assert result.gateway_reference == "kiel_txn_9"

    def test_missing_authorization_url_is_an_error(self, make_gateway, responder):
        gateway = make_gateway(
            responder(json={"status": True, "data": {"reference": "kiel_txn_0001"}})
        )
        with pytest.raises(TerminalGatewayError):
            gateway.initialize(initialize_request())


class TestVerify:
    def test_returns_a_populated_result(self, make_gateway, responder):
        gateway = make_gateway(responder(json=VERIFY_OK))
        result = gateway.verify("kiel_txn_0001")
        assert result.gateway_reference == "kiel_txn_0001"
        assert result.status is PaymentStatus.SUCCESS
        assert result.amount == 500000
        assert result.currency == "NGN"
        assert result.paid_at == datetime(
            2026, 7, 25, 10, 15, 32, tzinfo=timezone.utc
        )
        assert result.raw == VERIFY_OK["data"]

    def test_gets_the_right_endpoint(self, make_gateway, responder):
        seen = []
        gateway = make_gateway(responder(json=VERIFY_OK, record=seen))
        gateway.verify("kiel_txn_0001")
        assert seen[0].method == "GET"
        assert str(seen[0].url) == (
            "https://api.paystack.co/transaction/verify/kiel_txn_0001"
        )

    def test_percent_encodes_the_reference(self, make_gateway, responder):
        """A reference is caller-supplied and is interpolated into a path.
        A slash in it must not redirect the call to another endpoint."""
        seen = []
        gateway = make_gateway(responder(json=VERIFY_OK, record=seen))
        gateway.verify("../refund?x=1")
        assert str(seen[0].url) == (
            "https://api.paystack.co/transaction/verify/..%2Frefund%3Fx%3D1"
        )

    def test_reports_the_amount_paystack_gives_not_the_one_requested(
        self, make_gateway, responder
    ):
        """A short payment is a reconciliation event for the caller to
        detect, not something the adapter smooths over or raises on."""
        short = {**VERIFY_OK, "data": {**VERIFY_OK["data"], "amount": 499900}}
        gateway = make_gateway(responder(json=short))
        assert gateway.verify("kiel_txn_0001").amount == 499900

    def test_reports_the_currency_paystack_gives(self, make_gateway, responder):
        switched = {**VERIFY_OK, "data": {**VERIFY_OK["data"], "currency": "USD"}}
        gateway = make_gateway(responder(json=switched))
        assert gateway.verify("kiel_txn_0001").currency == "USD"

    @pytest.mark.parametrize(
        "paystack_status,expected",
        [
            ("success", PaymentStatus.SUCCESS),
            ("failed", PaymentStatus.FAILED),
            ("reversed", PaymentStatus.FAILED),
            ("abandoned", PaymentStatus.PENDING),
            ("ongoing", PaymentStatus.PENDING),
            ("pending", PaymentStatus.PENDING),
            ("processing", PaymentStatus.PENDING),
            ("queued", PaymentStatus.PENDING),
            ("SUCCESS", PaymentStatus.SUCCESS),
            ("  success  ", PaymentStatus.SUCCESS),
        ],
    )
    def test_status_mapping(self, make_gateway, responder, paystack_status, expected):
        payload = {
            **VERIFY_OK,
            "data": {**VERIFY_OK["data"], "status": paystack_status},
        }
        gateway = make_gateway(responder(json=payload))
        assert gateway.verify("kiel_txn_0001").status is expected

    @pytest.mark.parametrize("unknown", ["a_brand_new_state", "", None, 7])
    def test_unrecognised_statuses_become_pending(
        self, make_gateway, responder, unknown
    ):
        """Never invent "the money arrived" or "it never will"."""
        payload = {**VERIFY_OK, "data": {**VERIFY_OK["data"], "status": unknown}}
        gateway = make_gateway(responder(json=payload))
        assert gateway.verify("kiel_txn_0001").status is PaymentStatus.PENDING

    def test_a_failed_payment_is_a_successful_call(self, make_gateway, responder):
        """verify() raises only when it cannot get an answer. A decline is
        an answer."""
        payload = {
            "status": True,
            "message": "Verification successful",
            "data": {
                "status": "failed",
                "reference": "kiel_txn_0001",
                "amount": 500000,
                "currency": "NGN",
                "gateway_response": "Insufficient Funds",
            },
        }
        gateway = make_gateway(responder(json=payload))
        result = gateway.verify("kiel_txn_0001")
        assert result.status is PaymentStatus.FAILED
        assert result.raw["gateway_response"] == "Insufficient Funds"

    @pytest.mark.parametrize("paid_at", [None, "", "not a timestamp", 12345])
    def test_missing_or_junk_timestamps_become_none(
        self, make_gateway, responder, paid_at
    ):
        payload = {**VERIFY_OK, "data": {**VERIFY_OK["data"], "paid_at": paid_at}}
        gateway = make_gateway(responder(json=payload))
        assert gateway.verify("kiel_txn_0001").paid_at is None

    @pytest.mark.parametrize(
        "amount,expected", [(500000, 500000), ("500000", 500000), (None, 0), (5.5, 0)]
    )
    def test_amount_coercion_never_rounds(
        self, make_gateway, responder, amount, expected
    ):
        payload = {**VERIFY_OK, "data": {**VERIFY_OK["data"], "amount": amount}}
        gateway = make_gateway(responder(json=payload))
        assert gateway.verify("kiel_txn_0001").amount == expected


class TestRefund:
    def test_returns_a_populated_result(self, make_gateway, responder):
        gateway = make_gateway(responder(json=REFUND_OK))
        result = gateway.refund("kiel_txn_0001", amount=500000)
        assert result.gateway_reference == "kiel_txn_0001"
        assert result.status is PaymentStatus.PENDING
        assert result.amount == 500000
        assert result.raw == REFUND_OK["data"]

    def test_posts_to_the_right_endpoint(self, make_gateway, responder):
        seen = []
        gateway = make_gateway(responder(json=REFUND_OK, record=seen))
        gateway.refund("kiel_txn_0001")
        assert seen[0].method == "POST"
        assert str(seen[0].url) == "https://api.paystack.co/refund"

    def test_partial_refund_sends_the_amount(self, make_gateway, responder):
        seen = []
        gateway = make_gateway(responder(json=REFUND_OK, record=seen))
        gateway.refund("kiel_txn_0001", amount=100000)
        assert json.loads(seen[0].content) == {
            "transaction": "kiel_txn_0001",
            "amount": 100000,
        }

    def test_full_refund_omits_the_amount(self, make_gateway, responder):
        """Omitting the field is how Paystack is told "all of it". The
        adapter does not look up the original amount to fill it in."""
        seen = []
        gateway = make_gateway(responder(json=REFUND_OK, record=seen))
        gateway.refund("kiel_txn_0001")
        assert json.loads(seen[0].content) == {"transaction": "kiel_txn_0001"}

    @pytest.mark.parametrize(
        "refund_status,expected",
        [
            ("processed", PaymentStatus.SUCCESS),
            ("pending", PaymentStatus.PENDING),
            ("processing", PaymentStatus.PENDING),
            ("failed", PaymentStatus.FAILED),
            ("something_new", PaymentStatus.PENDING),
        ],
    )
    def test_refund_status_mapping(
        self, make_gateway, responder, refund_status, expected
    ):
        payload = {**REFUND_OK, "data": {**REFUND_OK["data"], "status": refund_status}}
        gateway = make_gateway(responder(json=payload))
        assert gateway.refund("kiel_txn_0001").status is expected

    def test_falls_back_to_the_requested_reference(self, make_gateway, responder):
        payload = {"status": True, "data": {"status": "pending", "amount": 1000}}
        gateway = make_gateway(responder(json=payload))
        assert gateway.refund("kiel_txn_0001").gateway_reference == "kiel_txn_0001"


class TestFailureClassification:
    @pytest.mark.parametrize(
        "exception",
        [
            httpx.ConnectTimeout("connect timed out"),
            httpx.ReadTimeout("read timed out"),
            httpx.ConnectError("connection refused"),
            httpx.PoolTimeout("pool exhausted"),
        ],
        ids=lambda exc: type(exc).__name__,
    )
    def test_transport_failures_are_retryable(
        self, make_gateway, raiser, exception
    ):
        gateway = make_gateway(raiser(exception))
        with pytest.raises(RetryableGatewayError):
            gateway.verify("kiel_txn_0001")

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_server_errors_are_retryable(self, make_gateway, responder, status):
        gateway = make_gateway(
            responder(status, json={"status": False, "message": "server error"})
        )
        with pytest.raises(RetryableGatewayError):
            gateway.verify("kiel_txn_0001")

    def test_rate_limiting_is_retryable(self, make_gateway, responder):
        gateway = make_gateway(
            responder(429, json={"status": False, "message": "Too many requests"})
        )
        with pytest.raises(RetryableGatewayError):
            gateway.verify("kiel_txn_0001")

    def test_a_declined_400_is_terminal(self, make_gateway, responder):
        payload = {
            "status": False,
            "message": "Charge attempted",
            "data": {"gateway_response": "Declined"},
        }
        gateway = make_gateway(responder(400, json=payload))
        with pytest.raises(TerminalGatewayError) as raised:
            gateway.initialize(initialize_request())
        assert raised.value.retryable is False
        assert raised.value.gateway_code == "Declined"

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_client_errors_other_than_429_are_terminal(
        self, make_gateway, responder, status
    ):
        gateway = make_gateway(
            responder(status, json={"status": False, "message": "nope"})
        )
        with pytest.raises(TerminalGatewayError):
            gateway.verify("kiel_txn_0001")

    def test_an_http_200_refusal_is_still_a_failure(self, make_gateway, responder):
        """Paystack reports application-level refusals with `status: false`
        inside an otherwise successful response."""
        gateway = make_gateway(
            responder(200, json={"status": False, "message": "Invalid key"})
        )
        with pytest.raises(TerminalGatewayError) as raised:
            gateway.verify("kiel_txn_0001")
        assert raised.value.status_code == 200

    def test_an_unknown_condition_classifies_as_terminal(
        self, make_gateway, responder
    ):
        """Default to terminal: never retry something you don't understand."""
        payload = {"status": False, "message": "Some condition invented next year"}
        gateway = make_gateway(responder(200, json=payload))
        with pytest.raises(TerminalGatewayError) as raised:
            gateway.verify("kiel_txn_0001")
        assert raised.value.retryable is False

    def test_a_non_json_body_is_classified_by_status(
        self, make_gateway, responder
    ):
        """An HTML error page usually means an edge proxy answered instead
        of Paystack, so the status code is the only usable signal."""
        gateway = make_gateway(responder(502, text="<html>Bad Gateway</html>"))
        with pytest.raises(RetryableGatewayError):
            gateway.verify("kiel_txn_0001")

        gateway = make_gateway(responder(400, text="<html>Bad Request</html>"))
        with pytest.raises(TerminalGatewayError):
            gateway.verify("kiel_txn_0001")

    def test_errors_carry_the_signals_they_were_classified_from(
        self, make_gateway, responder
    ):
        gateway = make_gateway(
            responder(503, json={"status": False, "message": "unavailable"})
        )
        with pytest.raises(RetryableGatewayError) as raised:
            gateway.verify("kiel_txn_0001")
        assert raised.value.gateway == "PAYSTACK"
        assert raised.value.status_code == 503


class TestSecretHandling:
    def test_repr_redacts_the_key(self, make_gateway, responder, secret_key):
        gateway = make_gateway(responder(json=VERIFY_OK))
        assert secret_key not in repr(gateway)
        assert "[REDACTED]" in repr(gateway)

    def test_repr_is_used_by_containers_and_f_strings(
        self, make_gateway, responder, secret_key
    ):
        gateway = make_gateway(responder(json=VERIFY_OK))
        assert secret_key not in repr([gateway])
        assert secret_key not in f"{gateway!r}"

    @pytest.mark.parametrize(
        "make_failure",
        [
            lambda: ("raise", httpx.ConnectError("refused")),
            lambda: ("respond", (503, {"status": False, "message": "down"})),
            lambda: ("respond", (400, {"status": False, "message": "Invalid key"})),
        ],
    )
    def test_no_exception_mentions_the_key(
        self, make_gateway, responder, raiser, secret_key, make_failure
    ):
        kind, payload = make_failure()
        if kind == "raise":
            gateway = make_gateway(raiser(payload))
        else:
            gateway = make_gateway(responder(payload[0], json=payload[1]))

        with pytest.raises(Exception) as raised:
            gateway.verify("kiel_txn_0001")
        assert secret_key not in str(raised.value)
        assert secret_key not in repr(raised.value)

    def test_debug_logs_redact_the_payload(
        self, make_gateway, responder, caplog, secret_key
    ):
        payload = {
            "status": True,
            "data": {
                "status": "success",
                "reference": "kiel_txn_0001",
                "amount": 500000,
                "currency": "NGN",
                "authorization": {"bin": "408408", "last4": "4081"},
            },
        }
        gateway = make_gateway(responder(json=payload))
        with caplog.at_level(logging.DEBUG, logger="kielsync.core.gateways.paystack"):
            gateway.verify("kiel_txn_0001")
        logged = caplog.text
        assert "kiel_txn_0001" in logged
        assert "408408" not in logged
        assert "4081" not in logged
        assert "[REDACTED]" in logged

    def test_debug_logs_redact_the_request_body(
        self, make_gateway, responder, caplog
    ):
        gateway = make_gateway(responder(json=INITIALIZE_OK))
        with caplog.at_level(logging.DEBUG, logger="kielsync.core.gateways.paystack"):
            gateway.initialize(
                initialize_request(metadata={"customer_token": "tok_secret"})
            )
        assert "tok_secret" not in caplog.text
