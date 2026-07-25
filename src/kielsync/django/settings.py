"""Configuration for the Django integration, and the gateway factory.

This module is the only place in KielSync that reads configuration from
its surroundings. Everything under :mod:`kielsync.core` receives its
credentials as constructor arguments, which is what allows the adapters
to be unit-tested without a settings module and instantiated more than
once with different keys in the same process.

Secrets are read from :data:`os.environ` and not from
:mod:`django.conf.settings`. Django settings modules are source files:
they are committed, they are printed by ``diffsettings``, and they are
dumped in full on Django's debug error page. An environment variable is
not perfect, but it does not end up in version control by accident.

There are no defaults. A missing secret key raises rather than falling
back, because the plausible fallbacks are all worse than stopping — a
placeholder key turns into confusing 401s in production, and a test key
turns into payments that silently never settle.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from kielsync.core.exceptions import ConfigurationError
from kielsync.core.gateways.base import Gateway
from kielsync.core.gateways.paystack import PaystackGateway

__all__ = [
    "PAYSTACK_SECRET_KEY_ENV",
    "get_gateway",
    "get_paystack_secret_key",
]

PAYSTACK_SECRET_KEY_ENV = "KIELSYNC_PAYSTACK_SECRET_KEY"


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value.strip():
        raise ConfigurationError(
            f"{name} is not set. KielSync reads gateway credentials from the "
            f"environment and has no default for this one."
        )
    return value


def get_paystack_secret_key() -> str:
    """Read the Paystack secret key from the environment.

    Raises :exc:`~kielsync.core.exceptions.ConfigurationError` when the
    variable is unset or blank. The exception names the variable but
    never its value, so the message is safe to log.
    """
    return _require_env(PAYSTACK_SECRET_KEY_ENV)


def _build_paystack() -> Gateway:
    return PaystackGateway(secret_key=get_paystack_secret_key())


_BUILDERS: dict[str, Callable[[], Gateway]] = {
    "PAYSTACK": _build_paystack,
}


def get_gateway(name: str) -> Gateway:
    """Construct the adapter for a gateway, with its configuration injected.

    ``name`` is matched case-insensitively against the values of
    ``PaymentAttempt.Gateway``, so a name read straight from a stored
    attempt can be passed in. An unrecognised name raises
    :exc:`~kielsync.core.exceptions.ConfigurationError` rather than
    returning ``None``: a caller that has reached this point has a
    payment to move, and there is no useful way to continue without an
    adapter.

    Each call builds a new adapter, and each adapter owns an HTTP
    connection pool. Callers that make more than an occasional request
    should hold on to the instance rather than calling this per request.
    The factory deliberately does not cache: a cached adapter would keep
    serving a rotated-out secret key until the process restarted.
    """
    key = name.strip().upper()
    try:
        builder = _BUILDERS[key]
    except KeyError:
        raise ConfigurationError(
            f"No KielSync gateway adapter is registered for {name!r}. "
            f"Known gateways: {sorted(_BUILDERS)}."
        ) from None
    return builder()
