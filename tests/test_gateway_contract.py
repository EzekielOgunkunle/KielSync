"""Tests for the gateway dataclasses and the Gateway protocol itself."""

import dataclasses
from datetime import datetime

import pytest

from kielsync.core.gateways.base import (
    Gateway,
    InitializeRequest,
    InitializeResult,
    PaymentStatus,
    RefundResult,
    VerificationResult,
    WebhookParseResult,
)
from kielsync.core.gateways.paystack import PaystackGateway

RESULT_TYPES = [
    InitializeRequest,
    InitializeResult,
    VerificationResult,
    RefundResult,
    WebhookParseResult,
]


@pytest.mark.parametrize("cls", RESULT_TYPES, ids=lambda c: c.__name__)
def test_all_boundary_types_are_frozen_dataclasses(cls):
    """Results crossing the adapter boundary must not be edited in place —
    they are the record of what a gateway said."""
    assert dataclasses.is_dataclass(cls)
    assert cls.__dataclass_params__.frozen is True


def test_initialize_request_rejects_assignment():
    request = InitializeRequest(
        amount=500000, currency="NGN", email="a@b.com", reference="ref_1"
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.amount = 1


class TestInitializeRequestValidation:
    def test_accepts_a_positive_integer_amount(self):
        request = InitializeRequest(
            amount=1, currency="NGN", email="a@b.com", reference="ref_1"
        )
        assert request.amount == 1

    @pytest.mark.parametrize("amount", [0, -1, -500000])
    def test_rejects_non_positive_amounts(self, amount):
        with pytest.raises(ValueError):
            InitializeRequest(
                amount=amount, currency="NGN", email="a@b.com", reference="ref_1"
            )

    @pytest.mark.parametrize("amount", [1.0, 5000.50, "5000", None])
    def test_rejects_non_integer_amounts(self, amount):
        """Money is never a float at this boundary. A float amount is the
        bug that turns 50.00 into 5000 or 0.5 depending on who rounds it."""
        with pytest.raises(TypeError):
            InitializeRequest(
                amount=amount, currency="NGN", email="a@b.com", reference="ref_1"
            )

    def test_rejects_bool_amount(self):
        """bool is an int subclass, so True would otherwise pass as 1."""
        with pytest.raises(TypeError):
            InitializeRequest(
                amount=True, currency="NGN", email="a@b.com", reference="ref_1"
            )

    def test_rejects_empty_currency_and_reference(self):
        with pytest.raises(ValueError):
            InitializeRequest(
                amount=100, currency="", email="a@b.com", reference="ref_1"
            )
        with pytest.raises(ValueError):
            InitializeRequest(
                amount=100, currency="NGN", email="a@b.com", reference=""
            )

    def test_optional_fields_default_empty(self):
        request = InitializeRequest(
            amount=100, currency="NGN", email="a@b.com", reference="ref_1"
        )
        assert request.callback_url is None
        assert request.metadata == {}

    def test_metadata_defaults_are_not_shared_between_instances(self):
        first = InitializeRequest(
            amount=100, currency="NGN", email="a@b.com", reference="ref_1"
        )
        second = InitializeRequest(
            amount=100, currency="NGN", email="a@b.com", reference="ref_2"
        )
        assert first.metadata is not second.metadata


class TestWebhookParseResult:
    def test_rejected_carries_nothing_but_the_verdict(self):
        result = WebhookParseResult.rejected()
        assert result.signature_valid is False
        assert result.event_id is None
        assert result.event_type is None
        assert result.gateway_reference is None
        assert result.status is None
        assert result.amount is None
        assert result.currency is None
        assert result.raw == {}

    def test_rejected_results_do_not_share_a_raw_mapping(self):
        assert WebhookParseResult.rejected().raw is not WebhookParseResult.rejected().raw


class TestPaymentStatus:
    def test_vocabulary_is_exactly_three_values(self):
        assert {member.value for member in PaymentStatus} == {
            "SUCCESS",
            "FAILED",
            "PENDING",
        }

    def test_members_compare_equal_to_their_string_value(self):
        """StrEnum keeps the results JSON-serialisable and comparable
        against the status strings stored on a PaymentAttempt."""
        assert PaymentStatus.SUCCESS == "SUCCESS"


class TestGatewayProtocol:
    def test_paystack_adapter_satisfies_the_protocol(self, make_gateway, responder):
        gateway = make_gateway(responder(json={"status": True, "data": {}}))
        assert isinstance(gateway, Gateway)

    def test_a_bare_test_double_satisfies_the_protocol(self):
        """The protocol is structural: a fake need not import or subclass
        anything from KielSync to stand in for a gateway."""

        class FakeGateway:
            def initialize(self, request):
                return InitializeResult(
                    gateway_reference=request.reference,
                    authorization_url="https://example.test/pay",
                )

            def verify(self, gateway_reference):
                return VerificationResult(
                    gateway_reference=gateway_reference,
                    status=PaymentStatus.SUCCESS,
                    amount=100,
                    currency="NGN",
                    paid_at=datetime(2026, 7, 25),
                )

            def refund(self, gateway_reference, amount=None):
                return RefundResult(
                    gateway_reference=gateway_reference,
                    status=PaymentStatus.PENDING,
                    amount=amount or 0,
                )

            def parse_webhook(self, raw_body, headers):
                return WebhookParseResult.rejected()

        assert isinstance(FakeGateway(), Gateway)

    def test_an_incomplete_implementation_does_not_satisfy_the_protocol(self):
        class HalfGateway:
            def initialize(self, request):
                ...

        assert not isinstance(HalfGateway(), Gateway)


@pytest.mark.parametrize(
    "method", ["initialize", "verify", "refund", "parse_webhook"]
)
def test_protocol_methods_are_documented(method):
    """The docstrings are the source material for SPEC.md, so an
    undocumented method is a gap in the specification, not just in the code."""
    docstring = getattr(Gateway, method).__doc__
    assert docstring and len(docstring.strip()) > 100


@pytest.mark.parametrize("method", ["initialize", "verify", "refund", "parse_webhook"])
def test_adapter_methods_are_documented(method):
    docstring = getattr(PaystackGateway, method).__doc__
    assert docstring and len(docstring.strip()) > 100
