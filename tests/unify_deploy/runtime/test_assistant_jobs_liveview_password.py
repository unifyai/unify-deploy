"""Unit tests for liveview_password plumbing through the Unity runtime backend."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import unify_deploy.runtime.assistant_jobs_api as api
import unify_deploy.runtime.assistant_jobs_backend as backend
from unify_deploy.runtime.assistant_jobs_backend import HostedAssistantJobsBackend


def test_patch_liveview_via_infra_omits_password_by_default() -> None:
    resp = MagicMock(ok=True)
    with patch.object(api.requests, "patch", return_value=resp) as patch_call:
        result = api.patch_liveview_via_infra(
            "https://comms.test",
            "key",
            assistant_id="42",
            job_name="job-abc",
            liveview_url="https://vm.example/desktop",
        )

    assert result is True
    payload = patch_call.call_args.kwargs["json"]
    assert "liveview_password" not in payload


def test_patch_liveview_via_infra_includes_password_when_set() -> None:
    resp = MagicMock(ok=True)
    with patch.object(api.requests, "patch", return_value=resp) as patch_call:
        api.patch_liveview_via_infra(
            "https://comms.test",
            "key",
            assistant_id="42",
            job_name="job-abc",
            liveview_url="https://vm.example/desktop",
            liveview_password="s3cr3t",
        )

    payload = patch_call.call_args.kwargs["json"]
    assert payload["liveview_password"] == "s3cr3t"


@pytest.fixture()
def primed_backend(monkeypatch: pytest.MonkeyPatch):
    """Make update_liveview_url's guards pass without touching real config."""
    monkeypatch.setattr(
        backend.SETTINGS.conversation,
        "COMMS_URL",
        "https://comms.test",
    )
    monkeypatch.setattr(backend.SETTINGS.conversation, "JOB_NAME", "job-abc")
    monkeypatch.setattr(backend.SESSION_DETAILS, "unify_key", "unify-key-1")
    monkeypatch.setattr(backend._log_created, "wait", lambda timeout=None: True)


def test_update_liveview_url_three_arg_call_still_works(primed_backend) -> None:
    """Callers built against the old 3-positional-arg signature must not raise."""
    with patch.object(
        backend,
        "patch_liveview_via_infra",
        return_value=True,
    ) as patch_call:
        backend.update_liveview_url("42", "user-1", "https://vm.example/desktop")

    assert patch_call.call_args.kwargs["liveview_password"] is None


def test_update_liveview_url_forwards_password_when_set(primed_backend) -> None:
    with patch.object(
        backend,
        "patch_liveview_via_infra",
        return_value=True,
    ) as patch_call:
        backend.update_liveview_url(
            "42",
            "user-1",
            "https://vm.example/desktop",
            "s3cr3t",
        )

    assert patch_call.call_args.kwargs["liveview_password"] == "s3cr3t"


def test_hosted_backend_forwards_password_positionally(primed_backend) -> None:
    """The unify pod calls HostedAssistantJobsBackend.update_liveview_url with a 4th arg."""
    with patch.object(
        backend,
        "patch_liveview_via_infra",
        return_value=True,
    ) as patch_call:
        HostedAssistantJobsBackend().update_liveview_url(
            "42",
            "user-1",
            "https://vm.example/desktop",
            "s3cr3t",
        )

    assert patch_call.call_args.kwargs["liveview_password"] == "s3cr3t"
