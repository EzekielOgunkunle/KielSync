from decimal import Decimal, InvalidOperation

from kielsync.core.exceptions import UnknownCurrency

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
