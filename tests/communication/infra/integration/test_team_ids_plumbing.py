"""Local-stack integration tests for assistant team membership plumbing."""

from __future__ import annotations

import time
import uuid

import pytest

from tests.communication.infra.integration.local_stack_orchestra_helpers import (
    create_organization,
    create_team,
    delete_subscription,
    delete_team,
    ensure_pubsub_topic_exists,
    fetch_admin_assistant_record,
    org_coordinator,
    post_membership_update,
    pull_pubsub_message,
    temporary_inbound_subscription,
)

pytestmark = [pytest.mark.integration, pytest.mark.local_stack]


def test_team_membership_round_trips_through_local_stack(
    require_local_stack,
    local_stack_adapters,
    local_stack_comms,
    local_stack_pubsub_subscriber,
):
    """Orchestra team memberships publish team_ids on the local Pub/Sub bus."""

    urls = require_local_stack
    org = None
    team_ids: list[int] = []
    subscription_path: str | None = None
    name_prefix = f"team-plumbing-{int(time.time())}-{uuid.uuid4().hex[:8]}"

    try:
        org = create_organization(urls, f"{name_prefix}-org")
        team_a = create_team(urls, org, f"{name_prefix}-a")
        team_b = create_team(urls, org, f"{name_prefix}-b")
        team_ids = [team_a["id"], team_b["id"]]

        coordinator = org_coordinator(urls, org)
        assistant_id = str(coordinator["agent_id"])

        record = fetch_admin_assistant_record(urls, assistant_id)
        assert sorted(record.get("team_ids") or []) == sorted(team_ids)
        summaries = record.get("team_summaries") or []
        assert {summary["team_id"] for summary in summaries} == set(team_ids)
        ensure_pubsub_topic_exists(urls, local_stack_comms, assistant_id)
        subscription_path = temporary_inbound_subscription(
            local_stack_pubsub_subscriber,
            urls,
            assistant_id,
            label="team-plumbing",
        )
        post_membership_update(local_stack_adapters, assistant_id)
        message = pull_pubsub_message(
            local_stack_pubsub_subscriber,
            subscription_path,
            thread="assistant_update",
        )

        event = message["event"]
        assert event["assistant_id"] == assistant_id
        assert event.get("update_kind") == "membership"
        assert sorted(event.get("team_ids") or []) == sorted(team_ids)
        assert {
            summary["team_id"] for summary in event.get("team_summaries") or []
        } == set(
            team_ids,
        )
    finally:
        if org is not None:
            for team_id in team_ids:
                delete_team(urls, org, team_id)
        if subscription_path is not None:
            delete_subscription(local_stack_pubsub_subscriber, subscription_path)
