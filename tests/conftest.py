"""Shared fixtures for the gateway adapter tests.

No test in this suite touches the network. Adapters are built over an
:class:`httpx.MockTransport`, which intercepts below the client so that
timeouts, headers, and URL construction are all exercised for real while
nothing leaves the process.
"""

import httpx
import pytest

from kielsync.core.gateways.paystack import PaystackGateway

# Obviously non-functional. Kept identical to the key the conformance
# vectors are signed with so that fixtures and vectors agree.
TEST_SECRET_KEY = "sk_test_kielsync_conformance_vector_key"


@pytest.fixture
def secret_key():
    return TEST_SECRET_KEY


@pytest.fixture
def make_gateway():
    """Build a PaystackGateway whose HTTP calls are served by a handler.

    The handler receives the real :class:`httpx.Request` the adapter
    built, so tests can assert on the URL, headers, and body that would
    have gone over the wire. Raising from the handler simulates a
    transport failure.
    """
    created = []

    def factory(handler, *, secret_key=TEST_SECRET_KEY):
        gateway = PaystackGateway(
            secret_key, transport=httpx.MockTransport(handler)
        )
        created.append(gateway)
        return gateway

    yield factory

    for gateway in created:
        gateway.close()


@pytest.fixture
def responder():
    """Build a handler that returns one fixed response and records requests."""

    def factory(status_code=200, *, json=None, text=None, record=None):
        def handler(request):
            if record is not None:
                record.append(request)
            if text is not None:
                return httpx.Response(status_code, text=text)
            return httpx.Response(status_code, json=json if json is not None else {})

        return handler

    return factory


@pytest.fixture
def raiser():
    """Build a handler that raises, simulating a transport-level failure."""

    def factory(exception):
        def handler(request):
            raise exception

        return handler

    return factory
