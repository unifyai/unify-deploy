"""Regression tests for local-assistant startup behavior in adapters."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ["GCP_SA_KEY"] = '{"type": "service_account", "project_id": "test"}'
os.environ["ORCHESTRA_ADMIN_KEY"] = "test-admin-key"
os.environ["GCP_PROJECT_ID"] = "test-project"
os.environ["ORCHESTRA_URL"] = "http://localhost:8000"
# Force-assign rather than setdefault so a developer's local .env value
# (loaded by conftest) never leaks into these tests and breaks the
# clientState check below.
os.environ["OUTLOOK_WEBHOOK_SECRET"] = "test-outlook-secret"
os.environ["TEAMS_WEBHOOK_SECRET"] = "test-teams-secret"

TEST_SELF_CONTACT_ID = 42
TEST_BOSS_CONTACT_ID = 43


class _GraphResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, response_payload: dict, calls: list | None = None):
        self._response_payload = response_payload
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, *_args, **kwargs):
        if self._calls is not None:
            self._calls.append(
                {
                    "url": url,
                    "headers": kwargs.get("headers", {}),
                },
            )
        return _GraphResponse(self._response_payload)


class _RoutedAsyncClient:
    """Graph fake that picks a response payload based on the request URL.

    Needed once the handler makes multiple Graph calls per notification
    (message fetch, chat metadata, roster); a single-payload fake can't
    distinguish between them.  Entries in *routes* are matched with a
    simple substring test and the first hit wins, falling back to the
    final ``default`` payload.
    """

    def __init__(
        self,
        routes: list[tuple[str, dict]],
        *,
        default: dict | None = None,
        calls: list | None = None,
    ):
        self._routes = routes
        self._default = default or {}
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, *_args, **kwargs):
        if self._calls is not None:
            self._calls.append(
                {
                    "url": url,
                    "headers": kwargs.get("headers", {}),
                },
            )
        for needle, payload in self._routes:
            if needle in url:
                return _GraphResponse(payload)
        return _GraphResponse(self._default)


@pytest.fixture(scope="module")
def app_module():
    # Share the already-loaded adapters.main with the rest of the suite.
    # A previous implementation wiped sys.modules["adapters.*"] and
    # reimported here, which left other test files holding stale
    # references to the old adapters.main (their `from adapters.main
    # import app` at module load would capture the pre-wipe module while
    # patches targeted the post-wipe module, so handlers ran on the old
    # one and ignored the mocks). The env/webhook-secret setup now
    # happens unconditionally at the top of this file, so reimporting is
    # no longer necessary.
    from adapters import main

    return main


def _assistant_data(*, is_local: bool) -> dict:
    return {
        "assistant_id": "assistant-123",
        "user_id": "user-123",
        "api_key": "api-key",
        "user_number": "",
        "user_whatsapp_number": "",
        "user_email": "user@example.com",
        "assistant_email": "assistant@example.com",
        "assistant_first_name": "Local",
        "assistant_surname": "Assistant",
        "assistant_age": "25",
        "assistant_nationality": "US",
        "assistant_about": "Local runtime test assistant",
        "assistant_timezone": "UTC",
        "assistant_number": "",
        "assistant_whatsapp_number": "",
        "voice_provider": "elevenlabs",
        "voice_id": "voice-123",
        "desktop_mode": "ubuntu",
        "user_desktops": [],
        "team_ids": [],
        "org_id": "",
        "deploy_env": "staging",
        "is_coordinator": False,
        "is_local": is_local,
        "self_contact_id": TEST_SELF_CONTACT_ID,
        "boss_contact_id": TEST_BOSS_CONTACT_ID,
        "secrets": {"MICROSOFT_ACCESS_TOKEN": "test-token"},
    }


def _mock_pubsub():
    mock_future = MagicMock()
    mock_future.result.return_value = "test-message-id"
    mock_publisher = MagicMock()
    mock_publisher.topic_path.return_value = "projects/test/topics/unity-assistant-123"
    mock_publisher.publish.return_value = mock_future
    return mock_publisher


@pytest.mark.parametrize(("is_local", "should_start"), [(True, False), (False, True)])
def test_outlook_notification_respects_local_runtime(
    app_module,
    is_local,
    should_start,
):
    from fastapi.testclient import TestClient

    assistant_data = _assistant_data(is_local=is_local)

    with (
        patch.object(
            app_module,
            "build_webhook_context",
            return_value={"assistant": assistant_data},
        ),
        patch.object(
            app_module,
            "get_outlook_graph_client",
            return_value=(MagicMock(), True),
        ),
        patch.object(
            app_module,
            "get_outlook_thread_id",
            new=AsyncMock(
                return_value=(
                    "conversation-123",
                    "email-123",
                    {"sender": "sender@example.com"},
                ),
            ),
        ),
        patch.object(
            app_module,
            "check_valid_contact",
            return_value=(
                [{"contact_id": TEST_BOSS_CONTACT_ID}],
                True,
                {"contact_id": TEST_BOSS_CONTACT_ID},
            ),
        ),
        patch.object(app_module, "publish_outlook_thread_id") as mock_publish_thread,
        patch.object(app_module, "start_unity_job") as mock_start_unity_job,
    ):
        client = TestClient(app_module.app)
        response = client.post(
            "/email/outlook",
            json={
                "clientState": "test-outlook-secret::assistant@example.com",
                "resource": "/users/test/mailFolders/inbox/Messages/email-123",
            },
        )

    assert response.status_code == 200
    assert response.text == "OK"
    mock_publish_thread.assert_called_once()
    if should_start:
        mock_start_unity_job.assert_called_once_with(assistant_data, "email")
    else:
        mock_start_unity_job.assert_not_called()


@pytest.mark.parametrize(("is_local", "should_start"), [(True, False), (False, True)])
def test_teams_notification_respects_local_runtime(
    app_module,
    is_local,
    should_start,
):
    from fastapi.testclient import TestClient

    assistant_data = _assistant_data(is_local=is_local)
    mock_publisher = _mock_pubsub()
    message_payload = {
        "id": "message-123",
        "from": {
            "user": {
                "displayName": "Sender",
                "userPrincipalName": "sender@example.com",
                "id": "sender-123",
            },
        },
        "body": {"content": "Hello from Teams", "contentType": "text"},
        "createdDateTime": "2026-04-10T00:00:00Z",
        "subject": "",
    }

    with (
        patch.object(
            app_module,
            "build_webhook_context",
            return_value={"assistant": assistant_data},
        ),
        patch.object(
            app_module,
            "check_valid_contact",
            return_value=(
                [{"contact_id": TEST_BOSS_CONTACT_ID}],
                True,
                {"contact_id": TEST_BOSS_CONTACT_ID},
            ),
        ),
        patch.object(app_module, "get_pubsub_client", return_value=mock_publisher),
        patch.object(app_module, "start_unity_job") as mock_start_unity_job,
        patch.object(
            app_module.httpx,
            "AsyncClient",
            return_value=_FakeAsyncClient(message_payload),
        ),
    ):
        client = TestClient(app_module.app)
        response = client.post(
            "/chat/teams",
            json={
                "clientState": "test-teams-secret::assistant@example.com",
                "resource": "/chats/chat-123/messages/message-123",
                "resourceData": {"id": "message-123", "chatId": "chat-123"},
            },
        )

    assert response.status_code == 200
    assert response.text == "OK"
    mock_publisher.publish.assert_called_once()
    if should_start:
        mock_start_unity_job.assert_called_once_with(assistant_data, "teams")
    else:
        mock_start_unity_job.assert_not_called()


def test_teams_notification_us_provisioned_uses_admin_bearer(app_module):
    """Us-provisioned assistants (no MICROSOFT_ACCESS_TOKEN) must fall
    back to an admin bearer token and address Graph chat endpoints via
    ``/users/{assistant_email}/chats/...`` rather than ``/me/chats/...``.
    """
    from fastapi.testclient import TestClient

    assistant_data = _assistant_data(is_local=False)
    assistant_data["secrets"] = {}
    assistant_data["assistant_email"] = "user@tenant.onmicrosoft.com"

    mock_publisher = _mock_pubsub()
    captured_calls: list[dict] = []
    message_payload = {
        "id": "message-123",
        "from": {
            "user": {
                "displayName": "Sender",
                "userPrincipalName": "sender@example.com",
                "id": "sender-123",
                "email": "sender@example.com",
            },
        },
        "body": {"content": "Hello from Teams", "contentType": "text"},
        "createdDateTime": "2026-04-10T00:00:00Z",
        "subject": "",
        "chatType": "oneOnOne",
    }

    with (
        patch.object(
            app_module,
            "build_webhook_context",
            return_value={"assistant": assistant_data},
        ),
        patch.object(
            app_module,
            "check_valid_contact",
            return_value=(
                [{"contact_id": TEST_BOSS_CONTACT_ID}],
                True,
                {"contact_id": TEST_BOSS_CONTACT_ID},
            ),
        ),
        patch.object(app_module, "get_pubsub_client", return_value=mock_publisher),
        patch.object(app_module, "start_unity_job"),
        patch.object(
            app_module,
            "get_admin_graph_bearer_token",
            return_value="admin-bearer-xyz",
        ) as mock_admin_bearer,
        patch.object(
            app_module.httpx,
            "AsyncClient",
            return_value=_FakeAsyncClient(message_payload, calls=captured_calls),
        ),
    ):
        client = TestClient(app_module.app)
        response = client.post(
            "/chat/teams",
            json={
                "clientState": f"test-teams-secret::{assistant_data['assistant_email']}",
                "resource": ("chats('19:uni01_abc@thread.v2')/messages('message-123')"),
                "resourceData": {
                    "id": "message-123",
                    "chatId": "19:uni01_abc@thread.v2",
                },
            },
        )

    assert response.status_code == 200
    assert response.text == "OK"
    mock_admin_bearer.assert_called_once()
    mock_publisher.publish.assert_called_once()
    assert captured_calls, "expected at least one Graph call"
    for call in captured_calls:
        assert (
            "/me/chats/" not in call["url"]
        ), f"us-provisioned path must not use /me, got {call['url']}"
        assert (
            call["headers"].get("Authorization") == "Bearer admin-bearer-xyz"
        ), "us-provisioned path must use admin bearer"
    chat_calls = [c for c in captured_calls if "/chats/" in c["url"]]
    assert chat_calls, "expected at least one chat-scoped Graph call"
    for call in chat_calls:
        assert (
            f"/users/{assistant_data['assistant_email']}/chats/" in call["url"]
            or f"/users/{assistant_data['assistant_email'].replace('@', '%40')}/chats/"
            in call["url"]
        ), f"chat call must be scoped to assistant user, got {call['url']}"


def test_teams_notification_emits_resolved_participants(app_module):
    """Group chat inbound must publish a ``participants`` list with the
    assistant resolved to its self contact, known senders matched to their
    contact row by email, and unknowns preserved with ``contact_id=None``
    so Unity finishes the resolve via its unknown-contact path.
    """
    from fastapi.testclient import TestClient

    assistant_data = _assistant_data(is_local=False)
    assistant_email = assistant_data["assistant_email"]
    chat_id = "19:group_xyz@thread.v2"

    message_payload = {
        "id": "message-123",
        "from": {
            "user": {
                "displayName": "Alice",
                "userPrincipalName": "alice@acme.com",
                "id": "alice-aad-id",
                "email": "alice@acme.com",
            },
        },
        "body": {"content": "Hello group", "contentType": "text"},
        "createdDateTime": "2026-04-10T00:00:00Z",
        "subject": "",
    }

    chat_metadata = {"chatType": "group", "topic": "Team sync"}

    roster_payload = {
        "value": [
            {
                "userId": "assistant-aad-id",
                "email": assistant_email,
                "displayName": "Assistant",
            },
            {
                "userId": "alice-aad-id",
                "email": "alice@acme.com",
                "displayName": "Alice",
            },
            {
                "userId": "bob-aad-id",
                "email": "bob@external.com",
                "displayName": "Bob",
            },
        ],
    }

    contacts = [
        {"contact_id": TEST_SELF_CONTACT_ID, "email_address": assistant_email},
        {
            "contact_id": 5,
            "email_address": "alice@acme.com",
            "first_name": "Alice",
            "surname": "",
        },
    ]

    mock_publisher = _mock_pubsub()
    routed = _RoutedAsyncClient(
        routes=[
            (f"/chats/{quote_chat_id(chat_id)}/members", roster_payload),
            (f"/chats/{chat_id}/members", roster_payload),
            (f"/chats/{chat_id}/messages/message-123", message_payload),
            (f"/chats/{chat_id}?", chat_metadata),
        ],
        default={},
    )

    with (
        patch.object(
            app_module,
            "build_webhook_context",
            return_value={"assistant": assistant_data},
        ),
        patch.object(
            app_module,
            "check_valid_contact",
            return_value=(contacts, True, contacts[1]),
        ),
        patch.object(app_module, "get_pubsub_client", return_value=mock_publisher),
        patch.object(app_module, "start_unity_job"),
        patch.object(app_module.httpx, "AsyncClient", return_value=routed),
    ):
        # Ensure a prior test didn't leave a stale roster cached for this chat.
        app_module._invalidate_teams_roster(
            chat_id=chat_id,
            team_id=None,
            channel_id=None,
        )
        client = TestClient(app_module.app)
        response = client.post(
            "/chat/teams",
            json={
                "clientState": f"test-teams-secret::{assistant_email}",
                "resource": f"/chats/{chat_id}/messages/message-123",
                "resourceData": {"id": "message-123", "chatId": chat_id},
            },
        )

    assert response.status_code == 200
    mock_publisher.publish.assert_called_once()
    import json as _json

    published = _json.loads(mock_publisher.publish.call_args[0][1].decode("utf-8"))
    event = published["event"]
    assert "participants" in event
    assert event.get("participants_incomplete") is False
    by_email = {p.get("email"): p for p in event["participants"]}

    assistant_entry = by_email.get(assistant_email)
    assert assistant_entry is not None, "assistant must be in participants"
    assert assistant_entry["contact_id"] == TEST_SELF_CONTACT_ID

    alice_entry = by_email.get("alice@acme.com")
    assert alice_entry is not None, "known sender must be in participants"
    assert alice_entry["contact_id"] == 5

    bob_entry = by_email.get("bob@external.com")
    assert bob_entry is not None, "unknown member must still be emitted"
    assert bob_entry["contact_id"] is None, (
        "unknown members must carry contact_id=None so Unity resolves via "
        "_get_or_create_unknown_contact rather than the adapter minting rows"
    )


def quote_chat_id(chat_id: str) -> str:
    from urllib.parse import quote

    return quote(chat_id, safe="")


def _run_channel_participants_test(
    app_module,
    *,
    membership_type: str,
    expect_team_members_call: bool,
    expected_reason: str,
):
    """Shared harness for the standard/private/shared channel tests.

    The only meaningful difference between the three flavours is the
    ``membershipType`` payload and the downstream assertion on whether
    ``/teams/{id}/members`` was called — everything else (message,
    mentions, contacts, assertions on sender/mentioned/assistant being
    present) is identical.
    """
    from fastapi.testclient import TestClient

    assistant_data = _assistant_data(is_local=False)
    assistant_email = assistant_data["assistant_email"]
    team_id = "team-xyz"
    channel_id = "19:channel_abc@thread.tacv2"

    message_payload = {
        "id": "message-123",
        "from": {
            "user": {
                "displayName": "Alice",
                "userPrincipalName": "alice@acme.com",
                "id": "alice-aad-id",
                "email": "alice@acme.com",
            },
        },
        "body": {"content": "Hello <at>Bob</at>", "contentType": "html"},
        "createdDateTime": "2026-04-10T00:00:00Z",
        "subject": "",
        "mentions": [
            {
                "id": 0,
                "mentionText": "Bob",
                "mentioned": {
                    "user": {
                        "id": "bob-aad-id",
                        "displayName": "Bob",
                        "userIdentityType": "aadUser",
                    },
                },
            },
        ],
    }

    # Channel messages are fetched from the "recent messages" list and
    # filtered by id; return a list payload whose first entry matches.
    channel_messages_payload = {"value": [message_payload]}

    team_members_payload = {
        "value": [
            {
                "userId": "assistant-aad-id",
                "email": assistant_email,
                "displayName": "Assistant",
            },
            {
                "userId": "alice-aad-id",
                "email": "alice@acme.com",
                "displayName": "Alice",
            },
        ],
    }

    bob_profile_payload = {
        "mail": "bob@external.com",
        "userPrincipalName": "bob@external.com",
    }

    contacts = [
        {"contact_id": TEST_SELF_CONTACT_ID, "email_address": assistant_email},
        {
            "contact_id": 5,
            "email_address": "alice@acme.com",
            "first_name": "Alice",
            "surname": "",
        },
    ]

    mock_publisher = _mock_pubsub()
    captured: list[dict] = []
    routed = _RoutedAsyncClient(
        routes=[
            # Channel metadata lookup for membershipType gating — must
            # come first so the ``$select=membershipType`` query string
            # wins over the less-specific ``/messages`` route below.
            ("$select=membershipType", {"membershipType": membership_type}),
            # Team roster only used for standard channels.
            (f"/teams/{team_id}/members", team_members_payload),
            # Message fetch (the handler queries a "recent messages" list).
            (f"/teams/{team_id}/channels/", channel_messages_payload),
            # Mention enrichment for Bob.
            ("/users/bob-aad-id", bob_profile_payload),
        ],
        default={},
        calls=captured,
    )

    with (
        patch.object(
            app_module,
            "build_webhook_context",
            return_value={"assistant": assistant_data},
        ),
        patch.object(
            app_module,
            "check_valid_contact",
            return_value=(contacts, True, contacts[1]),
        ),
        patch.object(app_module, "get_pubsub_client", return_value=mock_publisher),
        patch.object(app_module, "start_unity_job"),
        patch.object(app_module.httpx, "AsyncClient", return_value=routed),
    ):
        app_module._invalidate_teams_roster(
            chat_id=None,
            team_id=team_id,
            channel_id=channel_id,
        )
        # Membership-type cache is separate; wipe it too so the three
        # tests don't see each other's cached value.
        app_module._teams_membership_type_cache.pop(
            f"{team_id}::{channel_id}",
            None,
        )
        client = TestClient(app_module.app)
        response = client.post(
            "/chat/teams",
            json={
                "clientState": f"test-teams-secret::{assistant_email}",
                "resource": (
                    f"teams('{team_id}')/channels('{channel_id}')"
                    f"/messages('message-123')"
                ),
                "resourceData": {"id": "message-123"},
            },
        )

    assert response.status_code == 200
    mock_publisher.publish.assert_called_once()

    import json as _json

    published = _json.loads(mock_publisher.publish.call_args[0][1].decode("utf-8"))
    event = published["event"]

    assert event.get("participants_reason") == expected_reason, (
        f"expected participants_reason={expected_reason}, "
        f"got {event.get('participants_reason')}"
    )

    by_email = {p.get("email"): p for p in event["participants"]}

    # Sender (Alice) must always be in participants with her contact_id.
    alice_entry = by_email.get("alice@acme.com")
    assert alice_entry is not None, "sender must be in participants"
    assert alice_entry["contact_id"] == 5

    # @mentioned user (Bob) must always be in participants; contact_id
    # stays None because Bob isn't in the contacts dict — Unity's
    # unknown-contact path owns that creation.
    bob_entry = by_email.get("bob@external.com")
    assert bob_entry is not None, "@mentioned user must be in participants"
    assert bob_entry["contact_id"] is None

    # Assistant is always present under its resolved self contact.
    assistant_entry = by_email.get(assistant_email)
    assert assistant_entry is not None, "assistant must always be a participant"
    assert assistant_entry["contact_id"] == TEST_SELF_CONTACT_ID

    team_members_calls = [
        c for c in captured if c["url"].endswith(f"/teams/{team_id}/members")
    ]
    if expect_team_members_call:
        assert team_members_calls, (
            "standard channels must enumerate the team roster via "
            "/teams/{id}/members"
        )
    else:
        assert not team_members_calls, (
            f"{membership_type} channels must not call /teams/{{id}}/members "
            f"— ChannelMember.Read.All is required and not in our scope bundle"
        )


def test_teams_channel_standard_emits_team_roster(app_module):
    """Standard channels resolve participants from the team roster via
    ``/teams/{id}/members`` — the one channel path that works without
    ``ChannelMember.Read.All``.
    """
    _run_channel_participants_test(
        app_module,
        membership_type="standard",
        expect_team_members_call=True,
        expected_reason="ok",
    )


def test_teams_channel_private_uses_mentions_fallback(app_module):
    """Private channels can't enumerate members without
    ``ChannelMember.Read.All``, which we don't request.  Participants
    fall back to sender + @mentioned users, with
    ``participants_reason=private_channel`` so downstream can tell this
    apart from a transient Graph failure.
    """
    _run_channel_participants_test(
        app_module,
        membership_type="private",
        expect_team_members_call=False,
        expected_reason="private_channel",
    )


def test_teams_channel_shared_uses_mentions_fallback(app_module):
    """Shared channels are subject to the same scope gate as private
    ones — no roster, participants fall back to sender + @mentions,
    and the event carries ``participants_reason=shared_channel``.
    """
    _run_channel_participants_test(
        app_module,
        membership_type="shared",
        expect_team_members_call=False,
        expected_reason="shared_channel",
    )
