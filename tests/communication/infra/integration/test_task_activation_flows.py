"""End-to-end task activation flow tests against deployed services."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import time
from typing import Any

from google.cloud import pubsub_v1
import pytest
import requests

from communication.infra.assistant_sessions import read_bootstrap_secret

from .conftest import (
    ADAPTERS_URL,
    ADMIN_KEY,
    NAMESPACE,
    ORCHESTRA_URL,
    UNIFY_KEY,
    _assistant_readiness_snapshot,
    _create_test_assistant,
    _delete_test_assistant,
    cleanup_assistant_jobs,
    expire_test_assistant_records,
    get_assistant_session,
    poll_until,
    pull_outbound_messages,
    replenish_pool,
    send_test_message,
    wait_for_assistant_container_ready,
    wait_for_container_running,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

TASK_MACHINE_PROJECT_NAME = "Assistants"
TASK_DUE_LEAD_SECONDS = 60
TASK_FLOW_TIMEOUT_SECONDS = 240
RUNTIME_INIT_GRACE_SECONDS = 15
SILENT_START_OBSERVATION_SECONDS = 20
OFFLINE_NO_WAKE_GRACE_SECONDS = 30
# Offline integration cases only need a numeric entrypoint to satisfy the task
# contract; they do not assert the downstream offline function behavior itself.
TEST_OFFLINE_FUNCTION_ID = 777


def _orchestra_headers(api_key: str) -> dict[str, str]:
    """Return the auth headers for Orchestra requests."""

    return {"Authorization": f"Bearer {api_key}"}


def _task_context_name(assistant_data: dict[str, Any]) -> str:
    """Return the assistant-scoped task context name."""

    return f"{assistant_data['user_id']}/{assistant_data['assistant_id']}/Tasks"


def _activation_context_name(assistant_data: dict[str, Any]) -> str:
    """Return the assistant-scoped activation context name."""

    return f"{_task_context_name(assistant_data)}/Activations"


def _with_mutable_explicit_types(entries: dict[str, Any]) -> dict[str, Any]:
    """Mark task rows mutable so Orchestra accepts the integration payload."""

    enriched = deepcopy(entries)
    explicit_types = enriched.get("explicit_types")
    if not isinstance(explicit_types, dict):
        explicit_types = {}
    for key in list(enriched.keys()):
        if key in {"explicit_types", "infer_untyped_fields"}:
            continue
        field_meta = explicit_types.get(key)
        if not isinstance(field_meta, dict):
            field_meta = {}
        field_meta.setdefault("mutable", True)
        explicit_types[key] = field_meta
    enriched["explicit_types"] = explicit_types
    return enriched


def _wakeup(assistant_id: str) -> tuple[requests.Response, float]:
    """POST `/assistant/wakeup` and return the response with elapsed seconds."""

    t0 = time.monotonic()
    response = requests.post(
        f"{ADAPTERS_URL}/assistant/wakeup",
        data={"assistant_id": assistant_id},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=30,
    )
    return response, time.monotonic() - t0


def _task_id_seed() -> int:
    """Return a timestamp-based task id seed unique enough for test runs."""

    return int(time.time() * 1000) % 1_000_000_000


def _create_remote_task_assistant(batch_api) -> dict[str, Any]:
    """Create a quiet test assistant, then switch it to the deployed runtime lane."""

    assistant = _create_test_assistant(
        int(time.time() * 1000) % 1_000_000,
        desktop_mode=None,
    )
    assistant_id = str(assistant["assistant_id"])
    try:
        response = requests.patch(
            f"{ORCHESTRA_URL}/assistant/{assistant_id}/config",
            json={"is_local": False},
            headers={"Authorization": f"Bearer {UNIFY_KEY}"},
            timeout=30,
        )
        assert response.status_code == 200, (
            f"Failed to switch assistant {assistant_id} to the deployed runtime "
            f"lane: {response.status_code} {response.text}"
        )
        expire_test_assistant_records(assistant_id)
        cleanup_assistant_jobs(
            batch_api,
            [assistant_id],
            context="task-activation-setup",
        )
    except Exception:
        # The assistant already exists on Orchestra; delete it before
        # re-raising so a setup failure never leaks a paid runtime resource.
        _delete_test_assistant(assistant_id, batch_api)
        raise
    return assistant


def _scheduled_task_entries(
    assistant_data: dict[str, Any],
    *,
    task_id: int,
    start_at: str,
    offline: bool = False,
) -> dict[str, Any]:
    """Return one minimal scheduled task row for the given assistant."""

    entries = {
        "task_id": task_id,
        "instance_id": 0,
        "name": f"Integration scheduled task {task_id}",
        "description": "Quietly start this work when it becomes due.",
        "status": "scheduled",
        "priority": "normal",
        "_user_id": str(assistant_data["user_id"]),
        "_assistant_id": str(assistant_data["assistant_id"]),
        "schedule": {
            "prev_task": None,
            "next_task": None,
            "start_at": start_at,
        },
    }
    if offline:
        entries["offline"] = True
        entries["entrypoint"] = TEST_OFFLINE_FUNCTION_ID
    return _with_mutable_explicit_types(entries)


def _trigger_task_entries(
    assistant_data: dict[str, Any],
    *,
    task_id: int,
    medium: str = "unify_message",
    from_contact_ids: list[int] | None = None,
    offline: bool = False,
) -> dict[str, Any]:
    """Return one minimal triggerable task row for the given assistant."""

    entries = {
        "task_id": task_id,
        "instance_id": 0,
        "name": f"Integration trigger task {task_id}",
        "description": "Consider this task whenever the configured inbound arrives.",
        "status": "triggerable",
        "_user_id": str(assistant_data["user_id"]),
        "_assistant_id": str(assistant_data["assistant_id"]),
        "trigger": {
            "medium": medium,
            "from_contact_ids": list(from_contact_ids or [1]),
            "omit_contact_ids": [],
            "interrupt": True,
            "recurring": False,
        },
    }
    if offline:
        entries["offline"] = True
        entries["entrypoint"] = TEST_OFFLINE_FUNCTION_ID
    return _with_mutable_explicit_types(entries)


def _create_unity_task_log(
    assistant_data: dict[str, Any],
    entries: dict[str, Any],
) -> int:
    """Create one assistant task row through Orchestra and return its log id."""

    response = requests.post(
        f"{ORCHESTRA_URL}/logs",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "context": _task_context_name(assistant_data),
            "entries": entries,
        },
        headers=_orchestra_headers(assistant_data["api_key"]),
        timeout=30,
    )
    assert response.status_code == 200, (
        f"Failed creating task row in {_task_context_name(assistant_data)}: "
        f"{response.status_code} {response.text}"
    )
    return int(response.json()["log_event_ids"][0])


def _delete_unity_logs(assistant_data: dict[str, Any], log_ids: list[int]) -> None:
    """Delete task rows created by the test."""

    if not log_ids:
        return
    response = requests.delete(
        f"{ORCHESTRA_URL}/logs",
        json={
            "ids_and_fields": [[int(log_id), None] for log_id in log_ids],
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "context": _task_context_name(assistant_data),
        },
        headers=_orchestra_headers(assistant_data["api_key"]),
        timeout=30,
    )
    assert response.status_code == 200, (
        f"Failed deleting task rows {log_ids}: "
        f"{response.status_code} {response.text}"
    )


def _cleanup_unity_logs(assistant_data: dict[str, Any], log_ids: list[int]) -> None:
    """Best-effort cleanup so teardown never hides the original failure."""

    if not log_ids:
        return
    try:
        _delete_unity_logs(assistant_data, log_ids)
    except AssertionError as exc:
        print(f"[Cleanup] Failed deleting task rows {log_ids}: {exc}")


def _get_context_logs(
    assistant_data: dict[str, Any],
    context_name: str,
    *,
    filter_expr: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read logs from one Orchestra context, treating a missing context as empty."""

    params: dict[str, Any] = {
        "project_name": TASK_MACHINE_PROJECT_NAME,
        "context": context_name,
        "limit": limit,
    }
    if filter_expr:
        params["filter_expr"] = filter_expr
    response = requests.get(
        f"{ORCHESTRA_URL}/logs",
        params=params,
        headers=_orchestra_headers(assistant_data["api_key"]),
        timeout=30,
    )
    if response.status_code == 404:
        return []
    assert (
        response.status_code == 200
    ), f"Failed reading {context_name}: {response.status_code} {response.text}"
    return response.json().get("logs", [])


def _get_activation(
    assistant_data: dict[str, Any],
    task_id: int,
) -> dict[str, Any] | None:
    """Return the projected activation row for one logical task, if present."""

    assistant_id = str(assistant_data["assistant_id"])
    logs = _get_context_logs(
        assistant_data,
        _activation_context_name(assistant_data),
        limit=50,
    )
    for log in logs:
        entries = log.get("entries") or {}
        try:
            candidate_task_id = int(entries.get("task_id"))
        except (TypeError, ValueError):
            continue
        if candidate_task_id != task_id:
            continue
        if str(entries.get("assistant_id")) != assistant_id:
            continue
        return entries
    return None


def _wait_for_activation(
    assistant_data: dict[str, Any],
    task_id: int,
) -> dict[str, Any]:
    """Poll until Orchestra projects one activation row for the task."""

    return poll_until(
        lambda: _get_activation(assistant_data, task_id),
        timeout=60,
        interval=5,
        description=(
            f"Task activation for assistant {assistant_data['assistant_id']} "
            f"task {task_id}"
        ),
        failure_snapshot=lambda: {
            "tasks_context": _task_context_name(assistant_data),
            "activation_logs": _get_context_logs(
                assistant_data,
                _activation_context_name(assistant_data),
                limit=50,
            ),
        },
    )


def _wait_for_unity_log_substring(
    assistant_id: str,
    substring: str,
    *,
    batch_api,
    core_api,
    gce_client=None,
    timeout: float = 180,
) -> list[str]:
    """Poll Unity container logs until a line contains the requested substring."""

    return poll_until(
        lambda: (
            matches
            if (
                matches := [
                    line
                    for line in (
                        _assistant_readiness_snapshot(
                            assistant_id,
                            batch_api=batch_api,
                            core_api=core_api,
                            gce_client=gce_client,
                            # Push the substring into the log query itself;
                            # a newest-N sample of busy pods can otherwise
                            # displace the sought line indefinitely.
                            unity_log_contains=substring,
                        )
                        .get("recent_logs", {})
                        .get("unity", [])
                    )
                    if substring in line
                ]
            )
            else None
        ),
        timeout=timeout,
        interval=5,
        description=f"Unity logs for assistant {assistant_id} to contain {substring!r}",
        failure_snapshot=lambda: _assistant_readiness_snapshot(
            assistant_id,
            batch_api=batch_api,
            core_api=core_api,
            gce_client=gce_client,
        ),
    )


def _assert_no_outbound_messages(
    subscriber: pubsub_v1.SubscriberClient,
    assistant_id: str,
    *,
    duration: int = SILENT_START_OBSERVATION_SECONDS,
) -> None:
    """Assert the assistant stays silent on the outbound topic for a short window."""

    deadline = time.monotonic() + duration
    unexpected: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        timeout = max(1.0, min(5.0, deadline - time.monotonic()))
        messages = pull_outbound_messages(subscriber, assistant_id, timeout=timeout)
        if messages:
            unexpected.extend(messages)
            break
    assert not unexpected, (
        f"Expected scheduled start for assistant {assistant_id} to stay silent, "
        f"but observed outbound messages: {json.dumps(unexpected, indent=2)}"
    )


class TestTaskActivationFlows:
    """Real staging user-flow tests for scheduled, triggered, and offline tasks."""

    @pytest.mark.merge_gate
    def test_scheduled_due_wakes_sleeping_assistant_with_startup_reason_and_stays_silent(
        self,
        batch_api,
        core_api,
        gce_client,
        comms,
        pubsub_subscriber,
    ):
        """A due scheduled task should wake a sleeping assistant quietly."""

        assistant = _create_remote_task_assistant(batch_api)
        assistant_id = str(assistant["assistant_id"])
        created_log_ids: list[int] = []
        subscriber = pubsub_subscriber

        try:
            scheduled_for_dt = (
                datetime.now(UTC) + timedelta(seconds=TASK_DUE_LEAD_SECONDS)
            ).replace(microsecond=0)
            task_id = _task_id_seed()
            log_id = _create_unity_task_log(
                assistant,
                _scheduled_task_entries(
                    assistant,
                    task_id=task_id,
                    start_at=scheduled_for_dt.isoformat(),
                ),
            )
            created_log_ids.append(log_id)
            activation = _wait_for_activation(assistant, task_id)

            session = poll_until(
                lambda: get_assistant_session(comms, assistant_id),
                timeout=TASK_FLOW_TIMEOUT_SECONDS,
                interval=5,
                description=(
                    f"AssistantSession for sleeping assistant {assistant_id} "
                    f"after scheduled task {task_id} becomes due"
                ),
                failure_snapshot=lambda: {
                    "activation": _get_activation(assistant, task_id),
                    "assistant_readiness": _assistant_readiness_snapshot(
                        assistant_id,
                        batch_api=batch_api,
                        core_api=core_api,
                        gce_client=gce_client,
                    ),
                },
            )

            secret_name = str((session.get("spec") or {}).get("startupSecretRef") or "")
            assert secret_name, f"Expected startupSecretRef on session: {session}"
            bootstrap_payload = read_bootstrap_secret(core_api, NAMESPACE, secret_name)
            expected_reason = {
                "type": "task_due",
                "task_id": task_id,
                "source_task_log_id": log_id,
                "activation_revision": activation["activation_revision"],
                "scheduled_for": activation["next_due_at"],
                "execution_mode": "live",
                "source_type": "scheduled",
                "task_label": f"Integration scheduled task {task_id}",
                "task_summary": "Quietly start this work when it becomes due.",
                "visibility_policy": "silent_by_default",
                "recurrence_hint": "one_off",
            }
            assert expected_reason in list(
                bootstrap_payload.get("wake_reasons") or [],
            ), (
                f"Expected wake reason {expected_reason} in bootstrap payload "
                f"but saw {bootstrap_payload}"
            )

            wait_for_container_running(
                batch_api,
                assistant_id,
                timeout=180,
                interval=10,
            )
            wait_for_assistant_container_ready(
                assistant_id,
                batch_api=batch_api,
                core_api=core_api,
                gce_client=gce_client,
                timeout=300,
                interval=5,
            )
            _wait_for_unity_log_substring(
                assistant_id,
                f"Accepted due task {task_id}",
                batch_api=batch_api,
                core_api=core_api,
                gce_client=gce_client,
                # Staging services may cold-start after Cloud Run minScale=0.
                timeout=360,
            )
            _assert_no_outbound_messages(subscriber, assistant_id)
        finally:
            _cleanup_unity_logs(assistant, created_log_ids)
            cleanup_assistant_jobs(
                batch_api,
                [assistant_id],
                context="task-activation-e2e-scheduled-cold",
            )
            replenish_pool()
            _delete_test_assistant(assistant_id, batch_api)

    def test_scheduled_due_reaches_running_session_without_restarting_runtime(
        self,
        batch_api,
        core_api,
        gce_client,
        comms,
    ):
        """A due task should reach an already-running assistant without a restart."""

        assistant = _create_remote_task_assistant(batch_api)
        assistant_id = str(assistant["assistant_id"])
        created_log_ids: list[int] = []

        try:
            response, _elapsed = _wakeup(assistant_id)
            assert (
                response.status_code == 200
            ), f"Wakeup failed for assistant {assistant_id}: {response.status_code} {response.text}"
            wait_for_container_running(
                batch_api,
                assistant_id,
                timeout=180,
                interval=10,
            )
            ready_session = wait_for_assistant_container_ready(
                assistant_id,
                batch_api=batch_api,
                core_api=core_api,
                gce_client=gce_client,
                timeout=300,
                interval=5,
            )
            time.sleep(RUNTIME_INIT_GRACE_SECONDS)

            session_before = get_assistant_session(comms, assistant_id)
            assert session_before is not None
            secret_name = str(
                (session_before.get("spec") or {}).get("startupSecretRef") or "",
            )
            assert (
                secret_name
            ), f"Expected startupSecretRef on session: {session_before}"
            bootstrap_before = read_bootstrap_secret(core_api, NAMESPACE, secret_name)
            assert "wake_reasons" not in bootstrap_before

            binding_before = (session_before.get("status") or {}).get("binding") or {}
            job_name_before = (binding_before.get("jobRef") or {}).get("name")
            activation_id_before = str(
                (session_before.get("spec") or {}).get("activationId") or "",
            )
            assert (
                job_name_before
            ), f"Expected running job binding in session: {session_before}"
            assert ready_session.get("metadata", {}).get("name") == session_before.get(
                "metadata",
                {},
            ).get("name")

            scheduled_for_dt = (
                datetime.now(UTC) + timedelta(seconds=TASK_DUE_LEAD_SECONDS)
            ).replace(microsecond=0)
            task_id = _task_id_seed()
            created_log_ids.append(
                _create_unity_task_log(
                    assistant,
                    _scheduled_task_entries(
                        assistant,
                        task_id=task_id,
                        start_at=scheduled_for_dt.isoformat(),
                    ),
                ),
            )
            _wait_for_activation(assistant, task_id)

            _wait_for_unity_log_substring(
                assistant_id,
                f"Accepted due task {task_id}",
                batch_api=batch_api,
                core_api=core_api,
                gce_client=gce_client,
            )

            session_after = get_assistant_session(comms, assistant_id)
            assert session_after is not None
            assert session_after.get("metadata", {}).get("name") == session_before.get(
                "metadata",
                {},
            ).get("name")
            binding_after = (session_after.get("status") or {}).get("binding") or {}
            assert (binding_after.get("jobRef") or {}).get("name") == job_name_before
            assert str((session_after.get("spec") or {}).get("activationId") or "") == (
                activation_id_before
            )

            secret_name_after = str(
                (session_after.get("spec") or {}).get("startupSecretRef")
                or secret_name,
            )
            bootstrap_after = read_bootstrap_secret(
                core_api,
                NAMESPACE,
                secret_name_after,
            )
            assert "wake_reasons" not in bootstrap_after
        finally:
            _cleanup_unity_logs(assistant, created_log_ids)
            cleanup_assistant_jobs(
                batch_api,
                [assistant_id],
                context="task-activation-e2e-scheduled-live",
            )
            replenish_pool()
            _delete_test_assistant(assistant_id, batch_api)

    def test_triggered_inbound_surfaces_live_candidate_and_keeps_offline_candidate_hidden(
        self,
        batch_api,
        core_api,
        gce_client,
        pubsub_subscriber,
    ):
        """Triggered tasks should piggyback on the real inbound while hiding offline work."""

        assistant = _create_remote_task_assistant(batch_api)
        assistant_id = str(assistant["assistant_id"])
        created_log_ids: list[int] = []
        subscriber = pubsub_subscriber

        try:
            response, _elapsed = _wakeup(assistant_id)
            assert (
                response.status_code == 200
            ), f"Wakeup failed for assistant {assistant_id}: {response.status_code} {response.text}"
            wait_for_container_running(
                batch_api,
                assistant_id,
                timeout=180,
                interval=10,
            )
            wait_for_assistant_container_ready(
                assistant_id,
                batch_api=batch_api,
                core_api=core_api,
                gce_client=gce_client,
                timeout=300,
                interval=5,
            )
            time.sleep(RUNTIME_INIT_GRACE_SECONDS)

            live_task_id = _task_id_seed()
            offline_task_id = live_task_id + 1
            created_log_ids.extend(
                [
                    _create_unity_task_log(
                        assistant,
                        _trigger_task_entries(
                            assistant,
                            task_id=live_task_id,
                            from_contact_ids=[1],
                        ),
                    ),
                    _create_unity_task_log(
                        assistant,
                        _trigger_task_entries(
                            assistant,
                            task_id=offline_task_id,
                            from_contact_ids=[1],
                            offline=True,
                        ),
                    ),
                ],
            )
            live_activation = _wait_for_activation(assistant, live_task_id)
            offline_activation = _wait_for_activation(assistant, offline_task_id)
            assert live_activation["execution_mode"] == "live"
            assert offline_activation["execution_mode"] == "offline"

            token = f"TASK_TRIGGER_ACK_{int(time.time())}"
            msg_body = (
                "Please reply with exactly this token and nothing else: "
                f"{token}. This is a deterministic integration test."
            )
            message_response = send_test_message(assistant, body=msg_body)
            assert (
                message_response.status_code == 200
            ), f"Message send failed: {message_response.status_code} {message_response.text}"

            _wait_for_unity_log_substring(
                assistant_id,
                f"Matched trigger candidates [{live_task_id}]",
                batch_api=batch_api,
                core_api=core_api,
                gce_client=gce_client,
            )

            messages = poll_until(
                lambda: [
                    message
                    for message in pull_outbound_messages(
                        subscriber,
                        assistant_id,
                        timeout=10,
                    )
                    if token in json.dumps(message, sort_keys=True)
                ],
                timeout=120,
                interval=10,
                description=(
                    f"Outbound reply containing {token} for assistant {assistant_id}"
                ),
                failure_snapshot=lambda: _assistant_readiness_snapshot(
                    assistant_id,
                    batch_api=batch_api,
                    core_api=core_api,
                    gce_client=gce_client,
                ),
            )
            assert messages, f"Expected an outbound reply containing {token}"
        finally:
            _cleanup_unity_logs(assistant, created_log_ids)
            cleanup_assistant_jobs(
                batch_api,
                [assistant_id],
                context="task-activation-e2e-triggered",
            )
            replenish_pool()
            _delete_test_assistant(assistant_id, batch_api)

    @pytest.mark.merge_gate
    def test_offline_scheduled_task_runs_as_dedicated_job(
        self,
        batch_api,
        comms,
    ):
        """Offline scheduled tasks launch one-shot ``unity-task-run`` Jobs.

        Offline execution must never touch the interactive-session machinery:
        no AssistantSession is created, and the run executes inside a
        dedicated Kubernetes Job carrying the runner env. Waking the live
        ConversationManager lane for offline work is the regression this
        guards against.
        """

        assistant = _create_remote_task_assistant(batch_api)
        assistant_id = str(assistant["assistant_id"])
        created_log_ids: list[int] = []

        def _find_task_run_jobs() -> list[Any]:
            jobs = batch_api.list_namespaced_job(
                namespace=NAMESPACE,
                label_selector=(f"app=unity-task-run,assistant-id={assistant_id}"),
            )
            return list(jobs.items or [])

        try:
            scheduled_for_dt = (
                datetime.now(UTC) + timedelta(seconds=TASK_DUE_LEAD_SECONDS)
            ).replace(microsecond=0)
            task_id = _task_id_seed()
            created_log_ids.append(
                _create_unity_task_log(
                    assistant,
                    _scheduled_task_entries(
                        assistant,
                        task_id=task_id,
                        start_at=scheduled_for_dt.isoformat(),
                        offline=True,
                    ),
                ),
            )
            activation = _wait_for_activation(assistant, task_id)
            assert activation["execution_mode"] == "offline"

            task_run_jobs = poll_until(
                _find_task_run_jobs,
                timeout=TASK_DUE_LEAD_SECONDS + TASK_FLOW_TIMEOUT_SECONDS,
                interval=5,
                description=(
                    f"unity-task-run Job for offline task {task_id} "
                    f"on assistant {assistant_id}"
                ),
            )
            assert task_run_jobs, "Expected a dedicated unity-task-run Job"
            job = task_run_jobs[0]
            assert job.metadata.name.startswith("unity-task-run-")
            assert job.metadata.labels.get("task-id") == str(task_id)
            assert job.spec.backoff_limit == 0
            assert job.spec.ttl_seconds_after_finished is not None
            assert job.spec.active_deadline_seconds is not None

            # Offline work must never create an interactive AssistantSession;
            # a session here means the live ConversationManager lane woke.
            deadline = time.monotonic() + OFFLINE_NO_WAKE_GRACE_SECONDS
            while time.monotonic() < deadline:
                session = get_assistant_session(comms, assistant_id)
                assert session is None, (
                    f"Offline scheduled task created an AssistantSession: "
                    f"{json.dumps(session, indent=2)}"
                )
                time.sleep(min(5.0, deadline - time.monotonic()))
        finally:
            _cleanup_unity_logs(assistant, created_log_ids)
            for job in _find_task_run_jobs():
                try:
                    batch_api.delete_namespaced_job(
                        name=job.metadata.name,
                        namespace=NAMESPACE,
                        propagation_policy="Background",
                    )
                except Exception:
                    pass
            cleanup_assistant_jobs(
                batch_api,
                [assistant_id],
                context="task-activation-e2e-offline",
            )
            replenish_pool()
            _delete_test_assistant(assistant_id, batch_api)
