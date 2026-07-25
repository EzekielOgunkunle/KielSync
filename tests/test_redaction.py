"""Tests for the gateway payload redactor.

These assert two things that pull in opposite directions: that every
sensitive field is masked no matter how deeply it is nested, and that the
non-sensitive fields survive, because a redactor that masks everything is
safe and useless.
"""

import pytest

from kielsync.core.logging import REDACTED, is_sensitive_key, redact


class TestSensitiveKeyDetection:
    @pytest.mark.parametrize(
        "key",
        [
            "authorization",
            "Authorization",
            "AUTHORIZATION",
            "card",
            "card_number",
            "bin",
            "last4",
            "account_number",
            "Account-Number",
            "account number",
            "signature",
            "x-paystack-signature",
            "X-Paystack-Signature",
            "cvv",
            "pin",
            "exp_month",
            "exp_year",
            "secret_key",
            "SECRET_KEY",
            "api_key",
            "publicKey".lower(),
            "access_token",
            "refresh_token",
            "bearer_token",
            "password",
            "client_secret",
            "authorization_code",
        ],
    )
    def test_sensitive_keys_are_detected(self, key):
        assert is_sensitive_key(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            "status",
            "amount",
            "currency",
            "reference",
            "email",
            "event",
            "paid_at",
            "gateway_response",
            "channel",
            "message",
            "data",
            "id",
        ],
    )
    def test_useful_keys_survive(self, key):
        assert is_sensitive_key(key) is False

    def test_idempotency_key_is_over_matched_by_design(self):
        """The "key" substring catches benign names too. A reference lost
        from a log is recoverable from the database; a leaked secret is not."""
        assert is_sensitive_key("idempotency_key") is True


class TestRedact:
    def test_masks_top_level_secrets(self):
        result = redact({"secret_key": "sk_live_abc", "status": True})
        assert result["secret_key"] == REDACTED
        assert result["status"] is True

    def test_masks_nested_dicts(self):
        payload = {
            "data": {
                "reference": "ref_1",
                "customer": {"email": "a@b.com", "auth_token": "tok_live"},
            }
        }
        result = redact(payload)
        assert result["data"]["reference"] == "ref_1"
        assert result["data"]["customer"]["email"] == "a@b.com"
        assert result["data"]["customer"]["auth_token"] == REDACTED

    def test_masks_a_sensitive_subtree_whole(self):
        """The authorization block is masked entirely rather than field by
        field, so a card attribute Paystack adds later is withheld from the
        first delivery instead of leaking until someone notices."""
        payload = {
            "data": {
                "authorization": {
                    "bin": "408408",
                    "last4": "4081",
                    "some_future_field": "whatever it turns out to be",
                }
            }
        }
        assert redact(payload)["data"]["authorization"] == REDACTED

    def test_masks_inside_lists_of_dicts(self):
        payload = {"log": [{"signature": "abc", "step": 1}, {"step": 2}]}
        result = redact(payload)
        assert result["log"][0]["signature"] == REDACTED
        assert result["log"][0]["step"] == 1
        assert result["log"][1] == {"step": 2}

    def test_masks_through_deeply_nested_lists(self):
        payload = {"a": [[{"api_key": "k"}]], "b": [{"c": [{"cvv": "123"}]}]}
        result = redact(payload)
        assert result["a"][0][0]["api_key"] == REDACTED
        assert result["b"][0]["c"][0]["cvv"] == REDACTED

    def test_strings_are_not_treated_as_sequences(self):
        assert redact({"reference": "ref_1"})["reference"] == "ref_1"

    def test_non_string_values_under_sensitive_keys_are_masked(self):
        payload = {"card": ["4081", "1234"], "bin": 408408, "signature": None}
        result = redact(payload)
        assert result["card"] == REDACTED
        assert result["bin"] == REDACTED
        assert result["signature"] == REDACTED

    def test_input_is_never_mutated(self):
        """Callers pass the same decoded body they are about to persist."""
        payload = {"data": {"authorization": {"bin": "408408"}}, "secret": "s"}
        redact(payload)
        assert payload["data"]["authorization"] == {"bin": "408408"}
        assert payload["secret"] == "s"

    def test_empty_payload(self):
        assert redact({}) == {}

    def test_no_secret_survives_a_realistic_paystack_response(self):
        payload = {
            "status": True,
            "message": "Verification successful",
            "data": {
                "id": 302961,
                "status": "success",
                "reference": "kiel_txn_0001",
                "amount": 500000,
                "currency": "NGN",
                "authorization": {
                    "authorization_code": "AUTH_x1",
                    "bin": "408408",
                    "last4": "4081",
                    "exp_month": "12",
                    "exp_year": "2030",
                    "account_number": "0123456789",
                },
                "customer": {"email": "payer@example.com"},
                "meta": {"secret_key": "sk_live_leak", "signature": "deadbeef"},
            },
        }
        flattened = repr(redact(payload))
        for secret in (
            "AUTH_x1",
            "408408",
            "4081",
            "0123456789",
            "sk_live_leak",
            "deadbeef",
        ):
            assert secret not in flattened, secret
        # The fields an operator actually needs are still there.
        assert "kiel_txn_0001" in flattened
        assert "Verification successful" in flattened
        assert "500000" in flattened
