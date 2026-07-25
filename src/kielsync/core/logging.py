"""Redaction for anything derived from a gateway payload.

Gateway request and response bodies are exactly the material an
operator wants in a log when a payment goes wrong, and exactly the
material that must never be written to one. They carry API secrets,
webhook signatures, and cardholder data side by side with the reference
and status that are actually useful.

:func:`redact` is the single chokepoint. Every payload that reaches a
log record, an error report, or a debugging dump goes through it first.
It errs heavily toward over-redaction: losing a field from a log costs
one round of debugging, while leaking a secret key costs a credential
rotation and, for cardholder data, a compliance incident.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["REDACTED", "is_sensitive_key", "redact"]

REDACTED = "[REDACTED]"

# Field names that are sensitive in their entirety. Matched after folding
# the key to lowercase and normalising separators to underscores, so that
# "Account-Number", "account number", and "account_number" all match.
SENSITIVE_KEYS = frozenset(
    {
        "account_number",
        "authorization",
        "bank_account",
        "bin",
        "card",
        "card_number",
        "cvv",
        "exp_month",
        "exp_year",
        "last4",
        "number",
        "pin",
        "signature",
    }
)

# Substrings that make any field carrying them sensitive. These catch the
# open-ended names credentials arrive under — "secret_key", "api_key",
# "access_token", "x_paystack_signature", "Authorization" — without this
# module having to enumerate every gateway's spelling.
#
# "key" as a substring deliberately over-matches: it redacts benign
# fields such as "idempotency_key" too. That is the intended trade. A
# reference lost from a log is recoverable from the database; a secret
# written to a log is not recoverable at all.
SENSITIVE_KEY_SUBSTRINGS = (
    "authorization",
    "key",
    "password",
    "secret",
    "signature",
    "token",
)


def _normalise(key: str) -> str:
    folded = key.strip().lower()
    for separator in (" ", "-", "."):
        folded = folded.replace(separator, "_")
    return folded


def is_sensitive_key(key: str) -> bool:
    """Whether a field name must have its value withheld.

    Exposed so that tests and future adapters classify field names by the
    same rule the redactor uses, rather than maintaining a second list
    that can drift out of step with this one.
    """
    normalised = _normalise(key)
    if normalised in SENSITIVE_KEYS:
        return True
    return any(pattern in normalised for pattern in SENSITIVE_KEY_SUBSTRINGS)


def redact(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``payload`` with every sensitive value masked.

    Redaction is recursive and applies through nested mappings and
    through lists and tuples of them, because gateways nest the most
    sensitive material most deeply — Paystack returns cardholder details
    under ``data.authorization``, several levels below the response root.

    A sensitive key has its value replaced by the string ``[REDACTED]``
    regardless of that value's type. Masking a whole subtree this way,
    rather than descending into it, means a newly added field inside an
    already-sensitive structure is withheld by default instead of
    leaking until someone notices and extends the list.

    The input is never mutated: callers routinely pass the same decoded
    body they are about to persist, and a redactor that edited it in
    place would silently destroy the stored evidence.
    """
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(key, str) and is_sensitive_key(key):
            redacted[key] = REDACTED
        else:
            redacted[key] = _redact_value(value)
    return redacted


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return redact(value)
    if isinstance(value, (str, bytes)):
        return value
    if isinstance(value, Sequence):
        return [_redact_value(item) for item in value]
    return value
