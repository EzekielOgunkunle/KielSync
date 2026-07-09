from decimal import Decimal

import pytest

from kielsync.currency import to_display, to_minor_units
from kielsync.exceptions import UnknownCurrency


def test_to_minor_units_ngn_with_decimals():
    assert to_minor_units("5000.00", "NGN") == 500000


def test_to_minor_units_xof_has_zero_exponent():
    assert to_minor_units("5000", "XOF") == 5000


def test_to_minor_units_accepts_decimal_instance():
    assert to_minor_units(Decimal("12.34"), "USD") == 1234


def test_to_display_ngn():
    assert to_display(500000, "NGN") == Decimal("5000.00")


def test_to_display_xof():
    assert to_display(5000, "XOF") == Decimal("5000")


@pytest.mark.parametrize(
    "amount,currency",
    [
        ("100.50", "NGN"),
        ("1", "XOF"),
        ("99.99", "USD"),
        ("42.42", "GHS"),
    ],
)
def test_round_trip(amount, currency):
    minor = to_minor_units(amount, currency)
    assert to_display(minor, currency) == Decimal(amount)


def test_unknown_currency_raises_on_to_minor_units():
    with pytest.raises(UnknownCurrency):
        to_minor_units("100", "ZZZ")


def test_unknown_currency_raises_on_to_display():
    with pytest.raises(UnknownCurrency):
        to_display(100, "ZZZ")
