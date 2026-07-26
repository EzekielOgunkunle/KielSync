"""Deduplication identity for webhook deliveries.

Gateways retry. A webhook that times out, or that the receiving process
dies halfway through, comes back — sometimes minutes later, sometimes
for days. ``WebhookEvent`` therefore has a uniqueness constraint on
``(gateway, event_id)``, and everything upstream depends on that column
carrying a *stable* value: the same real-world event must always produce
the same id, or the constraint deduplicates nothing.

Paystack makes this easy by sending a transaction id that, combined with
the event name, identifies the delivery. Flutterwave does not send a
delivery identifier at all, so KielSync derives one.

The derived id has to satisfy two requirements that pull against each
other:

*Stability*
    A genuine redelivery of one event must hash to the same id, so the
    second copy is recognised as a duplicate and dropped. A timestamp,
    a nonce, or a received-at clock would all break this.

*Discrimination*
    A later, genuinely different event about the same transaction must
    hash to a *different* id, or it would be silently swallowed as a
    duplicate of the first. A transaction that is charged and then
    refunded produces two events; keying on the transaction alone would
    lose the refund.

:func:`derive_event_id` resolves this by hashing exactly the fields that
change when the meaning of the event changes — the event name, the
subject's identifier, and its status — and nothing that changes on
redelivery.
"""

from __future__ import annotations

import hashlib

__all__ = ["DERIVED_ID_PREFIX", "derive_event_id"]

# Marks an id as constructed by KielSync rather than supplied by the
# gateway. Worth being able to tell apart when reading the table: a
# derived id is only as good as the fields it was derived from.
DERIVED_ID_PREFIX = "d"

# Half a SHA-256 is 128 bits, far beyond collision range for the number
# of webhooks any single integration will ever receive, and it keeps the
# id inside WebhookEvent.event_id's 128-character column with room for
# the event name.
_DIGEST_CHARS = 32

_SEPARATOR = "\x1f"  # ASCII unit separator: cannot occur in these fields.


def derive_event_id(
    event_type: object,
    subject_id: object,
    status: object,
    *,
    extra: object = None,
) -> str | None:
    """Derive a stable deduplication id for a webhook that lacks one.

    ``event_type`` is the gateway's event name, ``subject_id`` the
    identifier of the thing the event is about (a transaction or refund
    id), and ``status`` its state at the time of the event. The status is
    part of the key because a gateway may report on the same transaction
    more than once as it progresses — pending, then successful — and
    those are distinct events that must both be recorded.

    Returns ``None`` when there is no ``subject_id`` to key on. A caller
    that gets ``None`` has nothing it can safely deduplicate against and
    must decide for itself whether to store the event; it must not
    substitute a random value, which would turn every redelivery into a
    new row.

    The returned id is prefixed with the event name for readability, so
    an operator reading the table can see what an event was without
    reversing a hash. Nothing parses that prefix — the hash is the
    identity.

    Known limitation, and the reason this is documented rather than
    merely implemented: two genuinely distinct events that share an event
    name, a subject id, and a status are indistinguishable to this
    function and will deduplicate to one row. The realistic case is two
    identical partial refunds of the same transaction. Gateways that
    issue a distinct id per refund avoid it, since ``subject_id`` is then
    the refund's own id rather than the transaction's; where they do not,
    pass a discriminator through ``extra``.
    """
    if subject_id is None or subject_id == "":
        return None

    name = str(event_type).strip() if event_type is not None else ""

    parts = [name, str(subject_id), str(status) if status is not None else ""]
    if extra is not None:
        parts.append(str(extra))

    digest = hashlib.sha256(
        _SEPARATOR.join(parts).encode("utf-8")
    ).hexdigest()[:_DIGEST_CHARS]

    prefix = f"{DERIVED_ID_PREFIX}:{name}" if name else DERIVED_ID_PREFIX
    return f"{prefix}:{digest}"
