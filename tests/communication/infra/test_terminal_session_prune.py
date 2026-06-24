from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


@pytest.fixture
def client():
    from communication.infra.views import router

    app = FastAPI()
    app.include_router(router, prefix="/infra")
    return TestClient(app)


def _session(
    assistant_id: str,
    *,
    phase: str,
    created_hours_ago: int,
    desired_state: str = "Stopped",
    binding: dict | None = None,
    deleting: bool = False,
    condition_transition_minutes_ago: int | None = None,
) -> dict:
    created_at = datetime.now(timezone.utc) - timedelta(hours=created_hours_ago)
    metadata = {
        "name": f"assistant-session-{assistant_id}",
        "creationTimestamp": created_at.isoformat().replace("+00:00", "Z"),
    }
    if deleting:
        metadata["deletionTimestamp"] = (
            datetime.now(timezone.utc)
            .isoformat()
            .replace(
                "+00:00",
                "Z",
            )
        )
    status = {"phase": phase}
    if condition_transition_minutes_ago is not None:
        transitioned_at = datetime.now(timezone.utc) - timedelta(
            minutes=condition_transition_minutes_ago,
        )
        status["conditions"] = [
            {
                "type": "Active",
                "status": "False",
                "reason": phase,
                "message": phase,
                "lastTransitionTime": transitioned_at.isoformat().replace(
                    "+00:00",
                    "Z",
                ),
            },
        ]
    status["observedActivationId"] = "act-1"
    if binding is not None:
        status["binding"] = binding
    return {
        "metadata": metadata,
        "spec": {
            "assistantId": assistant_id,
            "desiredState": desired_state,
            "activationId": "act-1",
        },
        "status": status,
    }


def _active_job(name: str = "unity-job-live"):
    job = MagicMock()
    job.metadata.name = name
    job.metadata.deletion_timestamp = None
    job.status.active = 1
    return job


def test_prune_terminal_sessions_deletes_old_terminal_crs_with_no_runtime(client):
    listed_sessions = [
        _session("100", phase="Released", created_hours_ago=72),
        _session(
            "101",
            phase="Failed",
            created_hours_ago=48,
            desired_state="Running",
        ),
        _session("102", phase="Released", created_hours_ago=2),
        _session(
            "103",
            phase="Released",
            created_hours_ago=72,
            binding={"id": "binding-1"},
        ),
        _session("104", phase="Released", created_hours_ago=72, deleting=True),
        _session("105", phase="Active", created_hours_ago=72),
    ]
    session_by_assistant_id = {
        str(session["spec"]["assistantId"]): session for session in listed_sessions
    }
    custom_api = MagicMock()
    custom_api.list_namespaced_custom_object.return_value = {"items": listed_sessions}
    batch_api = MagicMock()
    batch_api.list_namespaced_job.return_value = SimpleNamespace(items=[])

    def _patch_session_spec(_custom_api, _namespace, assistant_id, *, desired_state):
        updated = deepcopy(session_by_assistant_id[assistant_id])
        updated["spec"]["desiredState"] = desired_state
        session_by_assistant_id[assistant_id] = updated
        return updated

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(batch_api, MagicMock(), MagicMock(), MagicMock()),
        ),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=custom_api,
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            side_effect=lambda *_args: session_by_assistant_id.get(str(_args[-1])),
        ),
        patch(
            "communication.infra.views.split_binding_runtime_vms",
            return_value=([], []),
        ),
        patch("communication.infra.views.find_vm_with_disk", return_value=None),
        patch(
            "communication.infra.views.patch_assistant_session_spec",
            side_effect=_patch_session_spec,
        ),
        patch(
            "communication.infra.views.delete_assistant_session",
            return_value=True,
        ) as mock_delete_assistant_session,
    ):
        response = client.post(
            "/infra/sessions/prune-terminal",
            params={"retention_hours": 24, "limit": 10},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["terminal_sessions_found"] == 5
    assert body["prune_candidates"] == 3
    assert body["ghost_healed_count"] == 1
    assert body["ghost_healed_assistant_ids"] == ["101"]
    assert body["deleted_count"] == 2
    assert body["deleted_assistant_ids"] == ["100", "101"]
    assert body["remaining_candidates"] == 0
    assert body["skip_reasons"] == {
        "within_retention": 1,
        "already_terminating": 1,
        "runtime_resources_present": 1,
    }
    assert [call.args[2] for call in mock_delete_assistant_session.call_args_list] == [
        "100",
        "101",
    ]


def test_prune_terminal_sessions_skips_sessions_with_live_runtime_artifacts(client):
    listed_session = _session("200", phase="Released", created_hours_ago=48)
    custom_api = MagicMock()
    custom_api.list_namespaced_custom_object.return_value = {"items": [listed_session]}
    batch_api = MagicMock()
    batch_api.list_namespaced_job.return_value = SimpleNamespace(items=[_active_job()])

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(batch_api, MagicMock(), MagicMock(), MagicMock()),
        ),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=custom_api,
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=listed_session,
        ),
        patch(
            "communication.infra.views.split_binding_runtime_vms",
            return_value=([], []),
        ),
        patch("communication.infra.views.find_vm_with_disk", return_value=None),
        patch(
            "communication.infra.views.delete_assistant_session",
            return_value=True,
        ) as mock_delete_assistant_session,
    ):
        response = client.post(
            "/infra/sessions/prune-terminal",
            params={"retention_hours": 24, "limit": 10},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["deleted_count"] == 0
    assert body["skip_reasons"] == {"runtime_resources_present": 1}
    mock_delete_assistant_session.assert_not_called()


def test_prune_terminal_sessions_keeps_recent_failed_running_ghosts_in_grace_window(
    client,
):
    listed_session = _session(
        "210",
        phase="Failed",
        created_hours_ago=48,
        desired_state="Running",
        condition_transition_minutes_ago=2,
    )
    custom_api = MagicMock()
    custom_api.list_namespaced_custom_object.return_value = {"items": [listed_session]}
    batch_api = MagicMock()
    batch_api.list_namespaced_job.return_value = SimpleNamespace(items=[])

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(batch_api, MagicMock(), MagicMock(), MagicMock()),
        ),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=custom_api,
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=listed_session,
        ),
        patch(
            "communication.infra.views.split_binding_runtime_vms",
            return_value=([], []),
        ),
        patch("communication.infra.views.find_vm_with_disk", return_value=None),
        patch(
            "communication.infra.views.patch_assistant_session_spec",
            side_effect=AssertionError("ghost should not heal within grace"),
        ),
        patch(
            "communication.infra.views.delete_assistant_session",
            return_value=True,
        ) as mock_delete_assistant_session,
    ):
        response = client.post(
            "/infra/sessions/prune-terminal",
            params={"retention_hours": 24, "limit": 10},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ghost_healed_count"] == 0
    assert body["deleted_count"] == 0
    assert body["skip_reasons"] == {"desired_state_running": 1}
    mock_delete_assistant_session.assert_not_called()


@pytest.mark.parametrize(
    ("fresh_session", "expected_reason"),
    [
        (_session("300", phase="Active", created_hours_ago=72), "no_longer_terminal"),
        (
            _session("300", phase="Released", created_hours_ago=72, deleting=True),
            "already_terminating",
        ),
    ],
)
def test_prune_terminal_sessions_rechecks_candidate_before_delete(
    client,
    fresh_session,
    expected_reason,
):
    listed_session = _session("300", phase="Released", created_hours_ago=72)
    custom_api = MagicMock()
    custom_api.list_namespaced_custom_object.return_value = {"items": [listed_session]}
    batch_api = MagicMock()
    batch_api.list_namespaced_job.return_value = SimpleNamespace(items=[])

    with (
        patch(
            "communication.infra.views._get_k8s_clients",
            new_callable=AsyncMock,
            return_value=(batch_api, MagicMock(), MagicMock(), MagicMock()),
        ),
        patch(
            "communication.infra.views.get_custom_objects_api",
            return_value=custom_api,
        ),
        patch(
            "communication.infra.views.get_assistant_session",
            return_value=fresh_session,
        ),
        patch(
            "communication.infra.views.split_binding_runtime_vms",
            return_value=([], []),
        ),
        patch("communication.infra.views.find_vm_with_disk", return_value=None),
        patch(
            "communication.infra.views.delete_assistant_session",
            return_value=True,
        ) as mock_delete_assistant_session,
    ):
        response = client.post(
            "/infra/sessions/prune-terminal",
            params={"retention_hours": 24, "limit": 10},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["deleted_count"] == 0
    assert body["skip_reasons"] == {expected_reason: 1}
    mock_delete_assistant_session.assert_not_called()
