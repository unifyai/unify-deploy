"""Tests for the Teams channel creation endpoint.

Exercises POST /teams/channels with a mocked Graph client:
- standard / private / shared happy paths
- validation errors (missing fields, bad membership_type, private w/o owners)
- post-create watch rebuild is invoked on success, and a rebuild failure
  does not fail the channel create
- Graph failure surfacing to HTTP 500
"""

import os

import pytest
from fastapi.testclient import TestClient
from msgraph.generated.models.channel import Channel
from msgraph.generated.models.channel_membership_type import ChannelMembershipType
from unittest.mock import AsyncMock, MagicMock, patch

from common.settings import SETTINGS


def _member_upn(member) -> str:
    bind = (member.additional_data or {}).get("user@odata.bind", "")
    return bind.rsplit("/", 1)[-1]


@pytest.fixture
def mock_graph_client():
    graph = MagicMock()
    graph.me.get = AsyncMock(
        return_value=MagicMock(id="me-id", user_principal_name="assistant@contoso.com"),
    )
    channels_ns = graph.teams.by_team_id.return_value.channels
    channels_ns.post = AsyncMock(
        return_value=MagicMock(id="19:channel-id@thread.tacv2")
    )
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
        patch(
            "communication.teams.views._rebuild_teams_watches",
            new_callable=AsyncMock,
            return_value={"success": True, "channel_count": 5, "channel_failures": 0},
        ) as rebuild_mock,
        patch.object(SETTINGS, "orchestra_admin_key", "test-admin-key"),
    ):
        from communication.main import app

        test_client = TestClient(app)
        test_client.headers["Authorization"] = "Bearer test-admin-key"
        test_client.rebuild_mock = rebuild_mock
        yield test_client


class TestCreateStandardChannel:
    def test_standard_happy_path(self, client, mock_graph_client):
        resp = client.post(
            "/teams/channels",
            json={
                "from": "assistant@contoso.com",
                "team_id": "team-1",
                "display_name": "Launch",
                "description": "planning",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["success"] is True
        assert data["channel_id"] == "19:channel-id@thread.tacv2"
        assert data["team_id"] == "team-1"
        assert data["membership_type"] == "standard"
        assert data["watch_rebuild"] == {
            "success": True,
            "channel_count": 5,
            "channel_failures": 0,
        }

        mock_graph_client.teams.by_team_id.assert_called_with("team-1")
        channels_post = mock_graph_client.teams.by_team_id.return_value.channels.post
        (channel_arg,), _ = channels_post.await_args
        assert isinstance(channel_arg, Channel)
        assert channel_arg.display_name == "Launch"
        assert channel_arg.description == "planning"
        assert channel_arg.membership_type == ChannelMembershipType.Standard
        # Standard channels do not carry pre-populated members.
        assert channel_arg.members is None

        client.rebuild_mock.assert_awaited_once()
        _, kwargs = client.rebuild_mock.await_args
        assert kwargs["user_email"] == "assistant@contoso.com"
        assert kwargs["user_id"] == "me-id"


class TestCreatePrivateOrSharedChannel:
    @pytest.mark.parametrize(
        ("membership_type_raw", "expected_enum"),
        [
            ("private", ChannelMembershipType.Private),
            ("shared", ChannelMembershipType.Shared),
        ],
    )
    def test_non_standard_with_owners(
        self,
        client,
        mock_graph_client,
        membership_type_raw,
        expected_enum,
    ):
        resp = client.post(
            "/teams/channels",
            json={
                "from": "assistant@contoso.com",
                "team_id": "team-1",
                "display_name": "Secret",
                "membership_type": membership_type_raw,
                "owners": ["alice@contoso.com", "bob@contoso.com"],
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["membership_type"] == membership_type_raw

        channels_post = mock_graph_client.teams.by_team_id.return_value.channels.post
        (channel_arg,), _ = channels_post.await_args
        assert channel_arg.membership_type == expected_enum
        upns = sorted(_member_upn(m) for m in channel_arg.members)
        assert upns == ["alice@contoso.com", "bob@contoso.com"]
        for member in channel_arg.members:
            assert member.roles == ["owner"]
            assert member.odata_type == "#microsoft.graph.aadUserConversationMember"

    @pytest.mark.parametrize("membership_type_raw", ["private", "shared"])
    def test_non_standard_requires_owners(self, client, membership_type_raw):
        resp = client.post(
            "/teams/channels",
            json={
                "from": "assistant@contoso.com",
                "team_id": "team-1",
                "display_name": "Secret",
                "membership_type": membership_type_raw,
            },
        )
        assert resp.status_code == 400
        assert "owner" in resp.json()["detail"].lower()


class TestCreateChannelValidation:
    @pytest.mark.parametrize(
        "payload",
        [
            {"team_id": "t", "display_name": "n"},
            {"from": "s@x.com", "display_name": "n"},
            {"from": "s@x.com", "team_id": "t"},
        ],
    )
    def test_missing_required_fields(self, client, payload):
        resp = client.post("/teams/channels", json=payload)
        assert resp.status_code == 400
        assert "required" in resp.json()["detail"].lower()

    def test_bad_membership_type(self, client):
        resp = client.post(
            "/teams/channels",
            json={
                "from": "s@x.com",
                "team_id": "t",
                "display_name": "n",
                "membership_type": "public",
            },
        )
        assert resp.status_code == 400
        assert "membership_type" in resp.json()["detail"]

    def test_bad_owners_type(self, client):
        resp = client.post(
            "/teams/channels",
            json={
                "from": "s@x.com",
                "team_id": "t",
                "display_name": "n",
                "owners": "alice@contoso.com",
            },
        )
        assert resp.status_code == 400
        assert "owners" in resp.json()["detail"]


class TestCreateChannelErrorHandling:
    def test_graph_error_surfaces_as_500(self, client, mock_graph_client):
        channels_post = mock_graph_client.teams.by_team_id.return_value.channels.post
        channels_post.side_effect = RuntimeError("forbidden")
        resp = client.post(
            "/teams/channels",
            json={
                "from": "assistant@contoso.com",
                "team_id": "team-1",
                "display_name": "Launch",
            },
        )
        assert resp.status_code == 500
        assert "forbidden" in resp.json()["detail"]
        # Rebuild must not be invoked if the create itself failed.
        client.rebuild_mock.assert_not_called()

    def test_rebuild_failure_does_not_fail_create(self, client, mock_graph_client):
        client.rebuild_mock.side_effect = RuntimeError("subs exploded")
        resp = client.post(
            "/teams/channels",
            json={
                "from": "assistant@contoso.com",
                "team_id": "team-1",
                "display_name": "Launch",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["success"] is True
        assert data["channel_id"] == "19:channel-id@thread.tacv2"
        assert data["watch_rebuild"] is None
