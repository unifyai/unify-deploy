"""Unit tests for job-watcher offline task Job terminalization."""

from __future__ import annotations

import datetime
import importlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

WATCHER_DIR = Path(__file__).resolve().parents[3] / "base" / "scripts" / "job-watcher"


def _install_fake_kopf(monkeypatch) -> None:
    """Stub ``kopf`` so watcher imports without the operator dependency."""

    kopf = types.ModuleType("kopf")

    class OperatorSettings:
        def __init__(self):
            self.posting = types.SimpleNamespace(enabled=True)
            self.persistence = types.SimpleNamespace(finalizer="x")
            self.scanning = types.SimpleNamespace(disabled=False)

    def on_startup():
        def decorator(fn):
            return fn

        return decorator

    def on_event(*_args, **_kwargs):
        def decorator(fn):
            return fn

        return decorator

    def on_probe(*, id):  # noqa: A002
        def decorator(fn):
            return fn

        return decorator

    kopf.OperatorSettings = OperatorSettings
    kopf.on = types.SimpleNamespace(
        startup=on_startup,
        event=on_event,
        probe=on_probe,
    )
    monkeypatch.setitem(sys.modules, "kopf", kopf)


@pytest.fixture()
def watcher_module(monkeypatch):
    """Import watcher.py with required env vars and a clean module slot."""

    monkeypatch.setenv("UNITY_COMMS_URL", "https://comms.example")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin-key")
    monkeypatch.syspath_prepend(str(WATCHER_DIR))
    _install_fake_kopf(monkeypatch)
    sys.modules.pop("watcher", None)
    sys.modules.pop("assistant_jobs_api", None)
    return importlib.import_module("watcher")


def _job_event(
    *,
    terminal_type: str | None,
    assistant_id: str | None = "1406",
    run_key: str | None = "offline:rk",
    source_task_log_id: str | None = "555",
    active: int = 0,
    age_minutes: float = 0.0,
):
    now = datetime.datetime.now(datetime.timezone.utc)
    transition = (now - datetime.timedelta(minutes=age_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )
    conditions = []
    if terminal_type is not None:
        conditions.append(
            {
                "type": terminal_type,
                "status": "True",
                "lastTransitionTime": transition,
            },
        )
    annotations = {}
    if run_key is not None:
        annotations["unify.ai/task-execution-key"] = run_key
    if source_task_log_id is not None:
        annotations["unify.ai/source-task-log-id"] = source_task_log_id
    labels = {"app": "unity-task-execution"}
    if assistant_id is not None:
        labels["assistant-id"] = assistant_id
    return {
        "object": {
            "metadata": {
                "name": "unity-task-execution-abc",
                "labels": labels,
                "annotations": annotations,
            },
            "status": {"active": active, "conditions": conditions},
        },
    }


def test_offline_handler_posts_terminal_on_failed(watcher_module):
    with patch.object(
        watcher_module,
        "notify_offline_task_job_terminal",
    ) as mock_notify:
        watcher_module.on_offline_task_job_event(_job_event(terminal_type="Failed"))

    mock_notify.assert_called_once_with(
        watcher_module.COMMS_URL,
        watcher_module.ADMIN_KEY,
        assistant_id="1406",
        run_key="offline:rk",
        source_task_log_id=555,
        job_name="unity-task-execution-abc",
        terminal_type="Failed",
    )


def test_offline_handler_skips_incomplete_annotations(watcher_module):
    with patch.object(
        watcher_module,
        "notify_offline_task_job_terminal",
    ) as mock_notify:
        watcher_module.on_offline_task_job_event(
            _job_event(terminal_type="Failed", source_task_log_id=None),
        )

    mock_notify.assert_not_called()


def test_offline_handler_ignores_non_terminal(watcher_module):
    with patch.object(
        watcher_module,
        "notify_offline_task_job_terminal",
    ) as mock_notify:
        watcher_module.on_offline_task_job_event(
            _job_event(terminal_type=None, active=1),
        )

    mock_notify.assert_not_called()


def test_offline_handler_skips_stale_events(watcher_module):
    with patch.object(
        watcher_module,
        "notify_offline_task_job_terminal",
    ) as mock_notify:
        watcher_module.on_offline_task_job_event(
            _job_event(terminal_type="Complete", age_minutes=10),
        )

    mock_notify.assert_not_called()


def test_notify_offline_task_job_terminal_posts_comms(monkeypatch):
    monkeypatch.syspath_prepend(str(WATCHER_DIR))
    sys.modules.pop("assistant_jobs_api", None)
    api = importlib.import_module("assistant_jobs_api")

    response = MagicMock()
    response.ok = True
    response.text = '{"success": true}'
    with patch.object(api.requests, "post", return_value=response) as mock_post:
        ok = api.notify_offline_task_job_terminal(
            "https://comms.example",
            "admin-key",
            assistant_id="1406",
            run_key="rk",
            source_task_log_id=555,
            job_name="unity-task-execution-abc",
            terminal_type="Failed",
        )

    assert ok is True
    mock_post.assert_called_once()
    assert mock_post.call_args.args[0].endswith("/infra/offline-task/job-terminal")
    assert mock_post.call_args.kwargs["json"]["source_task_log_id"] == 555
