class KielSyncError(Exception):
    """Base exception for all kielsync errors."""


class InvalidTransition(KielSyncError):
    """Raised when an illegal state transition is attempted."""


class UnknownCurrency(KielSyncError):
    """Raised when a currency code has no known minor-unit exponent."""


class ConfigurationError(KielSyncError):
    """Raised when KielSync is asked to run without the configuration it needs.

    Always a deployment fault rather than a payment fault: a missing
    credential or an unknown gateway name. Raised at the point of use so
    that the message names the setting that is absent.
    """
