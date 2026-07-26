"""Tests for the Django layer's configuration reading and gateway factory."""

import pytest

from kielsync.core.exceptions import ConfigurationError
from kielsync.core.gateways.base import Gateway
from kielsync.core.gateways.flutterwave import FlutterwaveGateway
from kielsync.core.gateways.paystack import PaystackGateway
from kielsync.django.settings import (
    FLUTTERWAVE_SECRET_KEY_ENV,
    FLUTTERWAVE_WEBHOOK_HASH_ENV,
    PAYSTACK_SECRET_KEY_ENV,
    get_gateway,
    get_paystack_secret_key,
)


@pytest.fixture
def paystack_key(monkeypatch):
    key = "sk_test_kielsync_factory"
    monkeypatch.setenv(PAYSTACK_SECRET_KEY_ENV, key)
    return key


@pytest.fixture
def flutterwave_env(monkeypatch):
    monkeypatch.setenv(FLUTTERWAVE_SECRET_KEY_ENV, "FLWSECK_TEST-kielsync-factory")
    monkeypatch.setenv(FLUTTERWAVE_WEBHOOK_HASH_ENV, "kielsync-test-verif-hash")
    return "FLWSECK_TEST-kielsync-factory", "kielsync-test-verif-hash"


class TestFlutterwaveConstruction:
    def test_builds_a_configured_flutterwave_adapter(self, flutterwave_env):
        secret, webhook_hash = flutterwave_env
        gateway = get_gateway("FLUTTERWAVE")
        try:
            assert isinstance(gateway, FlutterwaveGateway)
            assert isinstance(gateway, Gateway)
            assert gateway._secret_key == secret
            assert gateway._webhook_secret_hash == webhook_hash
        finally:
            gateway.close()

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv(FLUTTERWAVE_SECRET_KEY_ENV, raising=False)
        with pytest.raises(ConfigurationError):
            get_gateway("FLUTTERWAVE")

    def test_webhook_hash_is_optional_but_api_key_is_not(self, monkeypatch):
        """An integration may take Flutterwave payments without receiving
        its webhooks. A missing hash makes the adapter reject every
        webhook rather than accept unauthenticated ones."""
        monkeypatch.setenv(FLUTTERWAVE_SECRET_KEY_ENV, "FLWSECK_TEST-x")
        monkeypatch.delenv(FLUTTERWAVE_WEBHOOK_HASH_ENV, raising=False)
        gateway = get_gateway("FLUTTERWAVE")
        try:
            assert gateway._webhook_secret_hash is None
            assert gateway.parse_webhook(
                b'{"event":"charge.completed"}', {"verif-hash": "anything"}
            ).signature_valid is False
        finally:
            gateway.close()

    def test_neither_credential_appears_in_the_repr(self, flutterwave_env):
        secret, webhook_hash = flutterwave_env
        gateway = get_gateway("FLUTTERWAVE")
        try:
            assert secret not in repr(gateway)
            assert webhook_hash not in repr(gateway)
        finally:
            gateway.close()


class TestSecretKeyReading:
    def test_reads_the_key_from_the_environment(self, paystack_key):
        assert get_paystack_secret_key() == paystack_key

    def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv(PAYSTACK_SECRET_KEY_ENV, raising=False)
        with pytest.raises(ConfigurationError):
            get_paystack_secret_key()

    @pytest.mark.parametrize("blank", ["", "   ", "\n"])
    def test_blank_key_is_treated_as_missing(self, monkeypatch, blank):
        monkeypatch.setenv(PAYSTACK_SECRET_KEY_ENV, blank)
        with pytest.raises(ConfigurationError):
            get_paystack_secret_key()

    def test_there_is_no_fallback_default(self, monkeypatch):
        """A placeholder default would surface in production as confusing
        401s, and a test-mode default as payments that never settle."""
        monkeypatch.delenv(PAYSTACK_SECRET_KEY_ENV, raising=False)
        with pytest.raises(ConfigurationError):
            get_gateway("PAYSTACK")

    def test_the_error_names_the_variable_but_not_its_value(self, monkeypatch):
        monkeypatch.setenv(PAYSTACK_SECRET_KEY_ENV, "   ")
        with pytest.raises(ConfigurationError) as raised:
            get_paystack_secret_key()
        assert PAYSTACK_SECRET_KEY_ENV in str(raised.value)


class TestGetGateway:
    def test_builds_a_configured_paystack_adapter(self, paystack_key):
        gateway = get_gateway("PAYSTACK")
        try:
            assert isinstance(gateway, PaystackGateway)
            assert isinstance(gateway, Gateway)
            assert gateway._secret_key == paystack_key
        finally:
            gateway.close()

    @pytest.mark.parametrize("name", ["paystack", "PayStack", "  PAYSTACK  "])
    def test_the_name_is_matched_case_and_whitespace_insensitively(
        self, paystack_key, name
    ):
        """The name may come straight from a stored PaymentAttempt row."""
        gateway = get_gateway(name)
        try:
            assert isinstance(gateway, PaystackGateway)
        finally:
            gateway.close()

    @pytest.mark.parametrize("name", ["stripe", "", "mpesa"])
    def test_an_unregistered_gateway_raises(self, paystack_key, name):
        with pytest.raises(ConfigurationError):
            get_gateway(name)

    def test_the_error_lists_the_gateways_that_do_exist(self, paystack_key):
        with pytest.raises(ConfigurationError) as raised:
            get_gateway("stripe")
        assert "PAYSTACK" in str(raised.value)

    def test_each_call_builds_a_fresh_adapter(self, paystack_key):
        """No caching, so a rotated key takes effect without a restart."""
        first = get_gateway("PAYSTACK")
        second = get_gateway("PAYSTACK")
        try:
            assert first is not second
        finally:
            first.close()
            second.close()

    def test_a_rotated_key_is_picked_up_immediately(self, monkeypatch):
        monkeypatch.setenv(PAYSTACK_SECRET_KEY_ENV, "sk_test_kielsync_before")
        before = get_gateway("PAYSTACK")
        monkeypatch.setenv(PAYSTACK_SECRET_KEY_ENV, "sk_test_kielsync_after")
        after = get_gateway("PAYSTACK")
        try:
            assert before._secret_key == "sk_test_kielsync_before"
            assert after._secret_key == "sk_test_kielsync_after"
        finally:
            before.close()
            after.close()

    def test_the_built_adapter_does_not_leak_the_key_in_its_repr(self, paystack_key):
        gateway = get_gateway("PAYSTACK")
        try:
            assert paystack_key not in repr(gateway)
        finally:
            gateway.close()
