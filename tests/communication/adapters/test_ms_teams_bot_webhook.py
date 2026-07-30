"""Routing tests for the POST /ms-teams-bot/messages webhook.

Covers the lifecycle signals and the command layer:

* Disconnect/teardown — a Teams admin removing the bot from a team
  (``conversationUpdate`` with the bot in ``membersRemoved``) and a user
  uninstalling their personal install (``installationUpdate`` with
  ``action="remove"``) must both funnel to ``revoke_ms_teams_bot_install``;
  the ``remove-upgrade`` variant (transient app-upgrade step) must not.
* Install welcome — sent once per conversation, gated on the Orchestra claim.
* Commands — which messages the adapter answers deterministically versus
  forwards to the assistant.
"""

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("GCP_SA_KEY", '{"type": "service_account", "project_id": "test"}')
os.environ.setdefault("ORCHESTRA_ADMIN_KEY", "test-admin-key")
os.environ.setdefault("GCP_PROJECT_ID", "test-project")
os.environ.setdefault("ORCHESTRA_URL", "http://localhost:8000")

BOT_ID = "28:bot-app-id"


@pytest.fixture(scope="module")
def app_module():
    from adapters import main

    return main


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    test_client = TestClient(app_module.app)
    test_client.headers["Authorization"] = "Bearer test-token"
    yield test_client


def _removed_conversation_update(tenant_id="tenant-1"):
    return {
        "type": "conversationUpdate",
        "recipient": {"id": BOT_ID},
        "membersRemoved": [{"id": BOT_ID}],
        "channelData": {"tenant": {"id": tenant_id}},
    }


def _installation_update(action, tenant_id="tenant-1"):
    return {
        "type": "installationUpdate",
        "action": action,
        "recipient": {"id": BOT_ID},
        "channelData": {"tenant": {"id": tenant_id}},
    }


def _added_conversation_update(tenant_id="tenant-1"):
    return {
        "type": "conversationUpdate",
        "recipient": {"id": BOT_ID},
        "membersAdded": [{"id": BOT_ID}],
        "from": {"aadObjectId": "installer-aad"},
        "conversation": {"id": "conv-1"},
        "serviceUrl": "https://smba.example/",
        "channelData": {"tenant": {"id": tenant_id}},
    }


def _added_team_conversation_update(tenant_id="tenant-1"):
    return {
        "type": "conversationUpdate",
        "recipient": {"id": BOT_ID},
        "membersAdded": [{"id": BOT_ID}],
        "conversation": {"id": "channel-1", "conversationType": "channel"},
        "serviceUrl": "https://smba.example/",
        "channelData": {"tenant": {"id": tenant_id}, "team": {"id": "team-1"}},
    }


def _message_activity(tenant_id="tenant-1", text="hi"):
    return {
        "type": "message",
        "text": text,
        "recipient": {"id": BOT_ID},
        "from": {"aadObjectId": "sender-aad", "name": "Reviewer"},
        "conversation": {"id": "conv-1", "conversationType": "personal"},
        "serviceUrl": "https://smba.example/",
        "channelData": {"tenant": {"id": tenant_id}},
    }


class TestMsTeamsBotDisconnect:
    def test_team_removal_revokes_install(self, app_module, client):
        with (
            patch.object(app_module, "verify_ms_teams_bot_token"),
            patch.object(app_module, "revoke_ms_teams_bot_install") as revoke,
            patch.object(app_module, "ensure_ms_teams_bot_pending_install") as ensure,
        ):
            resp = client.post(
                "/ms-teams-bot/messages",
                json=_removed_conversation_update(tenant_id="tenant-9"),
            )
        assert resp.status_code == 200
        revoke.assert_called_once()
        ensure.assert_not_called()

    def test_personal_uninstall_revokes_install(self, app_module, client):
        with (
            patch.object(app_module, "verify_ms_teams_bot_token"),
            patch.object(app_module, "revoke_ms_teams_bot_install") as revoke,
        ):
            resp = client.post(
                "/ms-teams-bot/messages",
                json=_installation_update("remove"),
            )
        assert resp.status_code == 200
        revoke.assert_called_once()

    def test_remove_upgrade_does_not_revoke(self, app_module, client):
        with (
            patch.object(app_module, "verify_ms_teams_bot_token"),
            patch.object(app_module, "revoke_ms_teams_bot_install") as revoke,
        ):
            resp = client.post(
                "/ms-teams-bot/messages",
                json=_installation_update("remove-upgrade"),
            )
        assert resp.status_code == 200
        revoke.assert_not_called()

    def test_install_add_does_not_revoke(self, app_module, client):
        with (
            patch.object(app_module, "verify_ms_teams_bot_token"),
            patch.object(app_module, "revoke_ms_teams_bot_install") as revoke,
            patch.object(
                app_module,
                "ensure_ms_teams_bot_pending_install",
                return_value={"id": 1, "created": False},
            ),
            patch.object(app_module, "send_ms_teams_bot_install_welcome"),
        ):
            resp = client.post(
                "/ms-teams-bot/messages",
                json=_installation_update("add"),
            )
        assert resp.status_code == 200
        revoke.assert_not_called()


class TestMsTeamsBotInstallWelcome:
    def test_personal_add_welcomes(self, app_module, client):
        # A personal-scope bot-add greets in the 1:1 conversation once the
        # per-conversation welcome claim is won.
        install = {"id": 7, "connect_url": "https://console/x"}
        with (
            patch.object(app_module, "verify_ms_teams_bot_token"),
            patch.object(
                app_module,
                "ensure_ms_teams_bot_pending_install",
                return_value=install,
            ) as ensure,
            patch.object(
                app_module,
                "claim_ms_teams_bot_welcome",
                return_value=True,
            ) as claim,
            patch.object(app_module, "send_ms_teams_bot_install_welcome") as welcome,
            patch.object(app_module, "revoke_ms_teams_bot_install") as revoke,
        ):
            resp = client.post(
                "/ms-teams-bot/messages",
                json=_added_conversation_update(),
            )
        assert resp.status_code == 200
        ensure.assert_called_once()
        # The welcome is claimed per (install, conversation) before sending.
        claim.assert_called_once_with(7, "conv-1")
        welcome.assert_called_once()
        # The DM helper receives the install Orchestra returned.
        assert welcome.call_args.args[1] == install
        revoke.assert_not_called()

    def test_team_add_welcomes_even_when_not_created(self, app_module, client):
        # Regression for the Teams Store failure "welcome not triggered in team
        # scope": a team-channel bot-add lands on an already-registered tenant
        # (``created`` false), yet must still welcome — the welcome is keyed on
        # the per-conversation claim, not on the per-tenant install row.
        install = {"id": 7, "created": False, "connect_url": "https://console/x"}
        with (
            patch.object(app_module, "verify_ms_teams_bot_token"),
            patch.object(
                app_module,
                "ensure_ms_teams_bot_pending_install",
                return_value=install,
            ) as ensure,
            patch.object(
                app_module,
                "claim_ms_teams_bot_welcome",
                return_value=True,
            ) as claim,
            patch.object(app_module, "send_ms_teams_bot_install_welcome") as welcome,
        ):
            resp = client.post(
                "/ms-teams-bot/messages",
                json=_added_team_conversation_update(),
            )
        assert resp.status_code == 200
        ensure.assert_called_once()
        claim.assert_called_once_with(7, "channel-1")
        welcome.assert_called_once()
        assert welcome.call_args.args[1] == install

    def test_redelivered_add_does_not_rewelcome(self, app_module, client):
        # Teams / the Bot Connector redeliver the bot-add for a conversation
        # that has already been welcomed. The claim comes back False, so the
        # bot must stay silent (Store "no repeating welcome messages" rule).
        install = {"id": 7, "connect_url": "https://console/x"}
        with (
            patch.object(app_module, "verify_ms_teams_bot_token"),
            patch.object(
                app_module,
                "ensure_ms_teams_bot_pending_install",
                return_value=install,
            ),
            patch.object(
                app_module,
                "claim_ms_teams_bot_welcome",
                return_value=False,
            ) as claim,
            patch.object(app_module, "send_ms_teams_bot_install_welcome") as welcome,
        ):
            resp = client.post(
                "/ms-teams-bot/messages",
                json=_added_conversation_update(),
            )
        assert resp.status_code == 200
        claim.assert_called_once_with(7, "conv-1")
        welcome.assert_not_called()

    def test_install_add_registers_without_welcome(self, app_module, client):
        # ``installationUpdate`` add only registers the tenant; the welcome flows
        # through the sibling ``conversationUpdate`` bot-add, so welcoming here
        # too would double-send.
        with (
            patch.object(app_module, "verify_ms_teams_bot_token"),
            patch.object(
                app_module,
                "ensure_ms_teams_bot_pending_install",
                return_value={"id": 8, "created": True},
            ) as ensure,
            patch.object(app_module, "send_ms_teams_bot_install_welcome") as welcome,
        ):
            resp = client.post(
                "/ms-teams-bot/messages",
                json=_installation_update("add"),
            )
        assert resp.status_code == 200
        ensure.assert_called_once()
        welcome.assert_not_called()


class TestMsTeamsBotCommandReplies:
    """The bot must answer generic commands, and answer them differently.

    On an unbound install every message is routed to the deterministic command
    layer with the classified command, so "hi", "help", and unrecognised input
    get distinct cards instead of one canned connect blurb (which read to the
    Store reviewer as the welcome message re-triggering on every command).
    """

    def _dispatch(self, app_module, client, data, text):
        with (
            patch.object(app_module, "verify_ms_teams_bot_token"),
            patch.object(
                app_module,
                "resolve_ms_teams_bot_inbound",
                return_value=data,
            ),
            patch.object(app_module, "send_ms_teams_bot_command_reply") as reply,
            # ``None`` stops the handler before the Pub/Sub fan-out; reaching
            # this call at all is what distinguishes "forwarded to the assistant"
            # from "answered by the command layer".
            patch.object(
                app_module,
                "get_assistant",
                return_value=None,
            ) as get_assistant,
        ):
            resp = client.post(
                "/ms-teams-bot/messages",
                json=_message_activity(text=text),
            )
        assert resp.status_code == 200
        return reply, get_assistant

    @pytest.mark.parametrize(
        ("text", "command"),
        [("hi", "greeting"), ("Help", "help"), ("do the thing", "other")],
    )
    def test_pending_install_replies_per_command(
        self,
        app_module,
        client,
        text,
        command,
    ):
        data = {
            "handled": False,
            "install_state": "pending",
            "connect_url": "https://console/z",
        }
        reply, _ = self._dispatch(app_module, client, data, text)
        reply.assert_called_once()
        assert reply.call_args.args[1] == command
        # The connect link rides along so every reply offers a way forward.
        assert reply.call_args.args[2] == "https://console/z"

    def test_message_on_no_install_does_not_reply(self, app_module, client):
        data = {"handled": False, "install_state": "none", "connect_url": None}
        reply, _ = self._dispatch(app_module, client, data, "hi")
        reply.assert_not_called()

    def test_unmentioned_channel_message_stays_silent(self, app_module, client):
        # A bound install returns handled=False for channel chatter the bot was
        # not addressed in. Replying there would make it answer every message.
        data = {"handled": False, "install_state": "bound", "connect_url": None}
        reply, _ = self._dispatch(app_module, client, data, "morning all")
        reply.assert_not_called()

    def test_help_on_bound_install_is_answered_deterministically(
        self,
        app_module,
        client,
    ):
        # ``help`` is a bot meta-command: answered from the adapter with the full
        # command list rather than forwarded to the assistant to improvise.
        data = {"handled": True, "install_state": "bound", "assistant_id": 42}
        reply, get_assistant = self._dispatch(app_module, client, data, "help")
        reply.assert_called_once()
        assert reply.call_args.args[1] == "help"
        assert reply.call_args.args[2] is None
        get_assistant.assert_not_called()

    def test_real_request_on_bound_install_reaches_the_assistant(
        self,
        app_module,
        client,
    ):
        # Exact-phrase classification: a request that merely contains "help" is
        # work for the assistant, not the help command.
        data = {"handled": True, "install_state": "bound", "assistant_id": 42}
        reply, get_assistant = self._dispatch(
            app_module,
            client,
            data,
            "help me draft this email",
        )
        reply.assert_not_called()
        get_assistant.assert_called_once()

    def test_greeting_on_bound_install_reaches_the_assistant(
        self,
        app_module,
        client,
    ):
        # A connected user greeting their teammate gets a real reply; only the
        # unbound path needs the canned greeting.
        data = {"handled": True, "install_state": "bound", "assistant_id": 42}
        reply, get_assistant = self._dispatch(app_module, client, data, "hi")
        reply.assert_not_called()
        get_assistant.assert_called_once()
