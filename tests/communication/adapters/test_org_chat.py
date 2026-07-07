"""Tests for POST /unify/org-chat adapter endpoint."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ["GCP_SA_KEY"] = '{"type": "service_account", "project_id": "test"}'
os.environ["ORCHESTRA_ADMIN_KEY"] = "test-admin-key"
os.environ["GCP_PROJECT_ID"] = "test-project"
os.environ["ORCHESTRA_URL"] = "http://localhost:8000"


@pytest.fixture(scope="module")
def app_module():
    from adapters import main

    return main


@pytest.fixture
def mock_pubsub():
    mock_future = MagicMock()
    mock_future.result.return_value = "test-message-id"
    mock_publisher = MagicMock()
    mock_publisher.topic_path.side_effect = (
        lambda project, topic: f"projects/{project}/topics/{topic}"
    )
    mock_publisher.publish.return_value = mock_future
    return mock_publisher


@pytest.fixture
def mock_webhook_context():
    return {
        "assistant": {
            "assistant_id": "777",
            "user_id": 12345,
            "self_contact_id": 0,
            "boss_contact_id": 1,
        },
        "contacts": [{"contact_id": 1, "first_name": "Test"}],
        "is_job_running": True,
    }


@pytest.fixture
def client(app_module, mock_pubsub, mock_webhook_context):
    from fastapi.testclient import TestClient

    app_module._ensured_org_topics.clear()
    with (
        patch.object(
            app_module,
            "get_pubsub_client",
            return_value=mock_pubsub,
        ),
        patch.object(
            app_module,
            "build_webhook_context",
            return_value=mock_webhook_context,
        ),
        patch.object(app_module.SETTINGS, "orchestra_admin_key", "test-admin-key"),
    ):
        test_client = TestClient(app_module.app)
        test_client.headers["Authorization"] = "Bearer test-admin-key"
        test_client._mock_pubsub = mock_pubsub
        yield test_client


TEAM_MESSAGE = {
    "message_id": 7,
    "team_id": 3,
    "organization_id": 11,
    "sender_kind": "user",
    "sender_user_id": "user-1",
    "sender_name": "Dana",
    "content": "Morning everyone",
    "mentions": [],
    "timestamp": "2026-07-07T12:00:00+00:00",
}


class TestOrgChat:

    def test_team_message_publishes_and_fans_out(self, client):
        response = client.post(
            "/unify/org-chat",
            json={
                "kind": "team",
                "organization_id": 11,
                "team_id": 3,
                "message": TEAM_MESSAGE,
                "fanout_assistant_ids": [777],
                "assistant_event": {
                    "team_id": 3,
                    "team_name": "Growth",
                    "organization_id": 11,
                    "body": "Morning everyone",
                    "group_message_id": 7,
                    "sender_user_id": "user-1",
                    "sender_email": "dana@example.com",
                    "sender_name": "Dana",
                },
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["published"] is True
        assert body["fanned_out"] == 1
        assert body["fanout_errors"] == []

        publish_calls = client._mock_pubsub.publish.call_args_list
        assert len(publish_calls) == 2

        org_call = publish_calls[0]
        assert org_call[0][0].endswith("/topics/unity-org-11")
        org_frame = json.loads(org_call[0][1].decode("utf-8"))
        assert org_frame["thread"] == "team_message"
        assert org_frame["event"]["content"] == "Morning everyone"
        assert org_call[1]["thread"] == "team_message"
        assert org_call[1]["team_id"] == "3"
        assert org_call[1]["organization_id"] == "11"

        # Fan-out is a standard unify_message envelope with team context; the
        # sender is not this assistant's owner so no contact_id is resolved
        # (the runtime resolves the sender by email).
        fanout_call = publish_calls[1]
        assert fanout_call[0][0].endswith("/topics/unity-777")
        fanout_frame = json.loads(fanout_call[0][1].decode("utf-8"))
        assert fanout_frame["thread"] == "unify_message"
        assert fanout_frame["event"]["assistant_id"] == "777"
        assert fanout_frame["event"]["team_id"] == 3
        assert fanout_frame["event"]["body"] == "Morning everyone"
        assert fanout_frame["event"]["sender_email"] == "dana@example.com"
        assert "contact_id" not in fanout_frame["event"]
        assert fanout_call[1]["thread"] == "inbound"

    def test_team_message_owner_sender_resolves_boss_contact(self, client):
        response = client.post(
            "/unify/org-chat",
            json={
                "kind": "team",
                "organization_id": 11,
                "team_id": 3,
                "message": TEAM_MESSAGE,
                "fanout_assistant_ids": [777],
                "assistant_event": {
                    "team_id": 3,
                    "team_name": "Growth",
                    "organization_id": 11,
                    "body": "Morning everyone",
                    "group_message_id": 7,
                    # Matches the mocked webhook context's assistant user_id,
                    # so the fan-out resolves contact_id to the boss contact.
                    "sender_user_id": "12345",
                    "sender_email": "owner@example.com",
                    "sender_name": "Owner",
                },
            },
        )
        assert response.status_code == 200, response.text

        fanout_call = client._mock_pubsub.publish.call_args_list[1]
        fanout_frame = json.loads(fanout_call[0][1].decode("utf-8"))
        assert fanout_frame["thread"] == "unify_message"
        assert fanout_frame["event"]["contact_id"] == 1

    def test_dm_message_publishes_frame_only(self, client):
        response = client.post(
            "/unify/org-chat",
            json={
                "kind": "dm",
                "organization_id": 11,
                "message": {
                    "id": 1,
                    "thread_id": 5,
                    "organization_id": 11,
                    "user_ids": ["user-a", "user-b"],
                    "sender_user_id": "user-a",
                    "sender_name": "Dana",
                    "content": "hi",
                    "timestamp": "2026-07-07T12:00:00+00:00",
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["fanned_out"] == 0

        publish_calls = client._mock_pubsub.publish.call_args_list
        assert len(publish_calls) == 1
        dm_call = publish_calls[0]
        assert dm_call[0][0].endswith("/topics/unity-org-11")
        assert dm_call[1]["thread"] == "dm_message"
        assert dm_call[1]["dm_user_a"] == "user-a"
        assert dm_call[1]["dm_user_b"] == "user-b"

    def test_rejects_bad_payloads(self, client):
        assert (
            client.post(
                "/unify/org-chat",
                json={"kind": "nope", "organization_id": 1, "message": {"a": 1}},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/unify/org-chat",
                json={"kind": "team", "message": {"a": 1}},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/unify/org-chat",
                json={"kind": "team", "organization_id": 1, "message": {"a": 1}},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/unify/org-chat",
                json={
                    "kind": "dm",
                    "organization_id": 1,
                    "message": {"user_ids": ["only-one"]},
                },
            ).status_code
            == 400
        )

    def test_requires_admin_key(self, client):
        response = client.post(
            "/unify/org-chat",
            headers={"Authorization": "Bearer wrong-key"},
            json={
                "kind": "dm",
                "organization_id": 1,
                "message": {"user_ids": ["a", "b"]},
            },
        )
        assert response.status_code == 403
