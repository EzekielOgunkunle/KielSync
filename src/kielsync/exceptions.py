class KielSyncError(Exception):
    """Base exception for all kielsync errors."""


class InvalidTransition(KielSyncError):
    """Raised when an illegal state transition is attempted."""


class UnknownCurrency(KielSyncError):
    """Raised when a currency code has no known minor-unit exponent."""
