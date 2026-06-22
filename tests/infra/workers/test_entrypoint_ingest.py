from __future__ import annotations

from datetime import datetime, timedelta, timezone

from droid_deploy.infra.workers import entrypoint_ingest


def test_duplicate_defer_seconds_waits_past_steal_grace(monkeypatch) -> None:
    monkeypatch.setenv("DROID_DUPLICATE_DEFER_JITTER_SECONDS", "0")
    monkeypatch.delenv("DROID_INGEST_LEASE_STEAL_GRACE_SECONDS", raising=False)
    monkeypatch.delenv("DROID_DUPLICATE_DEFER_BUFFER_SECONDS", raising=False)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()

    delay = entrypoint_ingest._duplicate_defer_seconds(expires_at)

    # ~120s to expiry + 30s steal grace + 5s buffer = ~155s, so the redelivery
    # lands after the lease is actually reclaimable (not on the dot of expiry).
    assert 153 <= delay <= 157


def test_duplicate_defer_seconds_is_bounded(monkeypatch) -> None:
    monkeypatch.setenv("DROID_DUPLICATE_DEFER_JITTER_SECONDS", "0")
    monkeypatch.setenv("DROID_DUPLICATE_DEFER_MAX_SECONDS", "600")
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    # Pub/Sub caps modify_ack_deadline at 600s, so the defer is bounded there
    # even when the owner lease nominally lasts longer.
    assert entrypoint_ingest._duplicate_defer_seconds(expires_at) == 600


def test_duplicate_defer_seconds_short_fallback_when_expiry_unparseable(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DROID_DUPLICATE_DEFER_JITTER_SECONDS", "0")
    monkeypatch.delenv("DROID_DUPLICATE_DEFER_FALLBACK_SECONDS", raising=False)

    # An unreadable expiry must fall back to a short re-check, not a 5-min stall.
    assert entrypoint_ingest._duplicate_defer_seconds("not-a-timestamp") == 30


def test_duplicate_defer_seconds_fallback_is_tunable(monkeypatch) -> None:
    monkeypatch.setenv("DROID_DUPLICATE_DEFER_JITTER_SECONDS", "0")
    monkeypatch.setenv("DROID_DUPLICATE_DEFER_FALLBACK_SECONDS", "45")

    assert entrypoint_ingest._duplicate_defer_seconds("") == 45


def test_lease_lifetime_cap_defaults(monkeypatch) -> None:
    monkeypatch.delenv("DROID_INGEST_LEASE_MAX_LIFETIME_S", raising=False)
    monkeypatch.delenv("DROID_INGEST_LEASE_MAX_EXTENSIONS", raising=False)

    max_lifetime_s, max_extensions = entrypoint_ingest._lease_lifetime_cap()

    assert max_lifetime_s == 1800.0
    assert max_extensions is None


def test_lease_lifetime_cap_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("DROID_INGEST_LEASE_MAX_LIFETIME_S", "0")
    monkeypatch.delenv("DROID_INGEST_LEASE_MAX_EXTENSIONS", raising=False)

    max_lifetime_s, max_extensions = entrypoint_ingest._lease_lifetime_cap()

    assert max_lifetime_s is None
    assert max_extensions is None


def test_lease_lifetime_cap_custom_values(monkeypatch) -> None:
    monkeypatch.setenv("DROID_INGEST_LEASE_MAX_LIFETIME_S", "900")
    monkeypatch.setenv("DROID_INGEST_LEASE_MAX_EXTENSIONS", "20")

    max_lifetime_s, max_extensions = entrypoint_ingest._lease_lifetime_cap()

    assert max_lifetime_s == 900.0
    assert max_extensions == 20


def test_lease_lifetime_cap_ignores_garbage(monkeypatch) -> None:
    monkeypatch.setenv("DROID_INGEST_LEASE_MAX_LIFETIME_S", "not-a-number")
    monkeypatch.setenv("DROID_INGEST_LEASE_MAX_EXTENSIONS", "also-bad")

    max_lifetime_s, max_extensions = entrypoint_ingest._lease_lifetime_cap()

    # Falls back to the safe default lifetime; bad extension count is ignored.
    assert max_lifetime_s == 1800.0
    assert max_extensions is None
