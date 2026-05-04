from __future__ import annotations

import logging
from types import SimpleNamespace

from unity_deploy import hook
from unity_deploy import startup_config
from unity_deploy.deployment_reconcile import runtime_state
from unity_deploy.utils.orchestra_client import OrchestraClientError


def test_sync_console_config_repairs_assistant_console_config(monkeypatch):
    captured: dict = {}

    def fake_patch_json(path, body):
        captured["path"] = path
        captured["body"] = body
        return {"status": "ok"}

    monkeypatch.setattr(hook, "patch_json", fake_patch_json)

    console_config = {
        "version": "1",
        "layout": {"mode": "dashboard-centric", "defaultTab": "dashboards"},
    }
    hook._sync_console_config(123, console_config)

    assert captured == {
        "path": "/admin/assistant/123",
        "body": {"console_config": console_config},
    }


def test_sync_console_config_drift_repair_is_best_effort(monkeypatch, caplog):
    def fake_patch_json(path, body):
        raise OrchestraClientError(500, "server exploded")

    monkeypatch.setattr(hook, "patch_json", fake_patch_json)

    with caplog.at_level(logging.WARNING):
        hook._sync_console_config(123, {"version": "1"})

    assert "Failed to sync console_config for assistant 123" in caplog.text


def _session_details():
    return SimpleNamespace(
        assistant=SimpleNamespace(agent_id=123),
        user=SimpleNamespace(id="user-1"),
        org_id=7,
        team_ids=[9],
    )


def test_startup_hook_returns_config_without_hydration_by_default(monkeypatch):
    resolved = SimpleNamespace(mcp_configs=[], console_config=None)
    calls: list[str] = []

    monkeypatch.setattr(
        startup_config,
        "resolve_startup_spec",
        lambda identity: resolved,
    )
    monkeypatch.setattr(
        startup_config,
        "expand_startup_integrations",
        lambda value: value,
    )
    monkeypatch.setattr(
        runtime_state,
        "materialize_runtime_state",
        lambda *args, **kwargs: calls.append("materialize"),
    )
    monkeypatch.setattr(
        startup_config,
        "build_actor_startup_config",
        lambda value: {"actor_kwargs": {"guidelines": "ready"}},
    )

    result = hook.startup_hook(None, _session_details())

    assert result == {"actor_kwargs": {"guidelines": "ready"}}
    assert calls == []


def test_startup_hook_runs_blocking_hydration_when_explicitly_enabled(monkeypatch):
    resolved = SimpleNamespace(mcp_configs=[], console_config=None)
    calls: list[str] = []

    monkeypatch.setenv("UNITY_DEPLOY_WAKE_HYDRATION_MODE", "blocking")
    monkeypatch.setattr(
        startup_config,
        "resolve_startup_spec",
        lambda identity: resolved,
    )
    monkeypatch.setattr(
        startup_config,
        "expand_startup_integrations",
        lambda value: value,
    )
    monkeypatch.setattr(
        runtime_state,
        "materialize_runtime_state",
        lambda *args, **kwargs: calls.append("materialize"),
    )
    monkeypatch.setattr(
        startup_config,
        "build_actor_startup_config",
        lambda value: {"actor_kwargs": {"guidelines": "ready"}},
    )

    result = hook.startup_hook(None, _session_details())

    assert result == {"actor_kwargs": {"guidelines": "ready"}}
    assert calls == ["materialize"]
