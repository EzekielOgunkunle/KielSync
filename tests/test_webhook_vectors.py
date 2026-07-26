"""Webhook parsing, driven by the conformance vectors in tests/vectors/.

The vectors are standalone JSON data files rather than fixtures embedded
in this module, because they are intended to outlive it: they describe
what any correct implementation of KielSync's webhook contract must do,
and they will be published for other implementations to run against.
Keeping the expectations in the data means a second implementation, in a
different language, can be checked without porting this file.

This module is the loader and the assertions. Anything specific to a
particular payload belongs in the vector, not here.
"""

import hashlib
import hmac
import json
import pathlib

import pytest

from kielsync.core.errors import RetryableGatewayError, TerminalGatewayError
from kielsync.core.gateways.base import PaymentStatus, WebhookParseResult
from kielsync.core.gateways.flutterwave import FlutterwaveGateway
from kielsync.core.gateways.paystack import PaystackGateway

VECTOR_DIR = pathlib.Path(__file__).parent / "vectors"

RESULT_FIELDS = (
    "signature_valid",
    "event_id",
    "event_type",
    "gateway_reference",
    "status",
    "amount",
    "currency",
)

EXCEPTIONS = {
    "TerminalGatewayError": TerminalGatewayError,
    "RetryableGatewayError": RetryableGatewayError,
}


def load_vectors():
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(VECTOR_DIR.glob("*.json"))
    ]


VECTORS = load_vectors()
PAYSTACK_VECTORS = [v for v in VECTORS if v["gateway"] == "paystack"]
FLUTTERWAVE_VECTORS = [v for v in VECTORS if v["gateway"] == "flutterwave"]


def vector_id(vector):
    return vector["name"]


def _build_gateway(vector):
    """Construct the adapter a vector is addressed to.

    ``parse_webhook`` does no I/O, so no transport is needed. Each vector
    carries the credentials its headers were built against, which is what
    keeps the files self-contained and portable to another
    implementation.
    """
    if vector["gateway"] == "paystack":
        return PaystackGateway(vector["secret_key"])
    if vector["gateway"] == "flutterwave":
        return FlutterwaveGateway(
            vector["secret_key"],
            webhook_secret_hash=vector["webhook_secret_hash"],
        )
    raise AssertionError(f"no adapter for gateway {vector['gateway']!r}")


@pytest.fixture
def gateway_for():
    built = []

    def factory(vector):
        gateway = _build_gateway(vector)
        built.append(gateway)
        return gateway

    yield factory

    for gateway in built:
        gateway.close()


class TestVectorFilesThemselves:
    def test_vectors_exist(self):
        """Guard against a glob that silently matches nothing."""
        assert len(VECTORS) >= 10

    def test_vector_names_are_unique(self):
        names = [vector["name"] for vector in VECTORS]
        assert len(names) == len(set(names))

    @pytest.mark.parametrize("vector", VECTORS, ids=vector_id)
    def test_vector_is_well_formed(self, vector):
        for field in ("name", "description", "gateway", "secret_key", "raw_body"):
            assert vector.get(field), f"{vector.get('name')} is missing {field}"
        assert isinstance(vector["headers"], dict)
        assert isinstance(vector["expected"], dict)
        assert vector["description"].strip()

    @pytest.mark.parametrize("vector", VECTORS, ids=vector_id)
    def test_expected_outcome_is_either_a_result_or_an_exception(self, vector):
        expected = vector["expected"]
        if "raises" in expected:
            assert expected["raises"] in EXCEPTIONS
        else:
            assert set(expected) == set(RESULT_FIELDS)

    def test_both_gateways_are_represented(self):
        assert len(PAYSTACK_VECTORS) >= 10
        assert len(FLUTTERWAVE_VECTORS) >= 10

    @pytest.mark.parametrize("vector", VECTORS, ids=vector_id)
    def test_no_vector_carries_a_plausible_live_key(self, vector):
        """Conformance vectors get published. A key that looks real must
        never be one, so the fixtures use unmistakably fake values in
        each gateway's own test-key format."""
        secret = vector["secret_key"]
        assert secret.startswith(("sk_test_", "FLWSECK_TEST-"))
        assert "kielsync" in secret
        if vector["gateway"] == "flutterwave":
            assert "kielsync" in vector["webhook_secret_hash"]

    @pytest.mark.parametrize("vector", FLUTTERWAVE_VECTORS, ids=vector_id)
    def test_flutterwave_vectors_declare_a_webhook_hash(self, vector):
        """The verif-hash secret is a different value from the API key,
        and the vector has to carry both to be self-contained."""
        assert vector["webhook_secret_hash"]
        assert vector["webhook_secret_hash"] != vector["secret_key"]


@pytest.mark.parametrize("vector", PAYSTACK_VECTORS, ids=vector_id)
def test_vector_signature_matches_its_declared_expectation(vector):
    """Check the fixtures against a signature computed here, independently
    of the adapter. A vector whose signature silently stopped matching its
    body would otherwise turn into a test that asserts nothing."""
    body = vector["raw_body"].encode("utf-8")
    expected_digest = hmac.new(
        vector["secret_key"].encode("utf-8"), body, hashlib.sha512
    ).hexdigest()

    supplied = None
    for name, value in vector["headers"].items():
        if name.lower() == "x-paystack-signature":
            supplied = value

    should_verify = vector["expected"].get("signature_valid") is True or (
        "raises" in vector["expected"]
    )
    if should_verify:
        assert supplied is not None, "a vector expected to verify needs a signature"
        assert supplied.lower() == expected_digest
    else:
        assert supplied is None or supplied.lower() != expected_digest


@pytest.mark.parametrize("vector", FLUTTERWAVE_VECTORS, ids=vector_id)
def test_flutterwave_vector_header_matches_its_declared_expectation(vector):
    """The Flutterwave equivalent of the signature check above, done
    independently of the adapter so a vector cannot drift into asserting
    nothing.

    Note how much simpler this is than the Paystack version, and why that
    is bad news rather than good: there is no digest to recompute because
    the header is just the shared secret, byte for byte, on every
    delivery. A vector verifies exactly when its header equals the
    configured hash — the body is not an input at all.
    """
    supplied = None
    for name, value in vector["headers"].items():
        if name.lower() == "verif-hash":
            supplied = value

    should_verify = vector["expected"].get("signature_valid") is True or (
        "raises" in vector["expected"]
    )
    if should_verify:
        assert supplied == vector["webhook_secret_hash"]
    else:
        assert supplied != vector["webhook_secret_hash"]


def test_the_tampered_flutterwave_vector_authenticates_on_purpose():
    """A vector that would look like a mistake without this assertion.

    flutterwave_tampered_body_still_authenticates carries a forged amount
    and expects signature_valid=True, because verif-hash says nothing
    about the body. It is in the set deliberately, as the executable
    statement of why the webhook handler cannot trust a payload even
    after authentication succeeds.
    """
    tampered = [
        v
        for v in FLUTTERWAVE_VECTORS
        if v["name"] == "flutterwave_tampered_body_still_authenticates"
    ]
    assert len(tampered) == 1
    vector = tampered[0]
    assert vector["expected"]["signature_valid"] is True
    assert vector["expected"]["amount"] == 9_999_999_900


@pytest.mark.parametrize("vector", VECTORS, ids=vector_id)
def test_parse_webhook_matches_the_vector(vector, gateway_for):
    gateway = gateway_for(vector)
    raw_body = vector["raw_body"].encode("utf-8")
    headers = vector["headers"]
    expected = vector["expected"]

    if "raises" in expected:
        with pytest.raises(EXCEPTIONS[expected["raises"]]):
            gateway.parse_webhook(raw_body, headers)
        return

    result = gateway.parse_webhook(raw_body, headers)

    assert result.signature_valid is expected["signature_valid"]
    assert result.event_id == expected["event_id"]
    assert result.event_type == expected["event_type"]
    assert result.gateway_reference == expected["gateway_reference"]
    assert result.amount == expected["amount"]
    assert result.currency == expected["currency"]

    if expected["status"] is None:
        assert result.status is None
    else:
        assert result.status is PaymentStatus[expected["status"]]


@pytest.mark.parametrize("vector", VECTORS, ids=vector_id)
def test_rejected_vectors_leak_nothing_from_the_body(vector, gateway_for):
    """The central webhook guarantee: when the signature does not verify,
    no value read from the payload reaches the caller. Not the amount, not
    the reference, not the raw body."""
    if vector["expected"].get("signature_valid") is not False:
        pytest.skip("vector is not a rejection case")

    gateway = gateway_for(vector)
    result = gateway.parse_webhook(vector["raw_body"].encode("utf-8"), vector["headers"])

    assert result == WebhookParseResult.rejected()
    assert result.raw == {}


class TestSignatureVerificationProperties:
    """Properties of the signature check that no single vector expresses."""

    def test_a_body_that_verifies_under_one_key_fails_under_another(self):
        body = b'{"event":"charge.success","data":{"id":1,"status":"success"}}'
        real = PaystackGateway("sk_test_kielsync_real")
        other = PaystackGateway("sk_test_kielsync_other")
        try:
            signature = hmac.new(
                b"sk_test_kielsync_real", body, hashlib.sha512
            ).hexdigest()
            headers = {"x-paystack-signature": signature}
            assert real.parse_webhook(body, headers).signature_valid is True
            assert other.parse_webhook(body, headers).signature_valid is False
        finally:
            real.close()
            other.close()

    def test_the_signature_covers_byte_order_not_json_meaning(self):
        """Re-serialising a payload before verification breaks the digest,
        which is why raw bytes must reach parse_webhook untouched."""
        key = "sk_test_kielsync_bytes"
        original = b'{"event":"charge.success","data":{"amount":100,"id":1}}'
        reordered = json.dumps(json.loads(original)).encode()
        assert json.loads(original) == json.loads(reordered)
        assert original != reordered

        signature = hmac.new(key.encode(), original, hashlib.sha512).hexdigest()
        headers = {"x-paystack-signature": signature}
        gateway = PaystackGateway(key)
        try:
            assert gateway.parse_webhook(original, headers).signature_valid is True
            assert gateway.parse_webhook(reordered, headers).signature_valid is False
        finally:
            gateway.close()

    def test_an_empty_body_still_requires_a_valid_signature(self):
        gateway = PaystackGateway("sk_test_kielsync_empty")
        try:
            assert gateway.parse_webhook(b"", {}).signature_valid is False
            digest = hmac.new(
                b"sk_test_kielsync_empty", b"", hashlib.sha512
            ).hexdigest()
            with pytest.raises(TerminalGatewayError):
                gateway.parse_webhook(b"", {"x-paystack-signature": digest})
        finally:
            gateway.close()
