from __future__ import annotations

from datetime import datetime, timedelta, timezone

from unity_deploy.infra.workers import entrypoint_ingest


def test_duplicate_defer_seconds_waits_until_near_lease_expiry(monkeypatch) -> None:
    monkeypatch.setenv("UNITY_DUPLICATE_DEFER_JITTER_SECONDS", "0")
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()

    delay = entrypoint_ingest._duplicate_defer_seconds(expires_at)

    assert 120 <= delay <= 125


def test_duplicate_defer_seconds_is_bounded(monkeypatch) -> None:
    monkeypatch.setenv("UNITY_DUPLICATE_DEFER_JITTER_SECONDS", "0")
    monkeypatch.setenv("UNITY_DUPLICATE_DEFER_MAX_SECONDS", "600")
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    assert entrypoint_ingest._duplicate_defer_seconds(expires_at) == 600
