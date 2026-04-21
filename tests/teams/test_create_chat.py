"""Tests for the Teams chat creation endpoint.

Exercises POST /teams/chats with a mocked Graph client:
- happy paths for oneOnOne and group (with topic, dedup of sender)
- validation errors (missing fields, wrong chat_type, member-count mismatch,
  oneOnOne with topic)
- Graph failure surfacing to HTTP 500
"""

import os

import pytest
from fastapi.testclient import TestClient
from msgraph.generated.models.chat import Chat
from msgraph.generated.models.chat_type import ChatType
from unittest.mock import AsyncMock, MagicMock, patch

from common.settings import SETTINGS


def _member_upn(member) -> str:
    """Extract the UPN out of a built AadUserConversationMember's bind URL."""
    bind = (member.additional_data or {}).get("user@odata.bind", "")
    return bind.rsplit("/", 1)[-1]


@pytest.fixture
def mock_graph_client():
    graph = MagicMock()
    graph.me.get = AsyncMock(
        return_value=MagicMock(
            id="me-id",
            user_principal_name="assistant@contoso.com",
        ),
    )
    graph.chats.post = AsyncMock(return_value=MagicMock(id="19:abc@thread.v2"))
    return graph


@pytest.fixture
def client(mock_graph_client):
    os.environ.setdefault("GCP_SA_KEY", "{}")
    with (
        patch(
            "communication.teams.views.get_graph_client",
            new_callable=AsyncMock,
            return_value=mock_graph_client,
        ),
        patch.object(SETTINGS, "orchestra_admin_key", "test-admin-key"),
    ):
        from communication.main import app

        test_client = TestClient(app)
        test_client.headers["Authorization"] = "Bearer test-admin-key"
        yield test_client


class TestCreateOneOnOneChat:
    def test_one_on_one_happy_path(self, client, mock_graph_client):
        resp = client.post(
            "/teams/chats",
            json={
                "from": "assistant@contoso.com",
                "chat_type": "oneOnOne",
                "members": ["alice@contoso.com"],
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data == {
            "success": True,
            "chat_id": "19:abc@thread.v2",
            "chat_type": "oneOnOne",
        }

        assert mock_graph_client.chats.post.await_count == 1
        (chat_arg,), _ = mock_graph_client.chats.post.await_args
        assert isinstance(chat_arg, Chat)
        assert chat_arg.chat_type == ChatType.OneOnOne
        assert chat_arg.topic is None

        upns = sorted(_member_upn(m).lower() for m in chat_arg.members)
        assert upns == ["alice@contoso.com", "assistant@contoso.com"]
        for member in chat_arg.members:
            assert member.roles == ["owner"]
            assert member.odata_type == "#microsoft.graph.aadUserConversationMember"
            assert member.additional_data["user@odata.bind"].startswith(
                "https://graph.microsoft.com/v1.0/users/",
            )

    def test_one_on_one_rejects_topic(self, client):
        resp = client.post(
            "/teams/chats",
            json={
                "from": "assistant@contoso.com",
                "chat_type": "oneOnOne",
                "members": ["alice@contoso.com"],
                "topic": "nope",
            },
        )
        assert resp.status_code == 400
        assert "oneOnOne" in resp.json()["detail"]

    @pytest.mark.parametrize("members", [[], ["a@x.com", "b@x.com"]])
    def test_one_on_one_member_count_mismatch(self, client, members):
        resp = client.post(
            "/teams/chats",
            json={
                "from": "assistant@contoso.com",
                "chat_type": "oneOnOne",
                "members": members,
            },
        )
        assert resp.status_code == 400
        assert "oneOnOne" in resp.json()["detail"]


class TestCreateGroupChat:
    def test_group_happy_path_with_topic(self, client, mock_graph_client):
        resp = client.post(
            "/teams/chats",
            json={
                "from": "assistant@contoso.com",
                "chat_type": "group",
                "members": [
                    "alice@contoso.com",
                    "bob@contoso.com",
                    "carol@contoso.com",
                ],
                "topic": "Launch",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["success"] is True
        assert data["chat_type"] == "group"
        assert data["chat_id"] == "19:abc@thread.v2"

        (chat_arg,), _ = mock_graph_client.chats.post.await_args
        assert chat_arg.chat_type == ChatType.Group
        assert chat_arg.topic == "Launch"

        upns = sorted(_member_upn(m).lower() for m in chat_arg.members)
        assert upns == [
            "alice@contoso.com",
            "assistant@contoso.com",
            "bob@contoso.com",
            "carol@contoso.com",
        ]

    def test_group_dedupes_sender_in_members(self, client, mock_graph_client):
        """If the caller accidentally puts the sender in ``members``,
        the payload still contains a single entry for them."""
        resp = client.post(
            "/teams/chats",
            json={
                "from": "assistant@contoso.com",
                "chat_type": "group",
                "members": [
                    "Assistant@contoso.com",
                    "alice@contoso.com",
                    "bob@contoso.com",
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        (chat_arg,), _ = mock_graph_client.chats.post.await_args
        upns = sorted(_member_upn(m).lower() for m in chat_arg.members)
        assert upns == [
            "alice@contoso.com",
            "assistant@contoso.com",
            "bob@contoso.com",
        ]
        assert chat_arg.topic is None

    def test_group_requires_at_least_two_members(self, client):
        resp = client.post(
            "/teams/chats",
            json={
                "from": "assistant@contoso.com",
                "chat_type": "group",
                "members": ["solo@contoso.com"],
            },
        )
        assert resp.status_code == 400
        assert "group" in resp.json()["detail"]


class TestCreateChatValidation:
    @pytest.mark.parametrize(
        "payload",
        [
            {"chat_type": "oneOnOne", "members": ["a@x.com"]},
            {"from": "s@x.com", "members": ["a@x.com"]},
            {"from": "s@x.com", "chat_type": "oneOnOne"},
        ],
    )
    def test_missing_required_fields(self, client, payload):
        resp = client.post("/teams/chats", json=payload)
        assert resp.status_code == 400
        assert "required" in resp.json()["detail"].lower()

    def test_bad_chat_type(self, client):
        resp = client.post(
            "/teams/chats",
            json={
                "from": "s@x.com",
                "chat_type": "channel",
                "members": ["a@x.com"],
            },
        )
        assert resp.status_code == 400
        assert "oneOnOne" in resp.json()["detail"]

    def test_graph_error_surfaces_as_500(self, client, mock_graph_client):
        mock_graph_client.chats.post.side_effect = RuntimeError("graph exploded")
        resp = client.post(
            "/teams/chats",
            json={
                "from": "assistant@contoso.com",
                "chat_type": "oneOnOne",
                "members": ["alice@contoso.com"],
            },
        )
        assert resp.status_code == 500
        assert "graph exploded" in resp.json()["detail"]
