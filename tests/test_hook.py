from __future__ import annotations

import logging
from types import SimpleNamespace

from unity_deploy import hook
from unity_deploy import startup_config
from unity_deploy.runtime_reconcile import runner as runtime_runner
from unity_deploy.runtime_reconcile.status import RuntimeReconcileStatusHandle
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


def _resolved_startup_spec():
    return SimpleNamespace(
        contacts=[],
        guidance_dirs=[],
        knowledge=[],
        secrets=[],
        blacklist_dirs=[],
        function_dirs=[],
        venv_dirs=[],
        integrations=[],
        mcp_configs=[],
        console_config=None,
    )


def test_startup_hook_starts_runtime_reconcile_async_by_default(monkeypatch):
    resolved = _resolved_startup_spec()
    calls: list[tuple[str, str]] = []

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

    def fake_start_runtime_reconcile(cm, resolved, identity, *, mode, revision=None):
        calls.append((mode, identity.user_id))
        status = RuntimeReconcileStatusHandle()
        status.update(phase="starting")
        return SimpleNamespace(status=status)

    monkeypatch.setattr(
        runtime_runner,
        "start_runtime_reconcile",
        fake_start_runtime_reconcile,
    )
    monkeypatch.setattr(
        startup_config,
        "build_actor_startup_config",
        lambda value: {"actor_kwargs": {"guidelines": "ready"}},
    )

    result = hook.startup_hook(None, _session_details())

    assert "ready" in result["actor_kwargs"]["guidelines"]
    assert (
        "Some assistant setup is still finishing"
        in result["actor_kwargs"]["guidelines"]
    )
    assert calls == [("async", "user-1")]


def test_startup_hook_runs_blocking_runtime_reconcile_when_explicitly_enabled(
    monkeypatch,
):
    resolved = _resolved_startup_spec()
    calls: list[str] = []

    monkeypatch.setenv("UNITY_DEPLOY_RUNTIME_RECONCILE_MODE", "blocking")
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

    def fake_start_runtime_reconcile(cm, resolved, identity, *, mode, revision=None):
        calls.append(mode)
        status = RuntimeReconcileStatusHandle()
        status.update(phase="complete")
        return SimpleNamespace(status=status)

    monkeypatch.setattr(
        runtime_runner,
        "start_runtime_reconcile",
        fake_start_runtime_reconcile,
    )
    monkeypatch.setattr(
        startup_config,
        "build_actor_startup_config",
        lambda value: {"actor_kwargs": {"guidelines": "ready"}},
    )

    result = hook.startup_hook(None, _session_details())

    assert result == {"actor_kwargs": {"guidelines": "ready"}}
    assert calls == ["blocking"]


def test_startup_hook_surfaces_runtime_reconcile_scheduling_failure(
    monkeypatch,
    caplog,
):
    resolved = _resolved_startup_spec()
    cm = SimpleNamespace()

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

    def fake_start_runtime_reconcile(cm, resolved, identity, *, mode, revision=None):
        raise RuntimeError("thread scheduler unavailable")

    monkeypatch.setattr(
        runtime_runner,
        "start_runtime_reconcile",
        fake_start_runtime_reconcile,
    )
    monkeypatch.setattr(
        startup_config,
        "build_actor_startup_config",
        lambda value: {"actor_kwargs": {"guidelines": "ready"}},
    )

    with caplog.at_level(logging.ERROR):
        result = hook.startup_hook(cm, _session_details())

    status = cm.deployment_runtime_reconcile_status.snapshot()
    assert status.current_phase == "failed"
    assert status.error == "thread scheduler unavailable"
    assert "Background assistant setup failed" in result["actor_kwargs"]["guidelines"]
    assert "Failed to schedule runtime reconciliation for assistant 123" in caplog.text
