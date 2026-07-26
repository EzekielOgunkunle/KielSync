"""Tests for derived webhook deduplication ids.

Two properties matter, and they pull in opposite directions: the same
event must always hash the same way (or redeliveries create duplicate
rows), and a different event must hash differently (or a real event is
silently swallowed as a duplicate). Everything here is one of those two.
"""

import pytest

from kielsync.core.webhooks import DERIVED_ID_PREFIX, derive_event_id


class TestStability:
    """Same event, same id — this is what makes deduplication work."""

    def test_identical_inputs_produce_identical_ids(self):
        first = derive_event_id("charge.completed", 1234, "successful")
        second = derive_event_id("charge.completed", 1234, "successful")
        assert first == second

    def test_stable_across_repeated_calls(self):
        ids = {
            derive_event_id("charge.completed", 1234, "successful")
            for _ in range(50)
        }
        assert len(ids) == 1

    def test_numeric_and_string_subject_ids_agree(self):
        """A gateway may send the id as a JSON number on one delivery and
        a string on the retry. That must not create a second row."""
        assert derive_event_id("charge.completed", 1234, "successful") == (
            derive_event_id("charge.completed", "1234", "successful")
        )

    def test_event_name_whitespace_does_not_change_the_id(self):
        assert derive_event_id("charge.completed", 1, "successful") == (
            derive_event_id("  charge.completed  ", 1, "successful")
        )

    def test_no_time_or_randomness_leaks_in(self):
        """A clock or a nonce in the key would break redelivery matching
        in a way no single-run test would catch."""
        import time

        first = derive_event_id("charge.completed", 99, "successful")
        time.sleep(0.01)
        assert derive_event_id("charge.completed", 99, "successful") == first


class TestDiscrimination:
    """Different event, different id — this is what stops silent loss."""

    def test_different_status_produces_a_different_id(self):
        """A transaction reported pending and later successful is two
        events, not one redelivery."""
        pending = derive_event_id("charge.completed", 1234, "pending")
        successful = derive_event_id("charge.completed", 1234, "successful")
        assert pending != successful

    def test_different_event_type_produces_a_different_id(self):
        """A charge and a later refund on the same transaction must both
        be recorded."""
        charge = derive_event_id("charge.completed", 1234, "successful")
        refund = derive_event_id("refund.completed", 1234, "successful")
        assert charge != refund

    def test_different_subject_produces_a_different_id(self):
        assert derive_event_id("charge.completed", 1234, "successful") != (
            derive_event_id("charge.completed", 5678, "successful")
        )

    def test_extra_discriminator_separates_otherwise_identical_events(self):
        """The documented escape hatch for two identical partial refunds."""
        base = derive_event_id("refund.completed", 1234, "completed")
        first = derive_event_id("refund.completed", 1234, "completed", extra="r1")
        second = derive_event_id("refund.completed", 1234, "completed", extra="r2")
        assert len({base, first, second}) == 3

    def test_fields_cannot_be_smuggled_across_the_separator(self):
        """Naive concatenation would make ("a", "bc") and ("ab", "c")
        collide. The separator has to actually separate."""
        assert derive_event_id("a", "bc", "x") != derive_event_id("ab", "c", "x")
        assert derive_event_id("charge", "1:2", "ok") != (
            derive_event_id("charge:1", "2", "ok")
        )


class TestMissingSubject:
    @pytest.mark.parametrize("subject", [None, ""])
    def test_no_subject_id_returns_none(self, subject):
        """Nothing to key on. Returning None makes the caller decide,
        rather than inventing a value that would defeat deduplication."""
        assert derive_event_id("charge.completed", subject, "successful") is None

    def test_zero_is_a_valid_subject_id(self):
        """0 is falsy but is a legitimate identifier; only None and the
        empty string mean 'absent'."""
        assert derive_event_id("charge.completed", 0, "successful") is not None

    def test_missing_status_is_tolerated(self):
        assert derive_event_id("charge.completed", 1234, None) is not None

    def test_missing_event_type_is_tolerated(self):
        assert derive_event_id(None, 1234, "successful") is not None


class TestShape:
    def test_is_marked_as_derived(self):
        """Derived ids are only as trustworthy as the fields behind them,
        so they are distinguishable from gateway-supplied ones."""
        assert derive_event_id("charge.completed", 1, "ok").startswith(
            f"{DERIVED_ID_PREFIX}:"
        )

    def test_includes_the_event_name_for_readability(self):
        assert "charge.completed" in derive_event_id("charge.completed", 1, "ok")

    def test_fits_the_event_id_column(self):
        """WebhookEvent.event_id is a CharField(max_length=128)."""
        long_name = "gateway.some.extremely.verbose.event.name" * 2
        assert len(derive_event_id(long_name, "9" * 40, "successful")) <= 128

    def test_is_a_string(self):
        assert isinstance(derive_event_id("charge.completed", 1, "ok"), str)


class TestRegression:
    """Pin the exact output. If the algorithm changes, every previously
    stored id stops matching and deduplication silently breaks for every
    in-flight event — so a change here must be a deliberate one."""

    def test_known_vector(self):
        assert derive_event_id("charge.completed", 1234, "successful") == (
            "d:charge.completed:bcd393cdfef1f1a0394897a314815c1b"
        )
