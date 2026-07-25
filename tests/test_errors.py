"""Table-driven tests for gateway failure classification.

The tables here are deliberately exhaustive over the documented code sets
rather than sampling them. classify() is the single decision that
governs whether a failed payment is re-attempted, and a code that drifts
out of the wrong set is not the kind of bug that shows up in staging —
it shows up as a duplicate charge on a real card.
"""

import httpx
import pytest

from kielsync.core.errors import (
    RETRYABLE_GATEWAY_CODES,
    TERMINAL_GATEWAY_CODES,
    ErrorClass,
    GatewayError,
    RetryableGatewayError,
    TerminalGatewayError,
    classify,
    gateway_error,
    normalise_gateway_code,
)


class TestExceptionHierarchy:
    def test_gateway_errors_are_kielsync_errors(self):
        from kielsync.core.exceptions import KielSyncError

        assert issubclass(GatewayError, KielSyncError)
        assert issubclass(RetryableGatewayError, GatewayError)
        assert issubclass(TerminalGatewayError, GatewayError)

    def test_retryable_flag_matches_class(self):
        assert RetryableGatewayError("x").retryable is True
        assert TerminalGatewayError("x").retryable is False

    def test_error_carries_classification_signals(self):
        error = TerminalGatewayError(
            "declined", gateway="PAYSTACK", status_code=400, gateway_code="declined"
        )
        assert error.gateway == "PAYSTACK"
        assert error.status_code == 400
        assert error.gateway_code == "declined"


class TestTransportExceptions:
    @pytest.mark.parametrize(
        "exception",
        [
            httpx.ConnectTimeout("connect timed out"),
            httpx.ReadTimeout("read timed out"),
            httpx.WriteTimeout("write timed out"),
            httpx.PoolTimeout("pool timed out"),
            httpx.ConnectError("connection refused"),
            httpx.ReadError("connection reset"),
            httpx.WriteError("broken pipe"),
            TimeoutError("stdlib timeout"),
            ConnectionError("stdlib connection error"),
            ConnectionResetError("stdlib reset"),
        ],
        ids=lambda exc: type(exc).__name__,
    )
    def test_transport_failures_are_retryable(self, exception):
        assert classify(exception=exception) is ErrorClass.RETRYABLE

    @pytest.mark.parametrize(
        "exception",
        [
            ValueError("bad value"),
            KeyError("missing"),
            httpx.InvalidURL("nonsense"),
            RuntimeError("who knows"),
        ],
        ids=lambda exc: type(exc).__name__,
    )
    def test_unrecognised_exceptions_are_terminal(self, exception):
        assert classify(exception=exception) is ErrorClass.TERMINAL

    def test_remote_protocol_error_is_terminal(self):
        """A server that disconnects mid-response may have processed the
        request, so re-sending is not provably safe."""
        assert (
            classify(exception=httpx.RemoteProtocolError("server disconnected"))
            is ErrorClass.TERMINAL
        )

    def test_already_classified_errors_are_not_reclassified(self):
        retryable = RetryableGatewayError("timed out")
        terminal = TerminalGatewayError("declined")
        assert classify(exception=retryable) is ErrorClass.RETRYABLE
        assert classify(exception=terminal) is ErrorClass.TERMINAL

    def test_prior_classification_outranks_contradicting_signals(self):
        terminal = TerminalGatewayError("declined")
        assert classify(status_code=503, exception=terminal) is ErrorClass.TERMINAL


class TestHttpStatus:
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 599])
    def test_retryable_statuses(self, status):
        assert classify(status_code=status) is ErrorClass.RETRYABLE

    @pytest.mark.parametrize(
        "status", [400, 401, 402, 403, 404, 409, 418, 422, 428, 430, 451]
    )
    def test_client_errors_other_than_429_are_terminal(self, status):
        assert classify(status_code=status) is ErrorClass.TERMINAL

    @pytest.mark.parametrize("status", [200, 201, 204, 301, 302])
    def test_non_error_statuses_without_other_signals_are_terminal(self, status):
        """Reaching classify() at all means something failed. A 200 with no
        recognised gateway code is an unexplained failure, and unexplained
        failures are not retried."""
        assert classify(status_code=status) is ErrorClass.TERMINAL


class TestGatewayCodes:
    @pytest.mark.parametrize("code", sorted(RETRYABLE_GATEWAY_CODES))
    def test_every_documented_retryable_code(self, code):
        assert classify(gateway_code=code) is ErrorClass.RETRYABLE

    @pytest.mark.parametrize("code", sorted(TERMINAL_GATEWAY_CODES))
    def test_every_documented_terminal_code(self, code):
        assert classify(gateway_code=code) is ErrorClass.TERMINAL

    def test_the_two_code_sets_do_not_overlap(self):
        assert not (RETRYABLE_GATEWAY_CODES & TERMINAL_GATEWAY_CODES)

    @pytest.mark.parametrize(
        "spelling,expected",
        [
            ("Insufficient Funds", ErrorClass.TERMINAL),
            ("INSUFFICIENT_FUNDS", ErrorClass.TERMINAL),
            ("insufficient-funds", ErrorClass.TERMINAL),
            ("  Declined  ", ErrorClass.TERMINAL),
            ("Do Not Honour", ErrorClass.TERMINAL),
            ("Issuer or switch inoperative", ErrorClass.RETRYABLE),
            ("Timeout waiting for response", ErrorClass.RETRYABLE),
            ("service.unavailable", ErrorClass.RETRYABLE),
        ],
    )
    def test_gateway_spellings_of_the_same_condition_agree(self, spelling, expected):
        assert classify(gateway_code=spelling) is expected

    @pytest.mark.parametrize(
        "code", ["", "   ", "wat", "some_new_paystack_code", "processing_error"]
    )
    def test_unrecognised_codes_are_terminal(self, code):
        assert classify(gateway_code=code) is ErrorClass.TERMINAL

    def test_gateway_code_outranks_http_status(self):
        """Gateways report declines and unreachable issuers inside HTTP 200."""
        assert classify(200, "declined") is ErrorClass.TERMINAL
        assert classify(200, "issuer_or_switch_inoperative") is ErrorClass.RETRYABLE

    def test_terminal_code_beats_a_server_error_status(self):
        """Contradictory signals resolve toward 'the payment was decided'."""
        assert classify(503, "insufficient_funds") is ErrorClass.TERMINAL

    def test_unrecognised_code_falls_through_to_status(self):
        assert classify(503, "mystery_condition") is ErrorClass.RETRYABLE
        assert classify(400, "mystery_condition") is ErrorClass.TERMINAL


class TestDefaults:
    def test_no_signals_at_all_is_terminal(self):
        assert classify() is ErrorClass.TERMINAL

    def test_all_none_is_terminal(self):
        assert classify(None, None, None) is ErrorClass.TERMINAL

    def test_normalise_returns_none_for_empty_input(self):
        assert normalise_gateway_code(None) is None
        assert normalise_gateway_code("") is None
        assert normalise_gateway_code("   ") is None
        assert normalise_gateway_code("___") is None

    def test_normalise_collapses_repeated_separators(self):
        assert normalise_gateway_code("Do  Not - Honour") == "do_not_honour"


class TestGatewayErrorFactory:
    def test_builds_retryable_for_retryable_signals(self):
        error = gateway_error(
            "upstream is down", gateway="PAYSTACK", status_code=503
        )
        assert isinstance(error, RetryableGatewayError)
        assert error.retryable is True

    def test_builds_terminal_for_terminal_signals(self):
        error = gateway_error(
            "card declined", gateway="PAYSTACK", status_code=400,
            gateway_code="declined",
        )
        assert isinstance(error, TerminalGatewayError)
        assert error.retryable is False

    def test_builds_terminal_with_no_signals(self):
        assert isinstance(gateway_error("something went wrong"), TerminalGatewayError)

    def test_message_is_preserved_verbatim(self):
        assert str(gateway_error("Paystack GET /x returned 503.")) == (
            "Paystack GET /x returned 503."
        )
