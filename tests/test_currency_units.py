"""Tests for major/minor unit conversion at a gateway boundary.

These were written before the implementation, because this is the
highest-risk arithmetic in the library. KielSync stores minor units
everywhere; Paystack speaks minor units too, so the Paystack adapter
never converts. Flutterwave's v3 API speaks *major* units, so every
amount crossing that boundary is converted twice — once out, once back.

A sign error costs nothing. An exponent error charges a Nigerian
customer ₦500,000 instead of ₦5,000, and the only thing standing between
that and production is this file.
"""

from decimal import Decimal

import pytest

from kielsync.core.currency import (
    from_major_units,
    to_major_units,
    to_minor_units,
)
from kielsync.core.exceptions import UnknownCurrency


class TestToMajorUnits:
    """Minor units out to the gateway."""

    @pytest.mark.parametrize(
        "minor,currency,expected",
        [
            (500_000, "NGN", "5000.00"),
            (1, "NGN", "0.01"),
            (100, "NGN", "1.00"),
            (99, "NGN", "0.99"),
            (123_456_789, "NGN", "1234567.89"),
            (1234, "USD", "12.34"),
            (4242, "GHS", "42.42"),
        ],
    )
    def test_two_exponent_currencies(self, minor, currency, expected):
        assert to_major_units(minor, currency) == Decimal(expected)

    @pytest.mark.parametrize(
        "minor,expected",
        [(5000, "5000"), (1, "1"), (0, "0"), (1_000_000, "1000000")],
    )
    def test_xof_is_a_zero_exponent_currency(self, minor, expected):
        """XOF has no subunit. 5000 XOF is 5000, not 50.00 — the single
        most likely place for a hardcoded /100 to go wrong."""
        assert to_major_units(minor, "XOF") == Decimal(expected)

    def test_returns_decimal_never_float(self):
        """A float amount is how a payments library loses a kobo per
        transaction and then loses an audit."""
        result = to_major_units(500_000, "NGN")
        assert isinstance(result, Decimal)
        assert not isinstance(result, float)

    def test_unknown_currency_raises(self):
        with pytest.raises(UnknownCurrency):
            to_major_units(1000, "ZZZ")


class TestFromMajorUnits:
    """Major units back from the gateway."""

    @pytest.mark.parametrize(
        "major,currency,expected",
        [
            ("5000.00", "NGN", 500_000),
            ("5000", "NGN", 500_000),
            ("0.01", "NGN", 1),
            ("0.99", "NGN", 99),
            ("1234567.89", "NGN", 123_456_789),
            ("12.34", "USD", 1234),
        ],
    )
    def test_two_exponent_currencies(self, major, currency, expected):
        assert from_major_units(major, currency) == expected

    @pytest.mark.parametrize("major,expected", [("5000", 5000), ("1", 1), ("0", 0)])
    def test_xof_is_a_zero_exponent_currency(self, major, expected):
        assert from_major_units(major, "XOF") == expected

    @pytest.mark.parametrize(
        "major", [5000, 5000.0, 5000.5, Decimal("5000.00"), "5000.00"]
    )
    def test_accepts_the_json_types_a_gateway_actually_sends(self, major):
        """Gateways send amounts as ints, floats, or numeric strings
        depending on the endpoint and the day."""
        assert isinstance(from_major_units(major, "NGN"), int)

    def test_returns_int_never_float(self):
        assert isinstance(from_major_units("5000.00", "NGN"), int)

    def test_unknown_currency_raises(self):
        with pytest.raises(UnknownCurrency):
            from_major_units("10.00", "ZZZ")

    @pytest.mark.parametrize("garbage", ["", "abc", "5,000.00", None, object()])
    def test_unparseable_amounts_raise(self, garbage):
        with pytest.raises((ValueError, TypeError)):
            from_major_units(garbage, "NGN")


class TestPrecisionIsRejectedNotRounded:
    """The behaviour that separates a payments library from a calculator."""

    @pytest.mark.parametrize(
        "major,currency",
        [
            ("5000.001", "NGN"),
            ("0.005", "NGN"),
            ("1.234", "USD"),
            ("5000.5", "XOF"),
            ("0.1", "XOF"),
        ],
    )
    def test_sub_minor_unit_precision_raises(self, major, currency):
        """Silently rounding a gateway's amount would let a mismatch pass
        the reconciliation check that exists to catch exactly that. If a
        gateway sends an amount KielSync cannot represent exactly, that is
        a fact worth an exception, not a rounding mode."""
        with pytest.raises(ValueError):
            from_major_units(major, currency)

    def test_the_error_names_the_currency_and_the_amount(self):
        with pytest.raises(ValueError) as raised:
            from_major_units("0.005", "NGN")
        message = str(raised.value)
        assert "NGN" in message
        assert "0.005" in message

    def test_trailing_zeros_are_not_precision(self):
        """5000.000000 is exactly 5000 and must be accepted."""
        assert from_major_units("5000.000000", "NGN") == 500_000
        assert from_major_units("5000.0", "XOF") == 5000


class TestRoundTrip:
    """The property that actually matters: conversion loses nothing."""

    @pytest.mark.parametrize("currency", ["NGN", "XOF", "USD", "GHS"])
    @pytest.mark.parametrize(
        "minor", [0, 1, 99, 100, 500_000, 999_999, 123_456_789]
    )
    def test_minor_to_major_and_back_is_identity(self, currency, minor):
        assert from_major_units(to_major_units(minor, currency), currency) == minor

    @pytest.mark.parametrize("currency", ["NGN", "XOF"])
    @pytest.mark.parametrize("minor", [1, 5000, 500_000])
    def test_round_trip_survives_string_serialisation(self, currency, minor):
        """The adapter puts the major amount into a JSON body as a string,
        so the round trip has to survive that too."""
        as_sent = str(to_major_units(minor, currency))
        assert from_major_units(as_sent, currency) == minor

    def test_ngn_and_xof_do_not_agree(self):
        """A guard against an implementation that ignores the exponent
        table and hardcodes a divisor: 5000 minor units is ₦50.00 but
        5000 XOF exactly."""
        assert to_major_units(5000, "NGN") == Decimal("50.00")
        assert to_major_units(5000, "XOF") == Decimal("5000")
        assert to_major_units(5000, "NGN") != to_major_units(5000, "XOF")


class TestAgreementWithTheExistingHelpers:
    """The new functions must not be a second, subtly different scheme."""

    @pytest.mark.parametrize("currency", ["NGN", "XOF", "USD", "GHS"])
    @pytest.mark.parametrize("minor", [1, 100, 5000, 500_000])
    def test_from_major_units_agrees_with_to_minor_units(self, currency, minor):
        major = to_major_units(minor, currency)
        assert from_major_units(major, currency) == to_minor_units(major, currency)
