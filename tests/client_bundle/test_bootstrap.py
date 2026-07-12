"""Tests for offline client-bundle bootstrap."""

from __future__ import annotations

from pathlib import Path

from unify_deploy.client_bundle import bootstrap


def test_ensure_offline_client_bundle_resolves_startup_spec(monkeypatch):
    calls: list = []

    monkeypatch.setenv("UNITY_DEPLOY_CLIENT_MODE", "bundled")
    monkeypatch.setenv("ASSISTANT_ID", "1406")
    monkeypatch.setenv("ORG_ID", "1")
    monkeypatch.setenv("USER_ID", "user-1")
    monkeypatch.setenv("TEAM_IDS", "11,22")

    def fake_resolve_startup_spec(identity):
        calls.append(identity)
        return object()

    monkeypatch.setattr(
        "unify_deploy.client_bundle.fetch.bundled_client_mode",
        lambda: True,
    )
    monkeypatch.setattr(
        "unify_deploy.startup_config.resolve_startup_spec",
        fake_resolve_startup_spec,
    )

    bootstrap.ensure_offline_client_bundle()

    assert len(calls) == 1
    identity = calls[0]
    assert identity.assistant_id == "1406"
    assert identity.user_id == "user-1"
    assert identity.org_id == 1
    assert identity.team_ids == (11, 22)


def test_ensure_offline_client_bundle_skips_without_assistant_id(monkeypatch):
    monkeypatch.setenv("UNITY_DEPLOY_CLIENT_MODE", "bundled")
    monkeypatch.delenv("ASSISTANT_ID", raising=False)

    called = {"resolve": False}

    monkeypatch.setattr(
        "unify_deploy.client_bundle.fetch.bundled_client_mode",
        lambda: True,
    )
    monkeypatch.setattr(
        "unify_deploy.startup_config.resolve_startup_spec",
        lambda identity: called.__setitem__("resolve", True),
    )

    bootstrap.ensure_offline_client_bundle()

    assert called["resolve"] is False


def test_ensure_offline_client_bundle_skips_when_not_bundled(monkeypatch):
    monkeypatch.setenv("ASSISTANT_ID", "1406")
    called = {"resolve": False}

    monkeypatch.setattr(
        "unify_deploy.client_bundle.fetch.bundled_client_mode",
        lambda: False,
    )
    monkeypatch.setattr(
        "unify_deploy.startup_config.resolve_startup_spec",
        lambda identity: called.__setitem__("resolve", True),
    )

    bootstrap.ensure_offline_client_bundle()

    assert called["resolve"] is False


def test_entrypoint_offline_path_bootstraps_client_bundle():
    entrypoint = Path(__file__).resolve().parents[2] / "base" / "entrypoint.sh"
    text = entrypoint.read_text()
    assert "unify_deploy.client_bundle.bootstrap" in text
    assert "unify.task_scheduler.offline_runner" in text
    bootstrap_idx = text.index("unify_deploy.client_bundle.bootstrap")
    runner_idx = text.index("unify.task_scheduler.offline_runner")
    assert bootstrap_idx < runner_idx
