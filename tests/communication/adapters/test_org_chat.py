"""Tests for the POST /unify/chat adapter endpoint."""

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
        patch.dict(os.environ, {"ORCHESTRA_ADMIN_KEY": "test-admin-key"}),
    ):
        test_client = TestClient(app_module.app)
        test_client.headers["Authorization"] = "Bearer test-admin-key"
        test_client._mock_pubsub = mock_pubsub
        yield test_client


TEAM_MESSAGE = {
    "id": 7,
    "thread_id": 31,
    "kind": "team",
    "team_id": 3,
    "organization_id": 11,
    "sender_kind": "user",
    "sender_user_id": "user-1",
    "sender_name": "Dana",
    "content": "Morning everyone",
    "mentions": [],
    "timestamp": "2026-07-07T12:00:00+00:00",
}

GROUP_MESSAGE = {
    "id": 9,
    "thread_id": 32,
    "kind": "group",
    "group_id": 42,
    "organization_id": 11,
    "sender_kind": "user",
    "sender_user_id": "user-1",
    "sender_name": "Dana",
    "content": "Group hello",
    "mentions": [],
    "timestamp": "2026-07-07T12:00:00+00:00",
}


class TestOrgChat:

    def test_team_message_publishes_and_fans_out(self, client):
        response = client.post(
            "/unify/chat",
            json={
                "kind": "team",
                "organization_id": 11,
                "thread_id": 31,
                "team_id": 3,
                "message": TEAM_MESSAGE,
                "fanout_assistant_ids": [777],
                "assistant_event": {
                    "team_id": 3,
                    "team_name": "Growth",
                    "organization_id": 11,
                    "body": "Morning everyone",
                    "chat_message_id": 7,
                    "thread_id": 31,
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
        assert org_frame["thread"] == "chat_message"
        assert org_frame["event"]["content"] == "Morning everyone"
        assert org_call[1]["thread"] == "chat_message"
        assert org_call[1]["kind"] == "team"
        assert org_call[1]["thread_id"] == "31"
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
        assert fanout_frame["event"]["thread_id"] == 31
        assert fanout_frame["event"]["body"] == "Morning everyone"
        assert fanout_frame["event"]["sender_email"] == "dana@example.com"
        assert "contact_id" not in fanout_frame["event"]
        assert fanout_call[1]["thread"] == "inbound"

    def test_group_message_publishes_and_fans_out(self, client):
        response = client.post(
            "/unify/chat",
            json={
                "kind": "group",
                "organization_id": 11,
                "thread_id": 32,
                "group_id": 42,
                "message": GROUP_MESSAGE,
                "fanout_assistant_ids": [777],
                "assistant_event": {
                    "group_id": 42,
                    "group_name": "Ops",
                    "organization_id": 11,
                    "body": "Group hello",
                    "chat_message_id": 9,
                    "thread_id": 32,
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
        assert org_frame["thread"] == "chat_message"
        assert org_frame["event"]["content"] == "Group hello"
        assert org_call[1]["thread"] == "chat_message"
        assert org_call[1]["kind"] == "group"
        assert org_call[1]["thread_id"] == "32"
        assert org_call[1]["group_id"] == "42"
        assert org_call[1]["organization_id"] == "11"
        assert "team_id" not in org_call[1]

        fanout_call = publish_calls[1]
        assert fanout_call[0][0].endswith("/topics/unity-777")
        fanout_frame = json.loads(fanout_call[0][1].decode("utf-8"))
        assert fanout_frame["thread"] == "unify_message"
        assert fanout_frame["event"]["assistant_id"] == "777"
        assert fanout_frame["event"]["group_id"] == 42
        assert fanout_frame["event"]["group_name"] == "Ops"
        assert fanout_frame["event"]["body"] == "Group hello"
        assert fanout_frame["event"]["sender_email"] == "dana@example.com"
        assert "contact_id" not in fanout_frame["event"]
        assert fanout_call[1]["thread"] == "inbound"

    def test_team_message_owner_sender_resolves_boss_contact(self, client):
        response = client.post(
            "/unify/chat",
            json={
                "kind": "team",
                "organization_id": 11,
                "thread_id": 31,
                "team_id": 3,
                "message": TEAM_MESSAGE,
                "fanout_assistant_ids": [777],
                "assistant_event": {
                    "team_id": 3,
                    "team_name": "Growth",
                    "organization_id": 11,
                    "body": "Morning everyone",
                    "chat_message_id": 7,
                    "thread_id": 31,
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
            "/unify/chat",
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
        assert dm_call[1]["thread"] == "chat_message"
        assert dm_call[1]["kind"] == "dm"
        assert dm_call[1]["dm_user_a"] == "user-a"
        assert dm_call[1]["dm_user_b"] == "user-b"

    def test_dm_call_publishes_frame_only(self, client):
        response = client.post(
            "/unify/chat",
            json={
                "kind": "call",
                "action": "incoming",
                "organization_id": 11,
                "call": {
                    "call_id": "call-123",
                    "room_name": "unity_call_call-123",
                    "status": "ringing",
                    "caller_user_id": "user-a",
                    "callee_user_id": "user-b",
                    "user_ids": ["user-a", "user-b", "user-c"],
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["fanned_out"] == 0

        publish_calls = client._mock_pubsub.publish.call_args_list
        assert len(publish_calls) == 1
        dm_call = publish_calls[0]
        assert dm_call[0][0].endswith("/topics/unity-org-11")
        org_frame = json.loads(dm_call[0][1].decode("utf-8"))
        assert org_frame["thread"] == "call_incoming"
        assert org_frame["event"]["call_id"] == "call-123"
        assert org_frame["event"]["user_ids"] == ["user-a", "user-b", "user-c"]
        assert dm_call[1]["thread"] == "call_incoming"
        assert dm_call[1]["dm_user_a"] == "user-a"
        assert dm_call[1]["dm_user_b"] == "user-b"
        assert dm_call[1]["call_id"] == "call-123"
        assert dm_call[1]["user_ids"] == "user-a,user-b,user-c"
        assert "group_id" not in dm_call[1]

    def test_assistant_dm_call_frame_routes_to_assistant_topic(self, client):
        """1:1 assistant call signaling rides the assistant topic, not an org."""
        response = client.post(
            "/unify/chat",
            json={
                "kind": "call",
                "action": "incoming",
                "organization_id": None,
                "call": {
                    "call_id": "call-adm-1",
                    "room_name": "unity_call_call-adm-1",
                    "status": "ringing",
                    "scope": "assistant_dm",
                    "caller_user_id": "user-a",
                    "created_by_assistant_id": 777,
                    "user_ids": ["user-a"],
                    "assistant_ids": [777],
                },
            },
        )
        assert response.status_code == 200, response.text

        publish_calls = client._mock_pubsub.publish.call_args_list
        assert len(publish_calls) == 1
        frame_call = publish_calls[0]
        assert frame_call[0][0].endswith("/topics/unity-777")
        frame = json.loads(frame_call[0][1].decode("utf-8"))
        assert frame["thread"] == "call_incoming"
        assert frame["event"]["call_id"] == "call-adm-1"
        assert frame_call[1]["thread"] == "call_incoming"
        assert frame_call[1]["assistant_id"] == "777"

    def test_org_call_passes_through_group_id(self, client):
        response = client.post(
            "/unify/chat",
            json={
                "kind": "call",
                "action": "incoming",
                "organization_id": 11,
                "call": {
                    "call_id": "call-group-1",
                    "room_name": "unity_call_call-group-1",
                    "status": "ringing",
                    "caller_user_id": "user-a",
                    "user_ids": ["user-a", "user-b"],
                    "group_id": 42,
                },
            },
        )
        assert response.status_code == 200, response.text

        publish_calls = client._mock_pubsub.publish.call_args_list
        assert len(publish_calls) == 1
        call_attrs = publish_calls[0][1]
        assert call_attrs["group_id"] == "42"
        assert "team_id" not in call_attrs
        org_frame = json.loads(publish_calls[0][0][1].decode("utf-8"))
        assert org_frame["event"]["group_id"] == 42

    def test_assistant_dm_publishes_to_assistant_topic_and_fans_out(self, client):
        response = client.post(
            "/unify/chat",
            json={
                "kind": "assistant_dm",
                "organization_id": None,
                "thread_id": 51,
                "assistant_id": 777,
                "message": {
                    "id": 4,
                    "thread_id": 51,
                    "kind": "assistant_dm",
                    "assistant_id": 777,
                    "user_id": "12345",
                    "sender_kind": "user",
                    "sender_user_id": "12345",
                    "sender_name": "Owner",
                    "content": "hello there",
                    "timestamp": "2026-07-07T12:00:00+00:00",
                },
                "fanout_assistant_ids": [777],
                "assistant_event": {
                    "thread_id": 51,
                    "thread_kind": "assistant_dm",
                    "chat_message_id": 4,
                    "body": "hello there",
                    "sender_user_id": "12345",
                    "sender_email": "owner@example.com",
                    "sender_name": "Owner",
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["fanned_out"] == 1

        publish_calls = client._mock_pubsub.publish.call_args_list
        assert len(publish_calls) == 2

        # Console frame goes to the per-assistant topic (1-on-1 stream).
        frame_call = publish_calls[0]
        assert frame_call[0][0].endswith("/topics/unity-777")
        frame = json.loads(frame_call[0][1].decode("utf-8"))
        assert frame["thread"] == "chat_message"
        assert frame["event"]["content"] == "hello there"
        assert frame_call[1]["kind"] == "assistant_dm"

        # The runtime receives a standard unify_message envelope with the
        # thread scope; the owner sender resolves to the boss contact.
        fanout_call = publish_calls[1]
        assert fanout_call[0][0].endswith("/topics/unity-777")
        fanout_frame = json.loads(fanout_call[0][1].decode("utf-8"))
        assert fanout_frame["thread"] == "unify_message"
        assert fanout_frame["event"]["thread_id"] == 51
        assert fanout_frame["event"]["contact_id"] == 1

    def test_assistant_peer_dm_publishes_to_both_topics_and_fans_out(self, client):
        response = client.post(
            "/unify/chat",
            json={
                "kind": "assistant_peer_dm",
                "organization_id": 9,
                "thread_id": 88,
                "assistant_id": 100,
                "peer_assistant_id": 200,
                "message": {
                    "id": 5,
                    "thread_id": 88,
                    "kind": "assistant_peer_dm",
                    "assistant_id": 100,
                    "peer_assistant_id": 200,
                    "assistant_ids": [100, 200],
                    "sender_kind": "assistant",
                    "sender_assistant_id": 100,
                    "sender_name": "Alpha",
                    "content": "hey peer",
                    "timestamp": "2026-07-07T12:00:00+00:00",
                },
                "fanout_assistant_ids": [200],
                "assistant_event": {
                    "thread_id": 88,
                    "thread_kind": "assistant_peer_dm",
                    "chat_message_id": 5,
                    "body": "hey peer",
                    "sender_kind": "assistant",
                    "sender_assistant_id": 100,
                    "sender_name": "Alpha",
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["fanned_out"] == 1

        publish_calls = client._mock_pubsub.publish.call_args_list
        # Two Console frames (one per peer topic) + one runtime fan-out.
        assert len(publish_calls) == 3
        frame_topics = {call[0][0] for call in publish_calls[:2]}
        assert any(t.endswith("/topics/unity-100") for t in frame_topics)
        assert any(t.endswith("/topics/unity-200") for t in frame_topics)
        for call in publish_calls[:2]:
            frame = json.loads(call[0][1].decode("utf-8"))
            assert frame["thread"] == "chat_message"
            assert frame["event"]["content"] == "hey peer"
            assert call[1]["kind"] == "assistant_peer_dm"

        fanout_call = publish_calls[2]
        # build_webhook_context is mocked to assistant 777 in this suite.
        assert fanout_call[0][0].endswith("/topics/unity-777")
        fanout_frame = json.loads(fanout_call[0][1].decode("utf-8"))
        assert fanout_frame["thread"] == "unify_message"
        assert fanout_frame["event"]["thread_id"] == 88
        assert fanout_frame["event"]["sender_assistant_id"] == 100

    def test_reaction_publishes_frame_and_runtime_envelope(self, client):
        response = client.post(
            "/unify/chat",
            json={
                "kind": "reaction",
                "thread_kind": "assistant_dm",
                "thread_id": 51,
                "assistant_id": 777,
                "reactor_user_id": "12345",
                "emoji": "👍",
                "message": {
                    "id": 4,
                    "thread_id": 51,
                    "kind": "assistant_dm",
                    "assistant_id": 777,
                    "sender_kind": "assistant",
                    "content": "nice",
                    "reactions": [{"user_id": "12345", "emoji": "👍"}],
                },
                "fanout_assistant_ids": [777],
            },
        )
        assert response.status_code == 200, response.text

        publish_calls = client._mock_pubsub.publish.call_args_list
        assert len(publish_calls) == 2
        frame = json.loads(publish_calls[0][0][1].decode("utf-8"))
        assert frame["thread"] == "chat_reaction"
        fanout_frame = json.loads(publish_calls[1][0][1].decode("utf-8"))
        assert fanout_frame["thread"] == "unify_message_reaction"
        assert fanout_frame["event"]["chat_message_id"] == 4
        assert fanout_frame["event"]["emoji"] == "👍"
        assert fanout_frame["event"]["contact_id"] == 1

    def test_rejects_bad_payloads(self, client):
        assert (
            client.post(
                "/unify/chat",
                json={"kind": "nope", "organization_id": 1, "message": {"a": 1}},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/unify/chat",
                json={"kind": "team", "message": {}},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/unify/chat",
                json={"kind": "assistant_dm", "message": {"content": "x"}},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/unify/chat",
                json={
                    "kind": "call",
                    "organization_id": 1,
                    "call": {"call_id": "call-1", "room_name": "room-1"},
                },
            ).status_code
            == 400
        )

    def test_requires_admin_key(self, client):
        response = client.post(
            "/unify/chat",
            headers={"Authorization": "Bearer wrong-key"},
            json={
                "kind": "dm",
                "organization_id": 1,
                "message": {"user_ids": ["a", "b"]},
            },
        )
        assert response.status_code == 403
