from __future__ import annotations

import logging

from unity_deploy import hook
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
