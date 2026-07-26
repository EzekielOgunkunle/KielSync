"""Conversion between minor units, major units, and display amounts.

KielSync stores and reasons about money as integers in the currency's
minor unit, everywhere, without exception. Floats never represent an
amount, and no amount is divided or multiplied outside this module.

Three vocabularies meet here:

*minor units*
    The integer KielSync stores. 500000 for ₦5,000.00; 5000 for 5,000
    XOF, which has no subunit at all.

*major units*
    What some gateway APIs expect on the wire. Flutterwave's v3 API
    takes ``5000`` to mean five thousand naira, where Paystack would
    take ``500000``. Represented as :class:`~decimal.Decimal`, never as
    a float.

*display amounts*
    What a human reads. Same numeric value as major units; the separate
    name exists because the intent differs, and code converting for a
    receipt should not look like code converting for an API.

The zero-exponent currencies are why this module exists rather than a
hardcoded ``/ 100``. XOF, XAF, and JPY have no minor unit, so the naive
divisor is wrong by a factor of a hundred for a large part of West
Africa — which is the market this library is for.
"""

from decimal import Decimal, InvalidOperation

from kielsync.core.exceptions import UnknownCurrency

__all__ = [
    "CURRENCY_EXPONENTS",
    "from_major_units",
    "to_display",
    "to_major_units",
    "to_minor_units",
]

CURRENCY_EXPONENTS = {
    "NGN": 2,
    "XOF": 0,
    "USD": 2,
    "GHS": 2,
}


def _exponent(currency):
    try:
        return CURRENCY_EXPONENTS[currency]
    except KeyError:
        raise UnknownCurrency(
            f"Unknown currency {currency!r}. "
            f"Known currencies: {sorted(CURRENCY_EXPONENTS)}."
        ) from None


def to_minor_units(display_amount, currency):
    exponent = _exponent(currency)
    if not isinstance(display_amount, Decimal):
        try:
            display_amount = Decimal(str(display_amount))
        except InvalidOperation:
            raise ValueError(f"Invalid display amount: {display_amount!r}") from None
    minor = display_amount.scaleb(exponent).to_integral_value()
    return int(minor)


def to_display(minor, currency):
    exponent = _exponent(currency)
    return Decimal(minor).scaleb(-exponent)


def to_major_units(minor, currency):
    """Convert stored minor units into the major-unit amount to send.

    Used by adapters whose gateway expects major units on the wire. The
    result is a :class:`~decimal.Decimal` so that it can be serialised
    exactly; converting it to a float on the way into a JSON body would
    reintroduce the representation error this module exists to avoid.

    Numerically identical to :func:`to_display`. The two are kept apart
    because they answer different questions, and a reader should be able
    to tell from the call site whether an amount is being prepared for a
    gateway or for a human.
    """
    exponent = _exponent(currency)
    if isinstance(minor, bool) or not isinstance(minor, int):
        raise TypeError(
            f"Minor-unit amounts must be int, got {type(minor).__name__}: "
            f"{minor!r}."
        )
    return Decimal(minor).scaleb(-exponent)


def from_major_units(major, currency):
    """Convert a gateway's major-unit amount into stored minor units.

    Accepts the types gateways actually send — ``int``, ``float``,
    numeric ``str``, and :class:`~decimal.Decimal` — and always returns
    an ``int``.

    Raises :exc:`ValueError` when the amount carries more precision than
    the currency has minor units, rather than rounding it away. Rounding
    here would be indefensible: the amount a gateway reports is compared
    against the amount a transaction expects, and quietly adjusting one
    side of that comparison would defeat the reconciliation check that
    exists to catch exactly this. An amount KielSync cannot represent
    exactly is a fact about the payment, and the caller must see it.

    Trailing zeros are not precision — ``"5000.000"`` for NGN is exactly
    ₦5,000 and converts cleanly.
    """
    exponent = _exponent(currency)

    if isinstance(major, bool):
        raise TypeError(f"Invalid major-unit amount: {major!r}.")
    if isinstance(major, Decimal):
        value = major
    elif isinstance(major, (int, float, str)):
        try:
            value = Decimal(str(major))
        except InvalidOperation:
            raise ValueError(f"Invalid major-unit amount: {major!r}.") from None
    else:
        raise TypeError(
            f"Invalid major-unit amount type {type(major).__name__}: {major!r}."
        )

    if not value.is_finite():
        raise ValueError(f"Invalid major-unit amount: {major!r}.")

    scaled = value.scaleb(exponent)
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"Amount {major!r} has more precision than {currency} can "
            f"represent: {currency} has {exponent} minor-unit digits, so "
            f"this amount is not a whole number of minor units."
        )
    return int(scaled)
