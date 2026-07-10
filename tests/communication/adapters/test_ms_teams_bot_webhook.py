"""Routing tests for the POST /ms-teams-bot/messages webhook.

Focused on the disconnect/teardown signals: a Teams admin removing the bot
from a team (``conversationUpdate`` with the bot in ``membersRemoved``) and a
user uninstalling their personal install (``installationUpdate`` with
``action="remove"``). Both must funnel to ``revoke_ms_teams_bot_install``;
the ``remove-upgrade`` variant (transient app-upgrade step) must not.
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
            patch.object(app_module, "ensure_ms_teams_bot_pending_install"),
        ):
            resp = client.post(
                "/ms-teams-bot/messages",
                json=_installation_update("add"),
            )
        assert resp.status_code == 200
        revoke.assert_not_called()


class TestMsTeamsBotInstallWelcome:
    def test_personal_add_records_install_and_dms_welcome(self, app_module, client):
        install = {"id": 7, "connect_url": "https://console/x"}
        with (
            patch.object(app_module, "verify_ms_teams_bot_token"),
            patch.object(
                app_module,
                "ensure_ms_teams_bot_pending_install",
                return_value=install,
            ) as ensure,
            patch.object(app_module, "send_ms_teams_bot_install_welcome") as welcome,
            patch.object(app_module, "revoke_ms_teams_bot_install") as revoke,
        ):
            resp = client.post(
                "/ms-teams-bot/messages",
                json=_added_conversation_update(),
            )
        assert resp.status_code == 200
        ensure.assert_called_once()
        welcome.assert_called_once()
        # The DM helper receives the install Orchestra returned.
        assert welcome.call_args.args[1] == install
        revoke.assert_not_called()

    def test_org_install_add_registers_but_does_not_dm(self, app_module, client):
        # ``installationUpdate`` add registers the tenant so the owner can bind
        # it, but must not DM (avoids double-DM with the personal add and org
        # installs may have no 1:1 to message).
        with (
            patch.object(app_module, "verify_ms_teams_bot_token"),
            patch.object(app_module, "ensure_ms_teams_bot_pending_install") as ensure,
            patch.object(app_module, "send_ms_teams_bot_install_welcome") as welcome,
        ):
            resp = client.post(
                "/ms-teams-bot/messages",
                json=_installation_update("add"),
            )
        assert resp.status_code == 200
        ensure.assert_called_once()
        welcome.assert_not_called()
