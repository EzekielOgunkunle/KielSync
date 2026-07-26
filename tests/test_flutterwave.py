"""Tests for the Flutterwave adapter.

Weighted toward the four ways Flutterwave differs from Paystack, because
those differences are where the bugs live: major-unit amounts, the two
identifiers, the weaker webhook authentication, and derived event ids.
"""

import inspect
import json
from decimal import Decimal

import httpx
import pytest

from kielsync.core.errors import RetryableGatewayError, TerminalGatewayError
from kielsync.core.gateways.base import Gateway, InitializeRequest, PaymentStatus
from kielsync.core.gateways.flutterwave import FlutterwaveGateway

SECRET = "FLWSECK_TEST-kielsync-adapter-tests"
WEBHOOK_HASH = "kielsync-test-verif-hash"


@pytest.fixture
def make_fw():
    created = []

    def factory(handler, *, secret_key=SECRET, webhook_secret_hash=WEBHOOK_HASH):
        gateway = FlutterwaveGateway(
            secret_key,
            webhook_secret_hash=webhook_secret_hash,
            transport=httpx.MockTransport(handler),
        )
        created.append(gateway)
        return gateway

    yield factory
    for gateway in created:
        gateway.close()


def envelope(data, *, status="success", message="ok"):
    return {"status": status, "message": message, "data": data}


PAYMENT_OK = envelope({"link": "https://checkout.flutterwave.com/v3/hosted/pay/abc"})

VERIFY_OK = envelope(
    {
        "id": 1234567,
        "tx_ref": "kiel_txn_0001",
        "status": "successful",
        "amount": 5000,
        "currency": "NGN",
        "created_at": "2026-07-26T10:15:32.000Z",
        "processor_response": "Approved",
    }
)


def request_of(**overrides):
    fields = {
        "amount": 500_000,
        "currency": "NGN",
        "email": "payer@example.com",
        "reference": "kiel_txn_0001",
    }
    fields.update(overrides)
    return InitializeRequest(**fields)


def responder(payload, status_code=200, record=None):
    def handler(request):
        if record is not None:
            record.append(request)
        return httpx.Response(status_code, json=payload)

    return handler


class TestProtocolAndConstruction:
    def test_satisfies_the_gateway_protocol(self, make_fw):
        assert isinstance(make_fw(responder(VERIFY_OK)), Gateway)

    @pytest.mark.parametrize("key", ["", "   ", None])
    def test_rejects_a_missing_secret_key(self, key):
        with pytest.raises(ValueError):
            FlutterwaveGateway(key)

    def test_timeouts_are_explicit(self, make_fw):
        client = make_fw(responder(VERIFY_OK))._client
        assert client.timeout.connect == 5.0
        assert client.timeout.read == 30.0

    def test_tls_verification_cannot_be_disabled_from_outside(self):
        assert "verify" not in inspect.signature(FlutterwaveGateway.__init__).parameters

    def test_reads_no_environment_variables(self, monkeypatch):
        monkeypatch.setenv("KIELSYNC_FLUTTERWAVE_SECRET_KEY", "FLWSECK-from-env")
        gateway = FlutterwaveGateway("FLWSECK-explicit")
        try:
            assert gateway._secret_key == "FLWSECK-explicit"
        finally:
            gateway.close()

    def test_repr_redacts_both_credentials(self, make_fw):
        gateway = make_fw(responder(VERIFY_OK))
        assert SECRET not in repr(gateway)
        assert WEBHOOK_HASH not in repr(gateway)
        assert "[REDACTED]" in repr(gateway)


class TestAmountUnits:
    """The highest-risk code in the library. A slip here is 100x."""

    @pytest.mark.parametrize(
        "minor,currency,expected_wire",
        [
            (500_000, "NGN", "5000.00"),
            (100, "NGN", "1.00"),
            (1, "NGN", "0.01"),
            (5000, "XOF", "5000"),
            (1, "XOF", "1"),
            (1234, "USD", "12.34"),
        ],
    )
    def test_initialize_sends_major_units(
        self, make_fw, minor, currency, expected_wire
    ):
        seen = []
        gateway = make_fw(responder(PAYMENT_OK, record=seen))
        gateway.initialize(request_of(amount=minor, currency=currency))
        assert json.loads(seen[0].content)["amount"] == expected_wire

    def test_xof_is_not_divided_by_a_hundred(self, make_fw):
        """XOF has no subunit. 5000 minor units is 5000 XOF, not 50."""
        seen = []
        gateway = make_fw(responder(PAYMENT_OK, record=seen))
        gateway.initialize(request_of(amount=5000, currency="XOF"))
        assert json.loads(seen[0].content)["amount"] == "5000"

    def test_ngn_and_xof_of_the_same_minor_amount_differ_on_the_wire(self, make_fw):
        sent = []
        for currency in ("NGN", "XOF"):
            seen = []
            gateway = make_fw(responder(PAYMENT_OK, record=seen))
            gateway.initialize(request_of(amount=5000, currency=currency))
            sent.append(json.loads(seen[0].content)["amount"])
        assert sent == ["50.00", "5000"]

    def test_amount_is_sent_as_a_string_not_a_float(self, make_fw):
        """A JSON float cannot hold every two-decimal value exactly, and
        the resulting sub-kobo drift fails reconciliation for reasons
        nobody can reproduce."""
        seen = []
        gateway = make_fw(responder(PAYMENT_OK, record=seen))
        gateway.initialize(request_of(amount=500_000, currency="NGN"))
        raw = seen[0].content.decode()
        assert '"amount": "5000.00"' in raw or '"amount":"5000.00"' in raw
        assert isinstance(json.loads(raw)["amount"], str)

    @pytest.mark.parametrize(
        "wire_amount,currency,expected_minor",
        [
            (5000, "NGN", 500_000),
            (5000.0, "NGN", 500_000),
            ("5000.00", "NGN", 500_000),
            (0.01, "NGN", 1),
            (5000, "XOF", 5000),
            (12.34, "USD", 1234),
        ],
    )
    def test_verify_converts_major_units_back(
        self, make_fw, wire_amount, currency, expected_minor
    ):
        payload = envelope(
            {**VERIFY_OK["data"], "amount": wire_amount, "currency": currency}
        )
        gateway = make_fw(responder(payload))
        assert gateway.verify("kiel_txn_0001").amount == expected_minor

    @pytest.mark.parametrize("minor", [1, 100, 5000, 500_000, 123_456_789])
    @pytest.mark.parametrize("currency", ["NGN", "XOF", "USD"])
    def test_round_trip_through_the_wire_is_lossless(
        self, make_fw, minor, currency
    ):
        """Send an amount, have the gateway echo it back, and require the
        value that comes out to equal the value that went in."""
        seen = []
        gateway = make_fw(responder(PAYMENT_OK, record=seen))
        gateway.initialize(request_of(amount=minor, currency=currency))
        echoed = json.loads(seen[0].content)["amount"]

        payload = envelope(
            {**VERIFY_OK["data"], "amount": echoed, "currency": currency}
        )
        verifier = make_fw(responder(payload))
        assert verifier.verify("kiel_txn_0001").amount == minor

    def test_unconvertible_amount_reports_none_rather_than_guessing(self, make_fw):
        """Sub-minor-unit precision from the gateway is surfaced as a
        missing amount, which fails reconciliation, rather than as a
        rounded one, which would silently pass it."""
        payload = envelope(
            {**VERIFY_OK["data"], "amount": "5000.005", "currency": "NGN"}
        )
        gateway = make_fw(responder(payload))
        assert gateway.verify("kiel_txn_0001").amount == 0

    def test_unknown_currency_on_send_is_a_terminal_error(self, make_fw):
        gateway = make_fw(responder(PAYMENT_OK))
        with pytest.raises(TerminalGatewayError):
            gateway.initialize(request_of(currency="ZZZ"))


class TestInitialize:
    def test_returns_a_populated_result(self, make_fw):
        gateway = make_fw(responder(PAYMENT_OK))
        result = gateway.initialize(request_of())
        assert result.gateway_reference == "kiel_txn_0001"
        assert result.authorization_url.startswith("https://checkout.flutterwave.com")

    def test_posts_to_the_payments_endpoint(self, make_fw):
        seen = []
        gateway = make_fw(responder(PAYMENT_OK, record=seen))
        gateway.initialize(request_of())
        assert seen[0].method == "POST"
        assert str(seen[0].url) == "https://api.flutterwave.com/v3/payments"

    def test_sends_our_reference_as_tx_ref(self, make_fw):
        seen = []
        gateway = make_fw(responder(PAYMENT_OK, record=seen))
        gateway.initialize(request_of(reference="kiel_txn_9"))
        assert json.loads(seen[0].content)["tx_ref"] == "kiel_txn_9"

    def test_authenticates_with_the_injected_key(self, make_fw):
        seen = []
        gateway = make_fw(responder(PAYMENT_OK, record=seen))
        gateway.initialize(request_of())
        assert seen[0].headers["authorization"] == f"Bearer {SECRET}"

    def test_forwards_callback_url_and_metadata(self, make_fw):
        seen = []
        gateway = make_fw(responder(PAYMENT_OK, record=seen))
        gateway.initialize(
            request_of(callback_url="https://m.test/back", metadata={"order": 7})
        )
        body = json.loads(seen[0].content)
        assert body["redirect_url"] == "https://m.test/back"
        assert body["meta"] == {"order": 7}

    def test_missing_link_is_an_error(self, make_fw):
        gateway = make_fw(responder(envelope({})))
        with pytest.raises(TerminalGatewayError):
            gateway.initialize(request_of())


class TestVerifyAndTheTwoIdentifiers:
    def test_verifies_by_reference_not_by_numeric_id(self, make_fw):
        """The sweeper only ever holds tx_ref, so this must be the
        primary path."""
        seen = []
        gateway = make_fw(responder(VERIFY_OK, record=seen))
        gateway.verify("kiel_txn_0001")
        assert seen[0].method == "GET"
        assert str(seen[0].url) == (
            "https://api.flutterwave.com/v3/transactions/"
            "verify_by_reference?tx_ref=kiel_txn_0001"
        )

    def test_percent_encodes_the_reference(self, make_fw):
        seen = []
        gateway = make_fw(responder(VERIFY_OK, record=seen))
        gateway.verify("a&b=c/d")
        assert "tx_ref=a%26b%3Dc%2Fd" in str(seen[0].url)

    def test_numeric_id_is_available_in_raw_for_the_caller_to_store(self, make_fw):
        gateway = make_fw(responder(VERIFY_OK))
        assert gateway.verify("kiel_txn_0001").raw["id"] == 1234567

    def test_returns_a_populated_result(self, make_fw):
        gateway = make_fw(responder(VERIFY_OK))
        result = gateway.verify("kiel_txn_0001")
        assert result.gateway_reference == "kiel_txn_0001"
        assert result.status is PaymentStatus.SUCCESS
        assert result.amount == 500_000
        assert result.currency == "NGN"
        assert result.paid_at is not None

    def test_reports_the_amount_flutterwave_gives(self, make_fw):
        short = envelope({**VERIFY_OK["data"], "amount": 4999})
        gateway = make_fw(responder(short))
        assert gateway.verify("kiel_txn_0001").amount == 499_900

    @pytest.mark.parametrize(
        "fw_status,expected",
        [
            ("successful", PaymentStatus.SUCCESS),
            ("success", PaymentStatus.SUCCESS),
            ("failed", PaymentStatus.FAILED),
            ("cancelled", PaymentStatus.FAILED),
            ("pending", PaymentStatus.PENDING),
            ("processing", PaymentStatus.PENDING),
            ("SUCCESSFUL", PaymentStatus.SUCCESS),
            ("  successful  ", PaymentStatus.SUCCESS),
        ],
    )
    def test_status_table(self, make_fw, fw_status, expected):
        payload = envelope({**VERIFY_OK["data"], "status": fw_status})
        gateway = make_fw(responder(payload))
        assert gateway.verify("kiel_txn_0001").status is expected

    @pytest.mark.parametrize("unknown", ["brand_new_state", "", None, 7])
    def test_unrecognised_statuses_become_pending(self, make_fw, unknown):
        payload = envelope({**VERIFY_OK["data"], "status": unknown})
        gateway = make_fw(responder(payload))
        assert gateway.verify("kiel_txn_0001").status is PaymentStatus.PENDING

    def test_a_failed_transaction_is_a_successful_call(self, make_fw):
        payload = envelope({**VERIFY_OK["data"], "status": "failed"})
        gateway = make_fw(responder(payload))
        assert gateway.verify("kiel_txn_0001").status is PaymentStatus.FAILED


class TestRefund:
    def test_resolves_the_numeric_id_then_refunds(self, make_fw):
        seen = []

        def handler(request):
            seen.append(request)
            if "verify_by_reference" in str(request.url):
                return httpx.Response(200, json=VERIFY_OK)
            return httpx.Response(
                200,
                json=envelope(
                    {"id": 77, "amount_refunded": 5000, "status": "completed",
                     "currency": "NGN"}
                ),
            )

        gateway = make_fw(handler)
        result = gateway.refund("kiel_txn_0001")
        assert "verify_by_reference" in str(seen[0].url)
        assert str(seen[1].url) == (
            "https://api.flutterwave.com/v3/transactions/1234567/refund"
        )
        assert result.status is PaymentStatus.SUCCESS
        assert result.amount == 500_000

    def test_numeric_reference_skips_the_lookup(self, make_fw):
        seen = []
        gateway = make_fw(
            responder(
                envelope({"amount_refunded": 5000, "status": "completed",
                          "currency": "NGN"}),
                record=seen,
            )
        )
        gateway.refund("1234567")
        assert len(seen) == 1
        assert str(seen[0].url).endswith("/transactions/1234567/refund")

    def test_partial_refund_converts_to_major_units(self, make_fw):
        seen = []

        def handler(request):
            seen.append(request)
            if "verify_by_reference" in str(request.url):
                return httpx.Response(200, json=VERIFY_OK)
            return httpx.Response(
                200,
                json=envelope({"amount_refunded": 1000, "status": "pending",
                               "currency": "NGN"}),
            )

        gateway = make_fw(handler)
        gateway.refund("kiel_txn_0001", amount=100_000)
        assert json.loads(seen[1].content)["amount"] == "1000.00"

    def test_full_refund_omits_the_amount(self, make_fw):
        seen = []

        def handler(request):
            seen.append(request)
            if "verify_by_reference" in str(request.url):
                return httpx.Response(200, json=VERIFY_OK)
            return httpx.Response(
                200,
                json=envelope({"amount_refunded": 5000, "status": "pending",
                               "currency": "NGN"}),
            )

        gateway = make_fw(handler)
        gateway.refund("kiel_txn_0001")
        assert "amount" not in json.loads(seen[1].content)

    @pytest.mark.parametrize(
        "fw_status,expected",
        [
            ("completed", PaymentStatus.SUCCESS),
            ("successful", PaymentStatus.SUCCESS),
            ("failed", PaymentStatus.FAILED),
            ("pending", PaymentStatus.PENDING),
            ("something_new", PaymentStatus.PENDING),
        ],
    )
    def test_refund_status_table(self, make_fw, fw_status, expected):
        gateway = make_fw(
            responder(
                envelope({"amount_refunded": 5000, "status": fw_status,
                          "currency": "NGN"})
            )
        )
        assert gateway.refund("1234567").status is expected

    def test_refund_completed_is_success_not_pending(self, make_fw):
        """The Night 2 Paystack bug, in its Flutterwave form: reading a
        refund's "completed" through the transaction table would report
        SUCCESS as PENDING, because "completed" is not a transaction word."""
        gateway = make_fw(
            responder(
                envelope({"amount_refunded": 5000, "status": "completed",
                          "currency": "NGN"})
            )
        )
        assert gateway.refund("1234567").status is PaymentStatus.SUCCESS


class TestWebhookAuthentication:
    def test_valid_hash_is_accepted(self, make_fw):
        gateway = make_fw(responder(VERIFY_OK))
        body = json.dumps(
            {"event": "charge.completed",
             "data": {"id": 1, "tx_ref": "r", "status": "successful",
                      "amount": 5000, "currency": "NGN"}}
        ).encode()
        assert gateway.parse_webhook(body, {"verif-hash": WEBHOOK_HASH}).signature_valid

    def test_header_lookup_is_case_insensitive(self, make_fw):
        gateway = make_fw(responder(VERIFY_OK))
        body = b'{"event":"charge.completed","data":{"id":1,"status":"successful"}}'
        assert gateway.parse_webhook(
            body, {"Verif-Hash": WEBHOOK_HASH}
        ).signature_valid

    @pytest.mark.parametrize(
        "headers", [{}, {"verif-hash": ""}, {"verif-hash": "wrong"},
                    {"other": WEBHOOK_HASH}]
    )
    def test_missing_or_wrong_hash_is_rejected(self, make_fw, headers):
        gateway = make_fw(responder(VERIFY_OK))
        body = b'{"event":"charge.completed","data":{"id":1,"status":"successful"}}'
        result = gateway.parse_webhook(body, headers)
        assert result.signature_valid is False

    def test_rejection_populates_nothing_from_the_body(self, make_fw):
        gateway = make_fw(responder(VERIFY_OK))
        body = json.dumps(
            {"event": "charge.completed",
             "data": {"id": 9, "tx_ref": "leaked", "status": "successful",
                      "amount": 999999, "currency": "NGN"}}
        ).encode()
        result = gateway.parse_webhook(body, {"verif-hash": "wrong"})
        assert result.gateway_reference is None
        assert result.amount is None
        assert result.event_id is None
        assert result.raw == {}

    def test_no_configured_hash_rejects_everything(self):
        """A missing credential must fail closed, not open."""
        gateway = FlutterwaveGateway(SECRET, webhook_secret_hash=None)
        try:
            body = b'{"event":"charge.completed","data":{"id":1}}'
            assert gateway.parse_webhook(body, {"verif-hash": ""}).signature_valid is False
            assert gateway.parse_webhook(body, {"verif-hash": "x"}).signature_valid is False
        finally:
            gateway.close()

    def test_the_hash_does_not_authenticate_the_body(self, make_fw):
        """Documenting the weakness in an executable form: the same
        static header validates any payload at all. This is precisely why
        the webhook handler must call verify() independently, and the
        test exists so that nobody later mistakes signature_valid=True
        for evidence about the payload's contents."""
        gateway = make_fw(responder(VERIFY_OK))
        honest = json.dumps(
            {"event": "charge.completed",
             "data": {"id": 1, "tx_ref": "r", "status": "successful",
                      "amount": 5000, "currency": "NGN"}}
        ).encode()
        forged = json.dumps(
            {"event": "charge.completed",
             "data": {"id": 1, "tx_ref": "r", "status": "successful",
                      "amount": 99_999_999, "currency": "NGN"}}
        ).encode()
        headers = {"verif-hash": WEBHOOK_HASH}
        assert gateway.parse_webhook(honest, headers).signature_valid is True
        assert gateway.parse_webhook(forged, headers).signature_valid is True

    def test_authenticated_but_malformed_body_raises(self, make_fw):
        gateway = make_fw(responder(VERIFY_OK))
        with pytest.raises(TerminalGatewayError):
            gateway.parse_webhook(b"not json", {"verif-hash": WEBHOOK_HASH})

    def test_authenticated_json_array_raises(self, make_fw):
        gateway = make_fw(responder(VERIFY_OK))
        with pytest.raises(TerminalGatewayError):
            gateway.parse_webhook(b'["nope"]', {"verif-hash": WEBHOOK_HASH})


class TestWebhookParsing:
    def _parse(self, gateway, payload):
        return gateway.parse_webhook(
            json.dumps(payload).encode(), {"verif-hash": WEBHOOK_HASH}
        )

    def test_populates_the_result(self, make_fw):
        gateway = make_fw(responder(VERIFY_OK))
        result = self._parse(
            gateway,
            {"event": "charge.completed",
             "data": {"id": 1234567, "tx_ref": "kiel_txn_0001",
                      "status": "successful", "amount": 5000, "currency": "NGN"}},
        )
        assert result.event_type == "charge.completed"
        assert result.gateway_reference == "kiel_txn_0001"
        assert result.status is PaymentStatus.SUCCESS
        assert result.amount == 500_000
        assert result.currency == "NGN"
        assert result.event_id is not None

    def test_webhook_amounts_are_converted_to_minor_units(self, make_fw):
        gateway = make_fw(responder(VERIFY_OK))
        result = self._parse(
            gateway,
            {"event": "charge.completed",
             "data": {"id": 1, "tx_ref": "r", "status": "successful",
                      "amount": 5000, "currency": "XOF"}},
        )
        assert result.amount == 5000

    def test_refund_events_do_not_use_transaction_semantics(self, make_fw):
        """Guards the Night 2 bug on the Flutterwave path."""
        gateway = make_fw(responder(VERIFY_OK))
        result = self._parse(
            gateway,
            {"event": "refund.completed",
             "data": {"id": 55, "tx_ref": "r", "status": "completed",
                      "amount": 5000, "currency": "NGN"}},
        )
        assert result.status is PaymentStatus.SUCCESS

    def test_redelivery_produces_the_same_event_id(self, make_fw):
        gateway = make_fw(responder(VERIFY_OK))
        payload = {"event": "charge.completed",
                   "data": {"id": 1234567, "tx_ref": "r", "status": "successful",
                            "amount": 5000, "currency": "NGN"}}
        assert self._parse(gateway, payload).event_id == (
            self._parse(gateway, payload).event_id
        )

    def test_a_later_distinct_event_produces_a_different_id(self, make_fw):
        gateway = make_fw(responder(VERIFY_OK))
        base = {"id": 1234567, "tx_ref": "r", "amount": 5000, "currency": "NGN"}
        pending = self._parse(
            gateway, {"event": "charge.completed", "data": {**base, "status": "pending"}}
        )
        done = self._parse(
            gateway,
            {"event": "charge.completed", "data": {**base, "status": "successful"}},
        )
        refunded = self._parse(
            gateway,
            {"event": "refund.completed", "data": {**base, "status": "completed"}},
        )
        assert len({pending.event_id, done.event_id, refunded.event_id}) == 3

    def test_missing_id_yields_no_event_id(self, make_fw):
        gateway = make_fw(responder(VERIFY_OK))
        result = self._parse(
            gateway, {"event": "charge.completed", "data": {"tx_ref": "r"}}
        )
        assert result.event_id is None
        assert result.signature_valid is True


class TestFailureClassification:
    @pytest.mark.parametrize(
        "exception",
        [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.ConnectError("c")],
        ids=lambda e: type(e).__name__,
    )
    def test_transport_failures_are_retryable(self, make_fw, exception):
        def handler(request):
            raise exception

        with pytest.raises(RetryableGatewayError):
            make_fw(handler).verify("kiel_txn_0001")

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_server_errors_are_retryable(self, make_fw, status):
        gateway = make_fw(responder(envelope(None, status="error"), status))
        with pytest.raises(RetryableGatewayError):
            gateway.verify("kiel_txn_0001")

    def test_rate_limiting_is_retryable(self, make_fw):
        gateway = make_fw(responder(envelope(None, status="error"), 429))
        with pytest.raises(RetryableGatewayError):
            gateway.verify("kiel_txn_0001")

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_client_errors_are_terminal(self, make_fw, status):
        gateway = make_fw(responder(envelope(None, status="error"), status))
        with pytest.raises(TerminalGatewayError):
            gateway.verify("kiel_txn_0001")

    def test_error_envelope_inside_http_200_is_still_a_failure(self, make_fw):
        """Flutterwave's envelope status is the string "error", which is
        truthy — a naive check would accept every failure as a success."""
        gateway = make_fw(
            responder({"status": "error", "message": "No transaction found",
                       "data": None})
        )
        with pytest.raises(TerminalGatewayError):
            gateway.verify("kiel_txn_0001")

    def test_declined_processor_response_is_terminal(self, make_fw):
        gateway = make_fw(
            responder(
                {"status": "error", "message": "declined",
                 "data": {"processor_response": "Insufficient Funds"}},
                400,
            )
        )
        with pytest.raises(TerminalGatewayError) as raised:
            gateway.initialize(request_of())
        assert raised.value.gateway_code == "Insufficient Funds"

    def test_non_json_body_is_classified_by_status(self, make_fw):
        def handler(request):
            return httpx.Response(502, text="<html>bad gateway</html>")

        with pytest.raises(RetryableGatewayError):
            make_fw(handler).verify("kiel_txn_0001")

    def test_no_exception_mentions_either_credential(self, make_fw):
        gateway = make_fw(responder(envelope(None, status="error"), 400))
        with pytest.raises(TerminalGatewayError) as raised:
            gateway.verify("kiel_txn_0001")
        assert SECRET not in str(raised.value)
        assert WEBHOOK_HASH not in str(raised.value)
