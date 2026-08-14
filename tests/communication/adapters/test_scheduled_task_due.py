"""Unit tests for the scheduled task due adapters endpoint."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from adapters.main import app


def _assistant_data() -> dict:
    return {
        "assistant_id": "assistant-123",
        "user_id": "user-123",
        "api_key": "user-api-key",
        "user_first_name": "Test",
        "user_surname": "User",
        "user_email": "user@example.com",
        "assistant_first_name": "Test",
        "assistant_surname": "Assistant",
        "assistant_age": "25",
        "assistant_nationality": "US",
        "assistant_about": "Test assistant",
        "assistant_timezone": "UTC",
        "assistant_email": "assistant@example.com",
        "user_number": "+1234567890",
        "assistant_number": "+1987654321",
        "user_whatsapp_number": "+1234567890",
        "assistant_whatsapp_number": "+1987654321",
        "assistant_discord_bot_id": "",
        "voice_provider": "elevenlabs",
        "voice_id": "voice-123",
        "desktop_mode": "none",
        "user_desktops": [],
        "team_ids": [],
        "org_id": None,
        "deploy_env": "staging",
        "is_coordinator": False,
        "is_local": False,
        "self_contact_id": 42,
        "boss_contact_id": 43,
    }


def _task_due_payload() -> dict:
    return {
        "assistant_id": "assistant-123",
        "task_id": 101,
        "source_task_log_id": 555,
        "revision": "rev-123",
        "scheduled_for": "2026-04-10T09:00:00+00:00",
        "delivery": "live",
        "wake": "scheduled",
        "task_label": "Morning briefing",
        "task_summary": "Prepare the morning update before the user checks in.",
        "visibility_policy": "silent_by_default",
        "recurrence_hint": "recurring",
    }


def test_scheduled_task_due_attaches_wake_reason_for_cold_start():
    """Cold-start deliveries should attach wake reasons without live publish."""

    client = TestClient(app)
    start_response = MagicMock(status_code=200)
    start_response.json.return_value = {
        "success": True,
        "activation_id": "activation-1",
        "reused_active_session": False,
        "active_session_already_running": False,
    }

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.get_assistant", return_value=_assistant_data()),
        patch("adapters.main.uses_local_unity_runtime", return_value=False),
        patch(
            "adapters.main.dispatch_unity_start_intent",
            return_value=start_response,
        ) as mock_dispatch,
        patch("adapters.main._publish_unity_system_event") as mock_publish,
    ):
        response = client.post(
            "/scheduled/tasks/due",
            headers={"Authorization": "Bearer test-admin-key"},
            json=_task_due_payload(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "attached_to_startup"
    mock_publish.assert_not_called()
    assert mock_dispatch.call_args.args[1] == "api_message"
    wake_reasons = mock_dispatch.call_args.kwargs["wake_reasons"]
    assert wake_reasons == [
        {
            "type": "task_due",
            "task_id": 101,
            "source_task_log_id": 555,
            "revision": "rev-123",
            "scheduled_for": "2026-04-10T09:00:00+00:00",
            "delivery": "live",
            "requires_filesystem": False,
            "requires_computer": False,
            "wake": "scheduled",
            "task_label": "Morning briefing",
            "task_summary": "Prepare the morning update before the user checks in.",
            "visibility_policy": "silent_by_default",
            "recurrence_hint": "recurring",
        },
    ]


def test_scheduled_task_due_publishes_event_for_running_session():
    """Active sessions should receive a live `task_due` system event."""

    client = TestClient(app)
    start_response = MagicMock(status_code=200)
    start_response.json.return_value = {
        "success": True,
        "activation_id": "activation-2",
        "reused_active_session": True,
        "active_session_already_running": True,
    }

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.get_assistant", return_value=_assistant_data()),
        patch("adapters.main.uses_local_unity_runtime", return_value=False),
        patch(
            "adapters.main.dispatch_unity_start_intent",
            return_value=start_response,
        ),
        patch("adapters.main._publish_unity_system_event") as mock_publish,
    ):
        response = client.post(
            "/scheduled/tasks/due",
            headers={"Authorization": "Bearer test-admin-key"},
            json=_task_due_payload(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "published_to_active_session"
    mock_publish.assert_called_once()
    assert mock_publish.call_args.kwargs["event_type"] == "task_due"
    assert (
        mock_publish.call_args.kwargs["message"]
        == "Scheduled task 'Morning briefing' became due at 2026-04-10T09:00:00+00:00."
    )
    assert mock_publish.call_args.kwargs["extra_event_fields"]["task_id"] == 101
    assert (
        mock_publish.call_args.kwargs["extra_event_fields"]["task_label"]
        == "Morning briefing"
    )


def test_scheduled_task_due_skips_deleted_assistant():
    """Missing assistants should ack the delivery without retries."""

    client = TestClient(app)

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.get_assistant", return_value={"assistant_id": None}),
    ):
        response = client.post(
            "/scheduled/tasks/due",
            headers={"Authorization": "Bearer test-admin-key"},
            json=_task_due_payload(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "skipped"
    assert body["reason"] == "assistant_not_found"


def test_scheduled_task_due_rejects_revoked_team_destination():
    """Revoked shared due delivery should ack without waking the assistant."""

    client = TestClient(app)
    assistant_data = _assistant_data()
    assistant_data["team_summaries"] = [
        {
            "team_id": 7,
            "name": "Revoked",
            "description": "Revoked workspace retained only in stale display metadata.",
        },
    ]

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.get_assistant", return_value=assistant_data),
        patch("adapters.main.dispatch_unity_start_intent") as mock_dispatch,
        patch("adapters.main._publish_unity_system_event") as mock_publish,
    ):
        response = client.post(
            "/scheduled/tasks/due",
            headers={"Authorization": "Bearer test-admin-key"},
            json={**_task_due_payload(), "destination": "team:7"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "status": "skipped",
        "reason": "destination_membership_revoked",
    }
    mock_dispatch.assert_not_called()
    mock_publish.assert_not_called()


def test_scheduled_task_due_carries_authorized_team_destination():
    """Authorized shared due deliveries should carry destination in wake reasons."""

    client = TestClient(app)
    start_response = MagicMock(status_code=200)
    start_response.json.return_value = {
        "success": True,
        "activation_id": "activation-1",
        "active_session_already_running": False,
    }
    assistant_data = _assistant_data()
    assistant_data["team_ids"] = [7]

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.get_assistant", return_value=assistant_data),
        patch("adapters.main.uses_local_unity_runtime", return_value=False),
        patch(
            "adapters.main.dispatch_unity_start_intent",
            return_value=start_response,
        ) as mock_dispatch,
        patch("adapters.main._publish_unity_system_event"),
    ):
        response = client.post(
            "/scheduled/tasks/due",
            headers={"Authorization": "Bearer test-admin-key"},
            json={**_task_due_payload(), "destination": "team:7"},
        )

    assert response.status_code == 200
    wake_reasons = mock_dispatch.call_args.kwargs["wake_reasons"]
    assert wake_reasons[0]["destination"] == "team:7"


def test_scheduled_task_due_sets_desktop_required_for_resource_flags():
    """Resource-flagged due wakes should force desktop_required on cold start."""

    client = TestClient(app)
    start_response = MagicMock(status_code=200)
    start_response.json.return_value = {
        "success": True,
        "activation_id": "activation-1",
        "active_session_already_running": False,
    }

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.get_assistant", return_value=_assistant_data()),
        patch("adapters.main.uses_local_unity_runtime", return_value=False),
        patch(
            "adapters.main.dispatch_unity_start_intent",
            return_value=start_response,
        ) as mock_dispatch,
        patch("adapters.main._publish_unity_system_event"),
    ):
        response = client.post(
            "/scheduled/tasks/due",
            headers={"Authorization": "Bearer test-admin-key"},
            json={**_task_due_payload(), "requires_computer": True},
        )

    assert response.status_code == 200
    assert mock_dispatch.call_args.kwargs["desktop_required"] is True
    wake_reasons = mock_dispatch.call_args.kwargs["wake_reasons"]
    assert wake_reasons[0]["requires_computer"] is True
    assert wake_reasons[0]["requires_filesystem"] is False


def test_scheduled_task_due_skips_account_without_runtime_access():
    """A schedule cannot start runtime work the account may not pay for."""

    client = TestClient(app)

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.get_assistant", return_value=_assistant_data()),
        patch("adapters.main.assistant_may_start_runtime", return_value=False),
        patch("adapters.main.uses_local_unity_runtime", return_value=False),
        patch("adapters.main.dispatch_unity_start_intent") as mock_dispatch,
        patch("adapters.main._publish_unity_system_event") as mock_publish,
    ):
        response = client.post(
            "/scheduled/tasks/due",
            headers={"Authorization": "Bearer test-admin-key"},
            json=_task_due_payload(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "status": "skipped",
        "reason": "payment_required",
    }
    # The local-runtime branch publishes instead of dispatching, so both
    # have to stay untouched for the skip to mean the work never began.
    mock_dispatch.assert_not_called()
    mock_publish.assert_not_called()


def test_scheduled_task_due_starts_when_runtime_access_is_unknown():
    """An unreachable ledger must not stop a paying customer's schedule."""

    client = TestClient(app)
    start_response = MagicMock(status_code=200)
    start_response.json.return_value = {
        "success": True,
        "activation_id": "activation-1",
        "active_session_already_running": False,
    }

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.get_assistant", return_value=_assistant_data()),
        # What the helper returns when Orchestra cannot be reached at all.
        patch("adapters.main.assistant_may_start_runtime", return_value=True),
        patch("adapters.main.uses_local_unity_runtime", return_value=False),
        patch(
            "adapters.main.dispatch_unity_start_intent",
            return_value=start_response,
        ) as mock_dispatch,
        patch("adapters.main._publish_unity_system_event"),
    ):
        response = client.post(
            "/scheduled/tasks/due",
            headers={"Authorization": "Bearer test-admin-key"},
            json=_task_due_payload(),
        )

    assert response.status_code == 200
    assert response.json()["status"] != "skipped"
    mock_dispatch.assert_called_once()
