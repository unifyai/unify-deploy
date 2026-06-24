"""Local-stack integration tests for team-scoped scheduled task due delivery."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import pytest
import requests

from tests.infra.integration.conftest import LocalStackUrls
from tests.infra.integration.local_stack_orchestra_helpers import (
    admin_key,
    auth_headers,
    create_organization,
    create_team,
    delete_subscription,
    delete_team,
    ensure_pubsub_topic_exists,
    fetch_admin_assistant_record,
    mark_assistant_local_runtime,
    org_coordinator,
    pull_pubsub_message,
    temporary_inbound_subscription,
)

pytestmark = [pytest.mark.integration, pytest.mark.local_stack]


def _task_due_payload(
    assistant_id: str,
    *,
    destination: str | None = None,
    task_id: int = 9001,
) -> dict:
    payload = {
        "assistant_id": assistant_id,
        "task_id": task_id,
        "source_task_log_id": 91001,
        "activation_revision": "local-stack-rev-1",
        "scheduled_for": datetime.now(timezone.utc).isoformat(),
        "execution_mode": "live",
        "source_type": "scheduled",
        "task_label": "Team briefing",
        "task_summary": "Deliver the shared-team update.",
        "visibility_policy": "silent_by_default",
        "recurrence_hint": "one_off",
    }
    if destination is not None:
        payload["destination"] = destination
    return payload


def _post_scheduled_task_due(urls: LocalStackUrls, payload: dict) -> requests.Response:
    return requests.post(
        f"{urls.adapters_url}/scheduled/tasks/due",
        json=payload,
        headers=auth_headers(admin_key()),
        timeout=30,
    )


def test_team_task_due_skips_revoked_destination(
    require_local_stack,
):
    """Adapters acks team destinations the assistant no longer belongs to."""

    urls = require_local_stack
    org = None
    team_ids: list[int] = []
    name_prefix = f"team-task-due-revoked-{int(time.time())}-{uuid.uuid4().hex[:8]}"

    try:
        org = create_organization(urls, f"{name_prefix}-org")
        team = create_team(urls, org, f"{name_prefix}-team")
        team_ids = [team["id"]]

        coordinator = org_coordinator(urls, org)
        assistant_id = str(coordinator["agent_id"])
        record = fetch_admin_assistant_record(urls, assistant_id)
        assert team["id"] in (record.get("team_ids") or [])

        revoked_team_id = max(team["id"] + 10_000, 999_999)
        assert revoked_team_id not in (record.get("team_ids") or [])

        response = _post_scheduled_task_due(
            urls,
            _task_due_payload(
                assistant_id,
                destination=f"team:{revoked_team_id}",
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body == {
            "success": True,
            "status": "skipped",
            "reason": "destination_membership_revoked",
        }
    finally:
        if org is not None:
            for team_id in team_ids:
                delete_team(urls, org, team_id)


def test_team_task_due_publishes_authorized_destination(
    require_local_stack,
    local_stack_comms,
    local_stack_pubsub_subscriber,
):
    """Authorized team destinations publish task_due on the local Pub/Sub bus."""

    urls = require_local_stack
    org = None
    team_ids: list[int] = []
    subscription_path: str | None = None
    name_prefix = f"team-task-due-auth-{int(time.time())}-{uuid.uuid4().hex[:8]}"

    try:
        org = create_organization(urls, f"{name_prefix}-org")
        team = create_team(urls, org, f"{name_prefix}-team")
        team_ids = [team["id"]]

        coordinator = org_coordinator(urls, org)
        assistant_id = str(coordinator["agent_id"])
        record = fetch_admin_assistant_record(urls, assistant_id)
        assert team["id"] in (record.get("team_ids") or [])

        mark_assistant_local_runtime(assistant_id)
        record = fetch_admin_assistant_record(urls, assistant_id)
        assert record.get("is_local") is True

        ensure_pubsub_topic_exists(urls, local_stack_comms, assistant_id)
        subscription_path = temporary_inbound_subscription(
            local_stack_pubsub_subscriber,
            urls,
            assistant_id,
            label="team-task-due",
        )

        destination = f"team:{team['id']}"
        response = _post_scheduled_task_due(
            urls,
            _task_due_payload(assistant_id, destination=destination),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["success"] is True
        assert body["status"] == "published_local"
        assert body["assistant_id"] == assistant_id

        message = pull_pubsub_message(
            local_stack_pubsub_subscriber,
            subscription_path,
            thread="unity_system_event",
        )
        event = message["event"]
        assert event["assistant_id"] == assistant_id
        assert event["event_type"] == "task_due"
        assert event["type"] == "task_due"
        assert event["destination"] == destination
        assert event["task_id"] == 9001
        assert event["task_label"] == "Team briefing"
    finally:
        if org is not None:
            for team_id in team_ids:
                delete_team(urls, org, team_id)
        if subscription_path is not None:
            delete_subscription(local_stack_pubsub_subscriber, subscription_path)
