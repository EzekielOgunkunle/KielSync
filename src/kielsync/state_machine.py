from kielsync.exceptions import InvalidTransition

TRANSACTION_TRANSITIONS = {
    "CREATED": {"PENDING"},
    "PENDING": {"SUCCESS", "FAILED", "ABANDONED"},
    "SUCCESS": set(),
    "FAILED": set(),
    "ABANDONED": set(),
}

PAYMENT_ATTEMPT_TRANSITIONS = {
    "INITIATED": {"REDIRECTED", "FAILED", "EXPIRED"},
    "REDIRECTED": {"SUCCESS", "FAILED", "EXPIRED"},
    "SUCCESS": set(),
    "FAILED": set(),
    "EXPIRED": set(),
}


def perform_transition(instance, new_status, transitions):
    current_status = instance.status
    allowed = transitions.get(current_status, set())
    if new_status not in allowed:
        raise InvalidTransition(
            f"Cannot transition {instance.__class__.__name__} "
            f"from {current_status!r} to {new_status!r}."
        )
    instance.status = new_status
    instance.save(update_fields=["status", "updated_at"])
