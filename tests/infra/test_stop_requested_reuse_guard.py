"""``/infra/job/start`` must refuse sessions with a pending stop.

The Orchestra-side membership-change runtime barrier already patches the
AssistantSession to ``desiredState=Stopped`` and persists ``suspendIntent``
against the current binding before committing a Hive mutation. The controller
needs one reconciliation loop to move the session into ``Released``; a wake
arriving in that window must not be allowed to reuse the still-``Active``
session. :func:`assistant_session_stop_requested` catches that case and the
start-job handler raises ``409 AssistantSession stop is in progress`` before
any bootstrap work is attempted.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from communication.infra.assistant_sessions import (
    DESIRED_STATE_STOPPED,
    SUSPEND_INTENT_STOP,
    assistant_session_stop_requested,
    build_binding,
    build_suspend_intent,
)


@pytest.fixture
def client():
    from communication.infra.views import router

    app = FastAPI()
    app.include_router(router, prefix="/infra")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _mock_idle_pool_replenishment():
    with patch(
        "communication.infra.views.schedule_idle_job_pool_replenishment",
        return_value=True,
    ):
        yield


def _start_job_payload() -> dict[str, str]:
    return {
        "api_key": "test-api-key",
        "medium": "phone",
        "assistant_id": "assistant-123",
        "user_id": "user-123",
        "user_first_name": "Test",
        "user_surname": "User",
        "user_email": "user@example.com",
        "assistant_first_name": "Updated",
        "assistant_surname": "Assistant",
        "assistant_age": "25",
        "assistant_nationality": "US",
        "assistant_about": "pending-stop session payload",
        "assistant_timezone": "UTC",
        "user_number": "+1234567890",
        "assistant_number": "+1987654321",
        "assistant_email": "assistant@example.com",
        "user_whatsapp_number": "+1234567890",
        "assistant_whatsapp_number": "+1987654321",
        "voice_provider": "elevenlabs",
        "voice_id": "voice-123",
        "desktop_mode": "ubuntu",
        "desktop_url": "",
        "user_desktop_mode": "",
        "user_desktop_filesys_sync": "false",
        "user_desktop_url": "",
        "is_coordinator": "false",
        "demo_id": "",
        "team_ids": "[]",
        "org_id": "",
    }


def _control_plane_ready_patch():
    return patch(
        "communication.infra.views._assistant_session_control_plane_ready",
        new_callable=AsyncMock,
        return_value=(True, None),
    )


def _session(*, desired_state: str, suspend_intent: dict | None) -> dict:
    binding = build_binding(binding_id="binding-current")
    status: dict = {"phase": "Active", "binding": binding}
    if suspend_intent is not None:
        status["suspendIntent"] = suspend_intent
    return {
        "metadata": {"name": "assistant-session-assistant-123"},
        "spec": {
            "assistantId": "assistant-123",
            "activationId": "activation-existing",
            "startupSecretRef": "assistant-session-bootstrap-assistant-123",
            "userId": "stale-user",
            "medium": "email",
            "desiredState": desired_state,
            "desktop": {"mode": "ubuntu", "required": True},
        },
        "status": status,
    }


def test_stop_requested_predicate_detects_pending_stop_on_current_binding():
    """The predicate must fire when desiredState=Stopped and suspendIntent=stop."""

    suspend_intent = build_suspend_intent(
        binding_id="binding-current",
        intent=SUSPEND_INTENT_STOP,
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    session = _session(
        desired_state=DESIRED_STATE_STOPPED,
        suspend_intent=suspend_intent,
    )
    assert assistant_session_stop_requested(session) is True


def test_stop_requested_predicate_ignores_stop_on_different_binding():
    """Stops recorded against a previous binding must not gate the new one."""

    suspend_intent = build_suspend_intent(
        binding_id="binding-previous",
        intent=SUSPEND_INTENT_STOP,
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    session = _session(
        desired_state=DESIRED_STATE_STOPPED,
        suspend_intent=suspend_intent,
    )
    assert assistant_session_stop_requested(session) is False


def test_stop_requested_predicate_false_when_desired_state_is_running():
    """A running session is not stop-requested even if suspendIntent lingers."""

    suspend_intent = build_suspend_intent(
        binding_id="binding-current",
        intent=SUSPEND_INTENT_STOP,
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    session = _session(desired_state="Running", suspend_intent=suspend_intent)
    assert assistant_session_stop_requested(session) is False


def test_start_job_rejects_session_with_pending_stop(client):
    """``/infra/job/start`` must 409 before doing any bootstrap work."""

    suspend_intent = build_suspend_intent(
        binding_id="binding-current",
        intent=SUSPEND_INTENT_STOP,
        requested_at=datetime.now(timezone.utc).isoformat(),
    )
    existing_session = _session(
        desired_state=DESIRED_STATE_STOPPED,
        suspend_intent=suspend_intent,
    )

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(MagicMock(), MagicMock(), MagicMock(), MagicMock()),
        ),
        _control_plane_ready_patch(),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=MagicMock(),
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=existing_session,
        ),
        patch(
            "communication.infra.views.create_or_update_bootstrap_secret",
        ) as mock_bootstrap,
        patch(
            "communication.infra.views.create_or_update_assistant_session",
        ) as mock_session,
    ):
        response = client.post("/infra/job/start", data=_start_job_payload())

    assert response.status_code == 409
    assert response.json()["detail"] == "AssistantSession stop is in progress"
    mock_bootstrap.assert_not_called()
    mock_session.assert_not_called()


def test_start_job_does_not_reject_running_session_on_stop_requested_branch(client):
    """A Running session must not be rejected by the stop-requested branch.

    The request may still fail on unrelated grounds (e.g. bootstrap errors in
    test plumbing), but it must advance past the stop-requested 409 — the
    assertion is specifically that the stop-requested detail never surfaces.
    """

    existing_session = _session(desired_state="Running", suspend_intent=None)

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(MagicMock(), MagicMock(), MagicMock(), MagicMock()),
        ),
        _control_plane_ready_patch(),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=MagicMock(),
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=existing_session,
        ),
        patch(
            "communication.infra.views.create_or_update_bootstrap_secret",
            return_value="assistant-session-bootstrap-assistant-123",
        ),
        patch(
            "communication.infra.views.create_or_update_assistant_session",
            side_effect=lambda *a, **kw: existing_session,
        ),
    ):
        response = client.post("/infra/job/start", data=_start_job_payload())

    if response.status_code == 409:
        assert response.json().get("detail") != "AssistantSession stop is in progress"
