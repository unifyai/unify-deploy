from __future__ import annotations

from datetime import datetime, timedelta, timezone

from unify_deploy.infra.workers import entrypoint_ingest


def test_duplicate_defer_seconds_waits_past_steal_grace(monkeypatch) -> None:
    monkeypatch.setenv("UNIFY_DUPLICATE_DEFER_JITTER_SECONDS", "0")
    monkeypatch.delenv("UNIFY_INGEST_LEASE_STEAL_GRACE_SECONDS", raising=False)
    monkeypatch.delenv("UNIFY_DUPLICATE_DEFER_BUFFER_SECONDS", raising=False)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()

    delay = entrypoint_ingest._duplicate_defer_seconds(expires_at)

    # ~120s to expiry + 30s steal grace + 5s buffer = ~155s, so the redelivery
    # lands after the lease is actually reclaimable (not on the dot of expiry).
    assert 153 <= delay <= 157


def test_duplicate_defer_seconds_is_bounded(monkeypatch) -> None:
    monkeypatch.setenv("UNIFY_DUPLICATE_DEFER_JITTER_SECONDS", "0")
    monkeypatch.setenv("UNIFY_DUPLICATE_DEFER_MAX_SECONDS", "600")
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    # Pub/Sub caps modify_ack_deadline at 600s, so the defer is bounded there
    # even when the owner lease nominally lasts longer.
    assert entrypoint_ingest._duplicate_defer_seconds(expires_at) == 600


def test_duplicate_defer_seconds_short_fallback_when_expiry_unparseable(
    monkeypatch,
) -> None:
    monkeypatch.setenv("UNIFY_DUPLICATE_DEFER_JITTER_SECONDS", "0")
    monkeypatch.delenv("UNIFY_DUPLICATE_DEFER_FALLBACK_SECONDS", raising=False)

    # An unreadable expiry must fall back to a short re-check, not a 5-min stall.
    assert entrypoint_ingest._duplicate_defer_seconds("not-a-timestamp") == 30


def test_duplicate_defer_seconds_fallback_is_tunable(monkeypatch) -> None:
    monkeypatch.setenv("UNIFY_DUPLICATE_DEFER_JITTER_SECONDS", "0")
    monkeypatch.setenv("UNIFY_DUPLICATE_DEFER_FALLBACK_SECONDS", "45")

    assert entrypoint_ingest._duplicate_defer_seconds("") == 45


def test_lease_lifetime_cap_defaults(monkeypatch) -> None:
    monkeypatch.delenv("UNIFY_INGEST_LEASE_MAX_LIFETIME_S", raising=False)
    monkeypatch.delenv("UNIFY_INGEST_LEASE_MAX_EXTENSIONS", raising=False)

    max_lifetime_s, max_extensions = entrypoint_ingest._lease_lifetime_cap()

    # An hour, not the previous 30 minutes. The cap bounds one *message*, and a
    # message is one whole file: 30 minutes was sized against a chunk time, so
    # every multi-million-row file tripped it repeatedly and paid a redelivery
    # and a resume each time.
    assert max_lifetime_s == 3600.0
    assert max_extensions is None


def test_lease_lifetime_cap_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("UNIFY_INGEST_LEASE_MAX_LIFETIME_S", "0")
    monkeypatch.delenv("UNIFY_INGEST_LEASE_MAX_EXTENSIONS", raising=False)

    max_lifetime_s, max_extensions = entrypoint_ingest._lease_lifetime_cap()

    assert max_lifetime_s is None
    assert max_extensions is None


def test_lease_lifetime_cap_custom_values(monkeypatch) -> None:
    monkeypatch.setenv("UNIFY_INGEST_LEASE_MAX_LIFETIME_S", "900")
    monkeypatch.setenv("UNIFY_INGEST_LEASE_MAX_EXTENSIONS", "20")

    max_lifetime_s, max_extensions = entrypoint_ingest._lease_lifetime_cap()

    assert max_lifetime_s == 900.0
    assert max_extensions == 20


def test_lease_lifetime_cap_ignores_garbage(monkeypatch) -> None:
    monkeypatch.setenv("UNIFY_INGEST_LEASE_MAX_LIFETIME_S", "not-a-number")
    monkeypatch.setenv("UNIFY_INGEST_LEASE_MAX_EXTENSIONS", "also-bad")

    max_lifetime_s, max_extensions = entrypoint_ingest._lease_lifetime_cap()

    # Falls back to the safe default lifetime; bad extension count is ignored.
    assert max_lifetime_s == 3600.0
    assert max_extensions is None


def test_surrender_grace_defaults_and_tunes(monkeypatch) -> None:
    # The window in which a surrendering body must reach a chunk boundary. The
    # lease keeps being renewed for this long so the message does not become
    # reclaimable while the predecessor still holds the attempt lease.
    monkeypatch.delenv("UNIFY_INGEST_LEASE_SURRENDER_GRACE_S", raising=False)
    assert entrypoint_ingest._surrender_grace_seconds() == 900.0

    monkeypatch.setenv("UNIFY_INGEST_LEASE_SURRENDER_GRACE_S", "120")
    assert entrypoint_ingest._surrender_grace_seconds() == 120.0


def test_surrender_grace_refuses_a_disabling_value(monkeypatch) -> None:
    # Zero would mean "nack immediately", which is the defect this replaced.
    # Garbage and zero both fall back to the default rather than to no grace.
    monkeypatch.setenv("UNIFY_INGEST_LEASE_SURRENDER_GRACE_S", "0")
    assert entrypoint_ingest._surrender_grace_seconds() == 900.0

    monkeypatch.setenv("UNIFY_INGEST_LEASE_SURRENDER_GRACE_S", "not-a-number")
    assert entrypoint_ingest._surrender_grace_seconds() == 900.0
