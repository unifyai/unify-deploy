from __future__ import annotations

from datetime import datetime, timedelta, timezone

from droid_deploy.infra.workers import entrypoint_ingest


def test_duplicate_defer_seconds_waits_until_near_lease_expiry(monkeypatch) -> None:
    monkeypatch.setenv("DROID_DUPLICATE_DEFER_JITTER_SECONDS", "0")
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()

    delay = entrypoint_ingest._duplicate_defer_seconds(expires_at)

    assert 120 <= delay <= 125


def test_duplicate_defer_seconds_is_bounded(monkeypatch) -> None:
    monkeypatch.setenv("DROID_DUPLICATE_DEFER_JITTER_SECONDS", "0")
    monkeypatch.setenv("DROID_DUPLICATE_DEFER_MAX_SECONDS", "600")
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    # Pub/Sub caps modify_ack_deadline at 600s, so the defer is bounded there
    # even when the owner lease nominally lasts longer.
    assert entrypoint_ingest._duplicate_defer_seconds(expires_at) == 600


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
