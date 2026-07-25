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


def vector_id(vector):
    return vector["name"]


@pytest.fixture
def gateway_for():
    """Build an adapter keyed on a vector's own secret, and close it after.

    parse_webhook does no I/O, so no transport is needed. The vector
    carries the key its signature was computed with, which keeps each
    file self-contained.
    """
    built = []

    def factory(vector):
        gateway = PaystackGateway(vector["secret_key"])
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

    @pytest.mark.parametrize("vector", VECTORS, ids=vector_id)
    def test_no_vector_carries_a_plausible_live_key(self, vector):
        """Conformance vectors get published. A key that looks real must
        never be one, so the fixtures use an unmistakably fake value."""
        assert vector["secret_key"].startswith("sk_test_")
        assert "kielsync" in vector["secret_key"]


@pytest.mark.parametrize("vector", VECTORS, ids=vector_id)
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
