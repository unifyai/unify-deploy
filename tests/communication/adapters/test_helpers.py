"""
Unit tests for the adapters helper functions.

These tests verify:
- Contact handling logic (WhatsApp is validated via Orchestra's resolve endpoint,
  not through check_contact_details)
"""

from datetime import datetime, timedelta, timezone
import json
import requests
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from adapters.helpers import (
    START_INTENT_DISPATCH_TIMEOUT_SECONDS,
    build_webhook_context,
    cleanup_idle_pool,
    check_valid_contact,
    create_conference_response,
    expire_all_stale_jobs,
    get_default_contacts,
    get_assistant,
    get_unity_jobs_inventory,
    check_contact_details,
    classify_ms_teams_bot_command,
    dispatch_unity_start_intent,
    replenish_idle_pool,
    revoke_ms_teams_bot_install,
    send_ms_teams_bot_command_reply,
    send_ms_teams_bot_install_welcome,
    start_unity_job,
)
from adapters.helpers import _build_start_job_request_data
from common.settings import SETTINGS
from communication.infra.views import _decode_owner_team_id_form

TEST_SELF_CONTACT_ID = 42
TEST_BOSS_CONTACT_ID = 43

# --- get_default_contacts tests ---


def test_get_default_contacts_includes_whatsapp_number():
    """Verify contacts include whatsapp_number field from assistant data."""
    assistant_data = {
        "assistant_first_name": "Test",
        "assistant_surname": "Assistant",
        "assistant_email": "test@example.com",
        "assistant_number": "+1234567890",
        "assistant_whatsapp_number": "+1112223333",
        "user_first_name": "Test",
        "user_surname": "User",
        "user_email": "user@example.com",
        "user_number": "+0987654321",
        "user_whatsapp_number": "+4445556666",
        "self_contact_id": TEST_SELF_CONTACT_ID,
        "boss_contact_id": TEST_BOSS_CONTACT_ID,
    }
    contacts = get_default_contacts(assistant_data)

    assert contacts[0]["whatsapp_number"] == "+1112223333"  # assistant
    assert contacts[1]["whatsapp_number"] == "+4445556666"  # user


def test_get_default_contacts_includes_phone_number():
    """Verify contacts include phone_number field."""
    assistant_data = {
        "assistant_first_name": "Test",
        "assistant_surname": "Assistant",
        "assistant_email": "test@example.com",
        "assistant_number": "+1234567890",
        "user_first_name": "Test",
        "user_surname": "User",
        "user_email": "user@example.com",
        "user_number": "+0987654321",
        "self_contact_id": TEST_SELF_CONTACT_ID,
        "boss_contact_id": TEST_BOSS_CONTACT_ID,
    }
    contacts = get_default_contacts(assistant_data)

    assert len(contacts) == 2
    assert contacts[0]["phone_number"] == "+1234567890"  # assistant
    assert contacts[1]["phone_number"] == "+0987654321"  # user


def test_get_default_contacts_uses_resolved_contact_ids():
    """Fallback contacts use the assistant's resolved self and boss ids."""
    assistant_data = {
        "assistant_first_name": "Test",
        "assistant_surname": "Assistant",
        "assistant_email": "test@example.com",
        "assistant_number": "+1234567890",
        "user_first_name": "Test",
        "user_surname": "User",
        "user_email": "user@example.com",
        "user_number": "+0987654321",
        "self_contact_id": TEST_SELF_CONTACT_ID,
        "boss_contact_id": TEST_BOSS_CONTACT_ID,
    }

    contacts = get_default_contacts(assistant_data)

    assert contacts[0]["contact_id"] == TEST_SELF_CONTACT_ID
    assert contacts[1]["contact_id"] == TEST_BOSS_CONTACT_ID


def test_get_default_contacts_requires_resolved_contact_ids():
    """Fallback contacts fail loudly when assistant identity has not resolved."""
    with pytest.raises(ValueError, match="self_contact_id"):
        get_default_contacts(
            {
                "assistant_first_name": "Test",
                "assistant_surname": "Assistant",
                "assistant_email": "test@example.com",
                "assistant_number": "+1234567890",
                "user_first_name": "Test",
                "user_surname": "User",
                "user_email": "user@example.com",
                "user_number": "+0987654321",
            },
        )


# --- check_contact_details tests ---


def test_check_contact_details_whatsapp_not_handled():
    """WhatsApp validation is handled by Orchestra's resolve endpoint, not here."""
    result = check_contact_details(
        phone_number="+1111111111",
        medium="whatsapp",
        user_number="+2222222222",
        user_whatsapp_number="+1111111111",
    )
    assert result is False


def test_check_contact_details_sms_uses_user_number():
    """Verify SMS/msg medium uses user_number parameter."""
    result = check_contact_details(
        phone_number="+1111111111",
        medium="msg",
        user_number="+1111111111",
        user_whatsapp_number="+2222222222",
    )
    assert result is True


def test_check_contact_details_phone_uses_user_number():
    """Verify phone medium uses user_number parameter."""
    result = check_contact_details(
        phone_number="+1111111111",
        medium="phone",
        user_number="+1111111111",
        user_whatsapp_number="+2222222222",
    )
    assert result is True


def test_check_contact_details_email_uses_user_email():
    """Verify email medium uses user_email parameter."""
    result = check_contact_details(
        email_address="user@example.com",
        medium="email",
        user_email="user@example.com",
    )
    assert result is True


def test_check_contact_details_email_mismatch():
    """Verify email medium returns False on mismatch."""
    result = check_contact_details(
        email_address="sender@example.com",
        medium="email",
        user_email="user@example.com",
    )
    assert result is False


def test_check_contact_details_returns_false_for_unknown_medium():
    """Verify unknown medium returns False."""
    result = check_contact_details(
        phone_number="+1111111111",
        medium="unknown",
        user_number="+1111111111",
    )
    assert result is False


@patch("adapters.helpers.get_contacts")
def test_check_valid_contact_uses_resolved_boss_contact_id(mock_get_contacts):
    """Inbound boss validation follows the resolved boss id, not contact 1."""
    assistant_data = {
        "assistant_first_name": "Test",
        "assistant_surname": "Assistant",
        "assistant_email": "assistant@example.com",
        "assistant_number": "+1234567890",
        "user_first_name": "Boss",
        "user_surname": "User",
        "user_email": "boss@example.com",
        "user_number": "+0987654321",
        "user_whatsapp_number": "+0987654321",
        "self_contact_id": 42,
        "boss_contact_id": 43,
    }
    mock_get_contacts.return_value = (
        {
            "logs": [
                {
                    "entries": {
                        "contact_id": 42,
                        "first_name": "Test",
                        "surname": "Assistant",
                        "email_address": "assistant@example.com",
                        "phone_number": "+1234567890",
                    },
                },
                {
                    "entries": {
                        "contact_id": 43,
                        "first_name": "Boss",
                        "surname": "User",
                        "email_address": "boss@example.com",
                        "phone_number": "+0987654321",
                    },
                },
            ],
        },
        200,
    )

    contacts, is_valid, matched_contact = check_valid_contact(
        email_address="boss@example.com",
        medium="email",
        assistant_context="user-123/assistant-123",
        api_key="test-api-key",
        user_email="boss@example.com",
        assistant_data=assistant_data,
    )

    assert is_valid is True
    assert matched_contact["contact_id"] == 43
    assert [contact["contact_id"] for contact in contacts] == [42, 43]


# --- start_unity_job tests ---


def _create_mock_assistant_data(desktop_mode="none"):
    """Create mock assistant data for testing.

    Args:
        desktop_mode: Desktop mode - use "none" to skip VM start call in tests
    """
    data = {
        "api_key": "test-api-key",
        "assistant_id": "12345",
        "user_id": "user-123",
        "user_first_name": "Test",
        "user_surname": "User",
        "user_email": "test@example.com",
        "assistant_first_name": "Test",
        "assistant_surname": "Assistant",
        "assistant_age": "25",
        "assistant_nationality": "US",
        "assistant_about": "Test assistant",
        "assistant_timezone": "UTC",
        "user_number": "+1234567890",
        "assistant_number": "+0987654321",
        "assistant_email": "assistant@example.com",
        "user_whatsapp_number": "+1234567890",
        "assistant_whatsapp_number": "+18501234567",
        "voice_provider": "elevenlabs",
        "voice_id": "voice-123",
        "desktop_mode": desktop_mode,  # Use "none" to skip VM start
        "user_desktops": [],
        "is_coordinator": False,
        "is_local": False,
        "team_ids": [11, 22],
        "team_summaries": [
            {
                "team_id": 11,
                "name": "Ops",
                "description": "Operations workspace for customer support.",
            },
        ],
        "self_contact_id": 42,
        "boss_contact_id": 43,
    }
    if desktop_mode in ("ubuntu", "windows"):
        data["managed_desktop_status"] = "active"
    return data


def _orchestra_assistant_record(**overrides):
    record = {
        "agent_id": "12345",
        "deploy_env": None,
        "user_id": "user-123",
        "api_key": "test-api-key",
        "user_first_name": "Test",
        "user_last_name": "User",
        "first_name": "Test",
        "surname": "Assistant",
        "age": 25,
        "nationality": "US",
        "about": "Test assistant",
        "job_title": "",
        "timezone": "UTC",
        "phone": "+0987654321",
        "assistant_whatsapp_number": "+18501234567",
        "assistant_discord_bot_id": "",
        "email": "assistant@example.com",
        "email_provider": "google_workspace",
        "user_phone": "+1234567890",
        "user_whatsapp_number": "+1234567890",
        "user_email": "test@example.com",
        "voice_provider": "elevenlabs",
        "voice_id": "voice-123",
        "secrets": {},
        "desktop_mode": "none",
        "user_desktops": [],
        "is_local": False,
        "is_coordinator": True,
        "team_ids": [],
        "self_contact_id": TEST_SELF_CONTACT_ID,
        "boss_contact_id": TEST_BOSS_CONTACT_ID,
        "organization_id": None,
    }
    record.update(overrides)
    return record


def test_get_assistant_local_payload_defaults_to_non_coordinator():
    """Local assistants should carry the same Coordinator flag shape as Orchestra rows."""

    assistant_data = get_assistant(assistant_id="local-assistant")

    assert assistant_data["is_coordinator"] is False


@patch("adapters.helpers.requests.get")
def test_get_assistant_preserves_coordinator_flag_from_orchestra(mock_get):
    """Coordinator lookups preserve role and repair missing desktop mode."""

    mock_get.return_value = MagicMock(
        json=MagicMock(
            return_value={
                "info": [
                    _orchestra_assistant_record(
                        is_coordinator=True,
                        desktop_mode=None,
                    ),
                ],
            },
        ),
    )

    assistant_data = get_assistant(assistant_id="12345")

    assert assistant_data["is_coordinator"] is True
    assert assistant_data["desktop_mode"] == "none"


@patch("adapters.helpers.requests.post")
@patch.dict("os.environ", {"ORCHESTRA_ADMIN_KEY": "test-key"})
def test_dispatch_unity_start_intent_includes_wake_reasons(mock_post):
    """Wake reasons should be serialized onto the start-intent form payload."""

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response
    assistant_data = _create_mock_assistant_data()
    assistant_data["desktop_mode"] = None
    assistant_data["is_coordinator"] = True
    wake_reasons = [{"type": "task_due", "task_id": 101}]

    response = dispatch_unity_start_intent(
        assistant_data,
        "api_message",
        wake_reasons=wake_reasons,
        timeout_seconds=12,
    )

    assert response is mock_response
    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["timeout"] == 12
    assert json.loads(call_kwargs["data"]["wake_reasons"]) == wake_reasons
    assert call_kwargs["data"]["medium"] == "api_message"
    assert call_kwargs["data"]["is_coordinator"] == "true"
    assert call_kwargs["data"]["desktop_mode"] == "none"


def test_call_activation_defers_desktop_binding():
    from adapters.helpers import call_activation_defers_desktop_binding

    assistant = {"desktop_mode": "ubuntu", "managed_desktop_status": "active"}
    assert call_activation_defers_desktop_binding("unify_meet", assistant)
    assert call_activation_defers_desktop_binding("phone", assistant)
    assert call_activation_defers_desktop_binding("whatsapp_call", assistant)
    assert not call_activation_defers_desktop_binding("email", assistant)
    assert not call_activation_defers_desktop_binding(
        "unify_meet",
        {"desktop_mode": "none"},
    )


@patch("adapters.helpers.requests.post")
@patch.dict("os.environ", {"ORCHESTRA_ADMIN_KEY": "test-key"})
def test_dispatch_unity_start_intent_includes_desktop_required_override(mock_post):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response
    assistant_data = _create_mock_assistant_data()
    assistant_data["desktop_mode"] = "ubuntu"
    assistant_data["managed_desktop_status"] = "active"

    dispatch_unity_start_intent(
        assistant_data,
        "unify_meet",
        desktop_required=False,
    )

    assert mock_post.call_args.kwargs["data"]["desktop_required"] == "false"


@patch("adapters.helpers.requests.post")
@patch.dict("os.environ", {"ORCHESTRA_ADMIN_KEY": "test-key"})
def test_dispatch_unity_start_intent_encodes_team_ids_for_form(mock_post):
    """Start-intent form payloads carry memberships as JSON strings."""

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response
    assistant_data = _create_mock_assistant_data()

    response = dispatch_unity_start_intent(assistant_data, "api_message")

    assert response is mock_response
    data = mock_post.call_args.kwargs["data"]
    assert json.loads(data["team_ids"]) == [11, 22]
    assert json.loads(data["team_summaries"]) == [
        {
            "team_id": 11,
            "name": "Ops",
            "description": "Operations workspace for customer support.",
        },
    ]
    assert data["self_contact_id"] == "42"
    assert data["boss_contact_id"] == "43"


@patch("adapters.helpers.requests.post")
@patch.dict("os.environ", {"ORCHESTRA_ADMIN_KEY": "test-key"})
def test_dispatch_unity_start_intent_returns_none_without_api_key(mock_post):
    """Assistants without API keys should not dispatch start intent requests."""

    assistant_data = _create_mock_assistant_data()
    assistant_data["api_key"] = ""

    response = dispatch_unity_start_intent(assistant_data, "api_message")

    assert response is None
    mock_post.assert_not_called()


@patch("adapters.helpers.requests.get")
def test_get_assistant_preserves_team_ids(mock_get):
    """Assistant lookups preserve live membership ids from Orchestra."""

    mock_get.return_value = MagicMock(
        json=MagicMock(
            return_value={
                "info": [
                    {
                        "agent_id": "assistant-123",
                        "deploy_env": "staging",
                        "user_id": "user-123",
                        "api_key": "test-api-key",
                        "user_first_name": "Test",
                        "user_last_name": "User",
                        "first_name": "Test",
                        "surname": "Assistant",
                        "age": 25,
                        "nationality": "US",
                        "about": "Test assistant",
                        "job_title": "",
                        "timezone": "UTC",
                        "phone": "+1987654321",
                        "assistant_whatsapp_number": "+18501234567",
                        "assistant_discord_bot_id": "",
                        "email": "assistant@example.com",
                        "email_provider": "google_workspace",
                        "user_phone": "+1234567890",
                        "user_whatsapp_number": "+1234567890",
                        "user_email": "test@example.com",
                        "voice_provider": "elevenlabs",
                        "voice_id": "voice-123",
                        "secrets": {},
                        "desktop_mode": "none",
                        "user_desktops": [],
                        "is_local": False,
                        "team_ids": [3, 4],
                        "team_summaries": [
                            {
                                "team_id": 3,
                                "name": "Support",
                                "description": "Support workspace for customer issues.",
                            },
                        ],
                        "self_contact_id": 42,
                        "boss_contact_id": 43,
                        "organization_id": 42,
                    },
                ],
            },
        ),
    )

    assistant = get_assistant(assistant_id="assistant-123")

    assert assistant["team_ids"] == [3, 4]
    assert assistant["team_summaries"] == [
        {
            "team_id": 3,
            "name": "Support",
            "description": "Support workspace for customer issues.",
        },
    ]
    assert assistant["self_contact_id"] == 42
    assert assistant["boss_contact_id"] == 43


@patch("adapters.helpers._fetch_infra_jobs")
def test_get_unity_jobs_inventory_uses_explicit_lookback(mock_fetch_infra_jobs):
    mock_response = MagicMock()
    mock_response.json.return_value = {"jobs": []}
    mock_fetch_infra_jobs.return_value = mock_response

    inventory = get_unity_jobs_inventory()

    assert inventory == {"running": [], "idle": []}
    mock_fetch_infra_jobs.assert_called_once()
    params = mock_fetch_infra_jobs.call_args.args[0]
    assert params["hours"] == SETTINGS.job_inventory_lookback_hours
    assert params["label_selector"] == "app=unity,unity-status!=done"


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers.requests.get")
@patch("adapters.helpers.get_target_idle_count")
@patch("adapters.helpers.get_unity_jobs_inventory")
def test_replenish_idle_pool_honors_extra_demand(
    mock_get_unity_jobs_inventory,
    mock_get_target_idle_count,
    mock_requests_get,
    mock_requests_post,
):
    mock_get_unity_jobs_inventory.return_value = {
        "running": [{"job_name": "running-1"}],
        "idle": [
            {
                "job_name": "idle-1",
                "labels": {"unity-status": "idle", "unity-image-hash": "abc123"},
            },
        ],
    }
    mock_get_target_idle_count.return_value = SimpleNamespace(
        target=3,
        demand_exceeds_floor=False,
    )
    mock_requests_get.return_value = MagicMock(
        json=MagicMock(return_value={"commit_hash": "abc123"}),
    )
    mock_requests_post.return_value = MagicMock(
        json=MagicMock(return_value={"status": "dispatched"}),
    )

    result = replenish_idle_pool(extra_demand=4)

    assert result["mode"] == "fill-reactive"
    assert result["created"] == 3
    assert result["target"] == 4
    assert result["extra_demand"] == 4
    assert mock_requests_post.call_count == 3


@patch("adapters.helpers._fetch_current_image_hash", return_value=None)
@patch("adapters.helpers.requests.get")
@patch("adapters.helpers.get_target_idle_count")
@patch("adapters.helpers.get_unity_jobs_inventory")
def test_cleanup_idle_pool_uses_explicit_lookback_for_idle_listing(
    mock_get_unity_jobs_inventory,
    mock_get_target_idle_count,
    mock_requests_get,
    _mock_fetch_hash,
):
    mock_get_unity_jobs_inventory.return_value = {"running": [], "idle": []}
    mock_get_target_idle_count.return_value = SimpleNamespace(target=0)
    mock_response = MagicMock()
    mock_response.json.return_value = {"jobs": []}
    mock_requests_get.return_value = mock_response

    result = cleanup_idle_pool()

    assert result["deleted"] == 0
    assert result["deleted_stale_hash"] == 0
    assert result["deleted_quota"] == 0
    assert result["running"] == 0
    mock_requests_get.assert_called_once()
    assert (
        mock_requests_get.call_args.kwargs["params"]["hours"]
        == SETTINGS.job_inventory_lookback_hours
    )


def _idle_job(job_name: str, image_hash: str | None = "abc123") -> dict:
    labels = {"unity-status": "idle"}
    if image_hash is not None:
        labels["unity-image-hash"] = image_hash
    return {"job_name": job_name, "labels": labels, "resource_version": "1"}


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers.requests.get")
@patch("adapters.helpers.get_target_idle_count")
@patch("adapters.helpers.get_unity_jobs_inventory")
@patch("adapters.helpers._fetch_current_image_hash", return_value="newhash")
def test_replenish_idle_pool_creates_jobs_when_only_stale_idle_exist(
    _mock_fetch_hash,
    mock_get_unity_jobs_inventory,
    mock_get_target_idle_count,
    mock_requests_get,
    mock_requests_post,
):
    mock_get_unity_jobs_inventory.return_value = {
        "running": [],
        "idle": [
            _idle_job(f"unity-2026-01-01-00-00-00-stale{i}", "oldhash")
            for i in range(6)
        ],
    }
    mock_get_target_idle_count.return_value = SimpleNamespace(
        target=3,
        demand_exceeds_floor=True,
    )
    mock_requests_get.return_value = MagicMock(
        json=MagicMock(return_value={"commit_hash": "newhash"}),
    )
    mock_requests_post.return_value = MagicMock(
        json=MagicMock(return_value={"status": "dispatched"}),
    )

    result = replenish_idle_pool()

    assert result["mode"] == "fill-demand"
    assert result["created"] == 3
    assert result["matching"] == 0
    assert result["stale"] == 6
    assert mock_requests_post.call_count == 3


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers.requests.get")
@patch("adapters.helpers.get_target_idle_count")
@patch("adapters.helpers.get_unity_jobs_inventory")
@patch("adapters.helpers._fetch_current_image_hash", return_value="newhash")
def test_replenish_idle_pool_counts_only_matching_idle_jobs(
    _mock_fetch_hash,
    mock_get_unity_jobs_inventory,
    mock_get_target_idle_count,
    mock_requests_get,
    mock_requests_post,
):
    mock_get_unity_jobs_inventory.return_value = {
        "running": [],
        "idle": [
            _idle_job("unity-2026-01-01-00-00-00-match", "newhash"),
            *[
                _idle_job(f"unity-2026-01-01-00-00-00-stale{i}", "oldhash")
                for i in range(3)
            ],
        ],
    }
    mock_get_target_idle_count.return_value = SimpleNamespace(
        target=3,
        demand_exceeds_floor=True,
    )
    mock_requests_get.return_value = MagicMock(
        json=MagicMock(return_value={"commit_hash": "newhash"}),
    )
    mock_requests_post.return_value = MagicMock(
        json=MagicMock(return_value={"status": "dispatched"}),
    )

    result = replenish_idle_pool()

    assert result["created"] == 2
    assert result["matching"] == 1
    assert result["stale"] == 3


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers.requests.get")
@patch("adapters.helpers.get_target_idle_count")
@patch("adapters.helpers.get_unity_jobs_inventory")
@patch("adapters.helpers._fetch_current_image_hash", return_value="newhash")
def test_replenish_idle_pool_refresh_gap_fills_when_only_stale_idle_exist(
    _mock_fetch_hash,
    mock_get_unity_jobs_inventory,
    mock_get_target_idle_count,
    mock_requests_get,
    mock_requests_post,
):
    mock_get_unity_jobs_inventory.return_value = {
        "running": [],
        "idle": [_idle_job("unity-2026-01-01-00-00-00-stale", "oldhash")],
    }
    mock_get_target_idle_count.return_value = SimpleNamespace(
        target=3,
        demand_exceeds_floor=True,
    )
    mock_requests_get.return_value = MagicMock(
        json=MagicMock(return_value={"commit_hash": "newhash"}),
    )
    mock_requests_post.return_value = MagicMock(
        json=MagicMock(return_value={"status": "dispatched"}),
    )

    result = replenish_idle_pool(refresh=True)

    assert result["mode"] == "refresh"
    assert result["created"] == 3
    assert result["matching"] == 0
    assert result["stale"] == 1
    assert mock_requests_post.call_count == 3


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers.requests.get")
@patch("adapters.helpers.get_target_idle_count")
@patch("adapters.helpers.get_unity_jobs_inventory")
@patch("adapters.helpers._fetch_current_image_hash", return_value="newhash")
def test_replenish_idle_pool_refresh_creates_zero_when_matching_at_target(
    _mock_fetch_hash,
    mock_get_unity_jobs_inventory,
    mock_get_target_idle_count,
    mock_requests_get,
    mock_requests_post,
):
    mock_get_unity_jobs_inventory.return_value = {
        "running": [],
        "idle": [
            _idle_job(f"unity-2026-01-01-00-00-0{i}-match", "newhash") for i in range(3)
        ],
    }
    mock_get_target_idle_count.return_value = SimpleNamespace(
        target=3,
        demand_exceeds_floor=False,
    )
    mock_requests_get.return_value = MagicMock(
        json=MagicMock(return_value={"commit_hash": "newhash"}),
    )

    result = replenish_idle_pool(refresh=True)

    assert result["status"] == "healthy"
    assert result["matching"] == 3
    assert result["target"] == 3
    mock_requests_post.assert_not_called()


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers.requests.get")
@patch("adapters.helpers.get_target_idle_count")
@patch("adapters.helpers.get_unity_jobs_inventory")
@patch("adapters.helpers._fetch_current_image_hash", return_value="newhash")
def test_replenish_idle_pool_floor_regime_gap_fills(
    _mock_fetch_hash,
    mock_get_unity_jobs_inventory,
    mock_get_target_idle_count,
    mock_requests_get,
    mock_requests_post,
):
    mock_get_unity_jobs_inventory.return_value = {
        "running": [],
        "idle": [_idle_job("unity-2026-01-01-00-00-00-match", "newhash")],
    }
    mock_get_target_idle_count.return_value = SimpleNamespace(
        target=3,
        demand_exceeds_floor=False,
    )
    mock_requests_get.return_value = MagicMock(
        json=MagicMock(return_value={"commit_hash": "newhash"}),
    )
    mock_requests_post.return_value = MagicMock(
        json=MagicMock(return_value={"status": "dispatched"}),
    )

    result = replenish_idle_pool()

    assert result["mode"] == "fill-floor"
    assert result["created"] == 2
    assert mock_requests_post.call_count == 2


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers.requests.get")
@patch("adapters.helpers.get_target_idle_count")
@patch("adapters.helpers.get_unity_jobs_inventory")
@patch("adapters.helpers._fetch_current_image_hash", return_value="newhash")
def test_replenish_idle_pool_floor_regime_creates_zero_at_target(
    _mock_fetch_hash,
    mock_get_unity_jobs_inventory,
    mock_get_target_idle_count,
    mock_requests_get,
    mock_requests_post,
):
    mock_get_unity_jobs_inventory.return_value = {
        "running": [],
        "idle": [
            _idle_job(f"unity-2026-01-01-00-00-0{i}-match", "newhash") for i in range(3)
        ],
    }
    mock_get_target_idle_count.return_value = SimpleNamespace(
        target=3,
        demand_exceeds_floor=False,
    )
    mock_requests_get.return_value = MagicMock(
        json=MagicMock(return_value={"commit_hash": "newhash"}),
    )

    result = replenish_idle_pool()

    assert result["status"] == "healthy"
    assert result["matching"] == 3
    mock_requests_post.assert_not_called()


@patch("adapters.helpers._delete_idle_jobs")
@patch("adapters.helpers._fetch_current_image_hash", return_value="newhash")
@patch("adapters.helpers.requests.get")
@patch("adapters.helpers.get_target_idle_count")
@patch("adapters.helpers.get_unity_jobs_inventory")
def test_cleanup_idle_pool_deletes_stale_hash_jobs_first(
    mock_get_unity_jobs_inventory,
    mock_get_target_idle_count,
    mock_requests_get,
    _mock_fetch_hash,
    mock_delete_idle_jobs,
):
    mock_get_unity_jobs_inventory.return_value = {"running": [], "idle": []}
    mock_get_target_idle_count.return_value = SimpleNamespace(target=1)
    mock_requests_get.return_value = MagicMock(
        json=MagicMock(
            return_value={
                "jobs": [
                    {
                        "job_name": "unity-2026-01-01-00-00-00-stale",
                        "labels": {
                            "unity-status": "idle",
                            "unity-image-hash": "oldhash",
                        },
                        "resource_version": "1",
                    },
                    {
                        "job_name": "unity-2026-01-01-00-00-00-match",
                        "labels": {
                            "unity-status": "idle",
                            "unity-image-hash": "newhash",
                        },
                        "resource_version": "2",
                    },
                ],
            },
        ),
    )

    result = cleanup_idle_pool()

    assert result["deleted_stale_hash"] == 1
    assert result["deleted_quota"] == 0
    assert result["retained"] == 1
    stale_delete_call = mock_delete_idle_jobs.call_args_list[0].args[0]
    assert stale_delete_call == {
        "unity-2026-01-01-00-00-00-stale": "1",
    }


@patch("adapters.helpers._delete_idle_jobs")
@patch("adapters.helpers._fetch_current_image_hash", return_value="newhash")
@patch("adapters.helpers.requests.get")
@patch("adapters.helpers.get_target_idle_count")
@patch("adapters.helpers.get_unity_jobs_inventory")
def test_cleanup_idle_pool_counts_very_new_toward_target(
    mock_get_unity_jobs_inventory,
    mock_get_target_idle_count,
    mock_requests_get,
    _mock_fetch_hash,
    mock_delete_idle_jobs,
):
    """Very-new idle jobs count toward target; do not stack on top of it."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    very_new_ts = (now - timedelta(seconds=10)).strftime("%Y-%m-%d-%H-%M-%S")
    old_ts = (now - timedelta(minutes=30)).strftime("%Y-%m-%d-%H-%M-%S")

    jobs = []
    for i in range(3):
        jobs.append(
            {
                "job_name": f"unity-{very_new_ts}-new{i}",
                "labels": {
                    "unity-status": "idle",
                    "unity-image-hash": "newhash",
                },
                "resource_version": str(i),
            },
        )
    for i in range(3):
        jobs.append(
            {
                "job_name": f"unity-{old_ts}-old{i}",
                "labels": {
                    "unity-status": "idle",
                    "unity-image-hash": "newhash",
                },
                "resource_version": str(10 + i),
            },
        )

    mock_get_unity_jobs_inventory.return_value = {"running": [], "idle": []}
    mock_get_target_idle_count.return_value = SimpleNamespace(target=3)
    mock_requests_get.return_value = MagicMock(
        json=MagicMock(return_value={"jobs": jobs}),
    )

    result = cleanup_idle_pool()

    assert result["retained"] == 3
    assert result["deleted_quota"] == 3
    assert result["target"] == 3
    # Only one delete call (no stale-hash); should be the 3 old jobs.
    quota_delete = mock_delete_idle_jobs.call_args_list[-1].args[0]
    assert len(quota_delete) == 3
    assert all("old" in name for name in quota_delete)


@patch("adapters.helpers._delete_idle_jobs")
@patch("adapters.helpers._fetch_current_image_hash", return_value=None)
@patch("adapters.helpers.requests.get")
@patch("adapters.helpers.get_target_idle_count")
@patch("adapters.helpers.get_unity_jobs_inventory")
def test_cleanup_idle_pool_skips_stale_pass_without_current_hash(
    mock_get_unity_jobs_inventory,
    mock_get_target_idle_count,
    mock_requests_get,
    _mock_fetch_hash,
    mock_delete_idle_jobs,
):
    mock_get_unity_jobs_inventory.return_value = {"running": [], "idle": []}
    mock_get_target_idle_count.return_value = SimpleNamespace(target=0)
    mock_requests_get.return_value = MagicMock(
        json=MagicMock(
            return_value={
                "jobs": [
                    {
                        "job_name": "unity-2026-01-01-00-00-00-old",
                        "labels": {
                            "unity-status": "idle",
                            "unity-image-hash": "oldhash",
                        },
                        "resource_version": "1",
                    },
                ],
            },
        ),
    )

    result = cleanup_idle_pool()

    assert result["deleted_stale_hash"] == 0
    assert mock_delete_idle_jobs.call_count == 1
    assert mock_delete_idle_jobs.call_args.args[0] == {
        "unity-2026-01-01-00-00-00-old": "1",
    }


@patch("adapters.helpers.requests.post")
@patch.dict("os.environ", {"ORCHESTRA_ADMIN_KEY": "test-key"})
def test_start_unity_job_passes_whatsapp_numbers(mock_post):
    """Both user_whatsapp_number and assistant_whatsapp_number are forwarded."""
    mock_post.return_value = MagicMock(status_code=200)

    assistant_data = _create_mock_assistant_data()
    start_unity_job(assistant_data, "whatsapp")

    data = mock_post.call_args.kwargs.get("data") or mock_post.call_args[1]["data"]
    assert data["user_whatsapp_number"] == "+1234567890"
    assert data["assistant_whatsapp_number"] == "+18501234567"


@patch("adapters.helpers.requests.post")
@patch.dict("os.environ", {"ORCHESTRA_ADMIN_KEY": "test-key"})
def test_start_unity_job_defaults_missing_assistant_whatsapp(mock_post):
    """assistant_whatsapp_number defaults to empty when absent from assistant data."""
    mock_post.return_value = MagicMock(status_code=200)

    assistant_data = _create_mock_assistant_data()
    del assistant_data["assistant_whatsapp_number"]
    start_unity_job(assistant_data, "phone")

    data = mock_post.call_args.kwargs.get("data") or mock_post.call_args[1]["data"]
    assert data["assistant_whatsapp_number"] == ""


@patch("adapters.helpers.logger.info")
@patch("adapters.helpers.requests.post")
@patch.dict("os.environ", {"ORCHESTRA_ADMIN_KEY": "test-key"})
def test_start_unity_job_timeout_is_best_effort_dispatch_only(
    mock_post,
    mock_logger_info,
):
    """Timeouts intentionally preserve webhook latency, not durable acceptance."""

    mock_post.side_effect = requests.exceptions.Timeout

    assistant_data = _create_mock_assistant_data()
    start_unity_job(assistant_data, "phone")

    assert (
        mock_post.call_args.kwargs["timeout"] == START_INTENT_DISPATCH_TIMEOUT_SECONDS
    )
    mock_logger_info.assert_called_once_with(
        "Activation request client timeout after %sms for assistant %s; "
        "adapters intentionally stop waiting here to preserve webhook "
        "latency. This does not confirm comms accepted the request.",
        int(START_INTENT_DISPATCH_TIMEOUT_SECONDS * 1000),
        "12345",
    )


# --- build_webhook_context local assistant tests ---


@patch("adapters.helpers.replenish_idle_pool")
@patch("adapters.helpers._WEBHOOK_BG_POOL.submit")
@patch("adapters.helpers._resolve_contacts", return_value=([], True, None))
def test_build_webhook_context_skips_job_start_for_local_assistant(
    _mock_resolve,
    mock_submit,
    _mock_replenish,
):
    """When is_local=True in assistant data, job start should be skipped."""
    assistant_data = {
        **_create_mock_assistant_data(),
        "is_local": True,
    }
    ctx = build_webhook_context(
        channel="whatsapp",
        destination="+0987654321",
        sender="whatsapp:+1234567890",
        assistant_data=assistant_data,
    )
    mock_submit.assert_not_called()
    assert ctx["is_valid_contact"] is True
    assert ctx["job_started"] is False
    assert ctx["is_job_running"] is False


@patch("adapters.helpers.replenish_idle_pool")
@patch("adapters.helpers._WEBHOOK_BG_POOL.submit")
@patch("adapters.helpers._resolve_contacts", return_value=([], True, None))
def test_build_webhook_context_starts_job_for_non_local_assistant(
    _mock_resolve,
    mock_submit,
    _mock_replenish,
):
    """Legacy flags only mean the async dispatch was scheduled.

    The adapter schedules ``start_unity_job`` on the webhook pool, then returns
    legacy compatibility flags immediately. Comms acceptance remains async.
    """
    assistant_data = _create_mock_assistant_data()
    ctx = build_webhook_context(
        channel="whatsapp",
        destination="+0987654321",
        sender="whatsapp:+1234567890",
        assistant_data=assistant_data,
    )
    mock_submit.assert_called_once()
    submitted = mock_submit.call_args[0][0]
    assert getattr(submitted, "func", submitted) == start_unity_job
    assert ctx["job_started"] is True
    assert ctx["is_job_running"] is True


# Wakeup dedup is now handled atomically by /infra/job/start.
# The previous is_job_running() check-then-act flow was removed as non-atomic.


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


def _stale_job(*, job_name: str, assistant_id: str, status: str = "running") -> dict:
    created_at = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    return {
        "job_name": job_name,
        "assistant_id": assistant_id,
        "labels": {"unity-status": status},
        "creation_timestamp": created_at,
    }


@patch.object(SETTINGS, "comms_url", "http://comms.test")
@patch("adapters.helpers.assistant_has_active_call", return_value=False)
@patch("adapters.helpers.requests.delete")
@patch("adapters.helpers.requests.post")
@patch("adapters.helpers.requests.get")
def test_expire_all_stale_jobs_stops_bound_session_before_deleting_orphans(
    mock_get,
    mock_post,
    mock_delete,
    _mock_active_call,
):
    events = []

    def _get(url, *args, **kwargs):
        if url.endswith("/infra/jobs"):
            params = kwargs.get("params") or {}
            assert "hours" not in params, params
            assert params.get("label_selector") == (
                "app=unity,unity-status in (running,done,idle)"
            )
            return _Response(
                200,
                {
                    "jobs": [
                        _stale_job(job_name="unity-job-bound", assistant_id="aid-1"),
                        _stale_job(job_name="unity-job-orphan", assistant_id="aid-2"),
                    ],
                },
            )
        if url.endswith("/infra/image"):
            return _Response(200, {"commit_hash": "current"})
        if url.endswith("/infra/session/aid-1"):
            return _Response(
                200,
                {
                    "spec": {"desiredState": "Running"},
                    "status": {
                        "phase": "Active",
                        "binding": {"jobRef": {"name": "unity-job-bound"}},
                    },
                },
            )
        if url.endswith("/infra/session/aid-2"):
            return _Response(404, {})
        raise AssertionError(f"unexpected GET {url}")

    def _post(url, *args, **kwargs):
        events.append(("stop", url))
        return _Response(200, {"stopped": True})

    def _delete(url, *args, **kwargs):
        events.append(("delete", kwargs["data"]["job_name"]))
        return _Response(200, {})

    mock_get.side_effect = _get
    mock_post.side_effect = _post
    mock_delete.side_effect = _delete

    result = expire_all_stale_jobs()

    assert result["stopped_assistants"] == ["aid-1"]
    assert result["cleaned_jobs"] == ["unity-job-orphan"]
    assert result["deferred_jobs"] == ["unity-job-bound"]
    assert ("delete", "unity-job-bound") not in events
    assert events == [
        ("stop", "http://comms.test/infra/session/aid-1/stop"),
        ("delete", "unity-job-orphan"),
    ]


@patch.object(SETTINGS, "comms_url", "http://comms.test")
@patch("adapters.helpers.assistant_has_active_call", return_value=False)
@patch("adapters.helpers.requests.delete")
@patch("adapters.helpers.requests.post")
@patch("adapters.helpers.requests.get")
def test_expire_all_stale_jobs_defers_current_binding_already_stopping(
    mock_get,
    mock_post,
    mock_delete,
    _mock_active_call,
):
    def _get(url, *args, **kwargs):
        if url.endswith("/infra/jobs"):
            return _Response(
                200,
                {
                    "jobs": [
                        _stale_job(job_name="unity-job-bound", assistant_id="aid-1"),
                    ],
                },
            )
        if url.endswith("/infra/session/aid-1"):
            return _Response(
                200,
                {
                    "spec": {"desiredState": "Stopped"},
                    "status": {
                        "phase": "Releasing",
                        "binding": {"jobRef": {"name": "unity-job-bound"}},
                    },
                },
            )
        raise AssertionError(f"unexpected GET {url}")

    mock_get.side_effect = _get

    result = expire_all_stale_jobs()

    assert result["stopped_assistants"] == []
    assert result["cleaned_jobs"] == []
    assert result["deferred_jobs"] == ["unity-job-bound"]
    mock_post.assert_not_called()
    mock_delete.assert_not_called()


# --- create_conference_response tests ---


def test_conference_disables_join_beep():
    """Twilio's default conference beep plays an artificial "call answered"
    tone at the callee the moment they pick up (and into the agent's STT);
    every leg renders with beep off."""
    twiml = str(create_conference_response("conf-1"))
    assert 'beep="false"' in twiml

    with_status = str(create_conference_response("conf-1", with_status=True))
    assert 'beep="false"' in with_status


def test_dialed_leg_waits_in_silence():
    """Legs we dial (SIP/agent, or a human who already answered) get no
    conference wait audio; only an inbound caller's own leg hears ringback."""
    twiml = str(create_conference_response("conf-1", ringback=False))
    assert 'waitUrl=""' in twiml
    assert "ring-tone" not in twiml


# --- revoke_ms_teams_bot_install tests ---


def _ms_teams_removed_activity(tenant_id="tenant-1"):
    """A conversationUpdate carrying the bot itself in membersRemoved."""
    return {
        "type": "conversationUpdate",
        "recipient": {"id": "28:bot-app-id"},
        "membersRemoved": [{"id": "28:bot-app-id"}],
        "channelData": {"tenant": {"id": tenant_id}},
    }


@patch("adapters.helpers.requests.delete")
@patch("adapters.helpers.requests.get")
def test_revoke_ms_teams_bot_install_deletes_resolved_install(mock_get, mock_delete):
    """Bot removed from a tenant → resolve the install then DELETE it."""
    mock_get.return_value = MagicMock(status_code=200, json=lambda: {"id": 77})
    mock_delete.return_value = MagicMock(status_code=200)

    revoke_ms_teams_bot_install(_ms_teams_removed_activity(tenant_id="tenant-9"))

    mock_get.assert_called_once()
    get_kwargs = mock_get.call_args.kwargs
    assert get_kwargs["params"] == {"tenant_id": "tenant-9"}
    mock_delete.assert_called_once()
    assert mock_delete.call_args.args[0].endswith("/admin/ms-teams-bot/install/77")


@patch("adapters.helpers.requests.delete")
@patch("adapters.helpers.requests.get")
def test_revoke_ms_teams_bot_install_no_tenant_is_noop(mock_get, mock_delete):
    """No tenant id on the activity → nothing to resolve or revoke."""
    revoke_ms_teams_bot_install({"type": "conversationUpdate", "channelData": {}})

    mock_get.assert_not_called()
    mock_delete.assert_not_called()


@patch("adapters.helpers.requests.delete")
@patch("adapters.helpers.requests.get")
def test_revoke_ms_teams_bot_install_unknown_install_is_noop(mock_get, mock_delete):
    """No live install for the tenant (404) → no DELETE attempted."""
    mock_get.return_value = MagicMock(status_code=404, json=lambda: {})

    revoke_ms_teams_bot_install(_ms_teams_removed_activity())

    mock_get.assert_called_once()
    mock_delete.assert_not_called()


# --- send_ms_teams_bot_install_welcome tests ---


def _ms_teams_added_activity(tenant_id="tenant-1"):
    return {
        "type": "conversationUpdate",
        "recipient": {"id": "28:bot-app-id"},
        "membersAdded": [{"id": "28:bot-app-id"}],
        "conversation": {"id": "conv-1"},
        "serviceUrl": "https://smba.example/",
        "channelData": {"tenant": {"id": tenant_id}},
    }


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers._mint_ms_teams_bot_connector_token", return_value="tok-1")
def test_send_welcome_posts_connect_card(mock_mint, mock_post):
    """Happy path: mint a token, POST an Adaptive Card with the connect URL."""
    mock_post.return_value = MagicMock(status_code=201)

    send_ms_teams_bot_install_welcome(
        _ms_teams_added_activity(),
        {"id": 5, "connect_url": "https://console/connect/ms-teams?nonce=abc"},
    )

    mock_mint.assert_called_once()
    mock_post.assert_called_once()
    url = mock_post.call_args.args[0]
    assert url == "https://smba.example/v3/conversations/conv-1/activities"
    body = mock_post.call_args.kwargs["json"]
    card = body["attachments"][0]["content"]
    # Pending install → the connect button is first and carries the bind link.
    action_url = card["actions"][0]["url"]
    assert action_url == "https://console/connect/ms-teams?nonce=abc"
    # Onboarding pathways are always offered alongside connect (cert #2/#8).
    titles = [a["title"] for a in card["actions"]]
    assert titles == ["Connect to Unify", "Sign up", "Help & docs", "Contact support"]
    # AI-generated-content disclosure is present (cert #21) exactly once — the
    # reviewer flagged the sentence repeating within a single message.
    body_texts = [b.get("text", "") for b in card["body"]]
    assert sum("generated by AI" in text for text in body_texts) == 1


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers._mint_ms_teams_bot_connector_token", return_value="tok-1")
def test_send_welcome_is_card_only(mock_mint, mock_post):
    """Regression for the Store failure "multiple welcome messages on install".

    Teams renders an activity's ``text`` and its card as two stacked blocks, so
    carrying the welcome in both made one send read as two welcomes. The activity
    must carry the card alone, with the plain-text form as the card's
    ``fallbackText`` (which Teams does not render alongside the card).
    """
    mock_post.return_value = MagicMock(status_code=201)

    send_ms_teams_bot_install_welcome(
        _ms_teams_added_activity(),
        {"id": 5, "connect_url": "https://console/x"},
    )

    body = mock_post.call_args.kwargs["json"]
    assert "text" not in body
    assert len(body["attachments"]) == 1
    assert body["attachments"][0]["content"]["fallbackText"]


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers._mint_ms_teams_bot_connector_token", return_value="tok-1")
def test_send_welcome_without_connect_url_omits_connect_copy(mock_mint, mock_post):
    """A bound tenant must not be told to connect — in the copy, not just the
    buttons. The old card kept the "tap Connect to Unify" line even when the
    button was dropped."""
    mock_post.return_value = MagicMock(status_code=201)

    send_ms_teams_bot_install_welcome(_ms_teams_added_activity(), {"id": 5})

    card = mock_post.call_args.kwargs["json"]["attachments"][0]["content"]
    body_texts = " ".join(b.get("text", "") for b in card["body"])
    assert "Connect to Unify" not in body_texts


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers._mint_ms_teams_bot_connector_token")
def test_send_welcome_noop_without_install(mock_mint, mock_post):
    """No install (e.g. transport failure upstream) → nothing to send."""
    send_ms_teams_bot_install_welcome(_ms_teams_added_activity(), None)

    mock_mint.assert_not_called()
    mock_post.assert_not_called()


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers._mint_ms_teams_bot_connector_token", return_value="tok-1")
def test_send_welcome_without_connect_url_omits_connect_button(mock_mint, mock_post):
    """No connect_url (already bound) still sends the welcome card, but the
    ``Connect to Unify`` button is dropped — an already-bound tenant must not be
    told to reconnect. The onboarding buttons and AI disclosure still render."""
    mock_post.return_value = MagicMock(status_code=201)

    send_ms_teams_bot_install_welcome(_ms_teams_added_activity(), {"id": 5})

    mock_mint.assert_called_once()
    mock_post.assert_called_once()
    card = mock_post.call_args.kwargs["json"]["attachments"][0]["content"]
    titles = [a["title"] for a in card["actions"]]
    assert titles == ["Sign up", "Help & docs", "Contact support"]
    assert "Connect to Unify" not in titles


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers._mint_ms_teams_bot_connector_token")
def test_send_welcome_noop_without_conversation(mock_mint, mock_post):
    """Missing conversation/service_url → skip before minting a token."""
    activity = {"type": "conversationUpdate", "channelData": {"tenant": {"id": "t"}}}
    send_ms_teams_bot_install_welcome(
        activity,
        {"id": 5, "connect_url": "https://console/x"},
    )

    mock_mint.assert_not_called()
    mock_post.assert_not_called()


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers._mint_ms_teams_bot_connector_token", return_value=None)
def test_send_welcome_noop_without_token(mock_mint, mock_post):
    """No connector token (unconfigured/mint failed) → no send, no raise."""
    send_ms_teams_bot_install_welcome(
        _ms_teams_added_activity(),
        {"id": 5, "connect_url": "https://console/x"},
    )

    mock_mint.assert_called_once()
    mock_post.assert_not_called()


# --- classify_ms_teams_bot_command tests ---


@pytest.mark.parametrize(
    "text",
    ["hi", "Hi", "  hello  ", "Hey there!", "hello?", "Good morning"],
)
def test_classify_greetings(text):
    assert classify_ms_teams_bot_command(text) == "greeting"


@pytest.mark.parametrize("text", ["help", "Help!", "/help", "HELP", "commands"])
def test_classify_help(text):
    assert classify_ms_teams_bot_command(text) == "help"


@pytest.mark.parametrize(
    "text",
    [
        # Exact-phrase matching keeps real requests away from the canned cards:
        # a message that merely starts with a command word is work, not a command.
        "help me draft this email",
        "hi, can you summarise yesterday's standup?",
        "book me a meeting with Dana",
        "",
    ],
)
def test_classify_other(text):
    assert classify_ms_teams_bot_command(text) == "other"


# --- send_ms_teams_bot_command_reply tests ---


def _ms_teams_message_activity(tenant_id="tenant-1"):
    return {
        "type": "message",
        "text": "hi",
        "recipient": {"id": "28:bot-app-id"},
        "from": {"aadObjectId": "sender-aad", "name": "Reviewer"},
        "conversation": {"id": "conv-1", "conversationType": "personal"},
        "serviceUrl": "https://smba.example/",
        "channelData": {"tenant": {"id": tenant_id}},
    }


def _sent_card(mock_post):
    body = mock_post.call_args.kwargs["json"]
    # Card-only, like the welcome: text + card renders as two stacked blocks.
    assert "text" not in body
    return body["attachments"][0]["content"]


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers._mint_ms_teams_bot_connector_token", return_value="tok-1")
def test_command_reply_greeting_is_not_the_welcome(mock_mint, mock_post):
    """Regression for "welcome message triggers for any bot command": a greeting
    on an unbound install gets a short hello, not the install welcome."""
    mock_post.return_value = MagicMock(status_code=201)

    send_ms_teams_bot_command_reply(
        _ms_teams_message_activity(),
        "greeting",
        "https://console/connect/ms-teams?nonce=xyz",
    )

    mock_mint.assert_called_once()
    url = mock_post.call_args.args[0]
    assert url == "https://smba.example/v3/conversations/conv-1/activities"
    card = _sent_card(mock_post)
    assert card["body"][0]["text"] == "Hi there"
    assert "Welcome to" not in card["body"][0]["text"]
    assert "Thanks for adding" not in card["fallbackText"]
    # Still a way forward: connect link plus the pointer to help.
    assert card["actions"][0]["url"] == "https://console/connect/ms-teams?nonce=xyz"
    assert "Help" in card["fallbackText"]


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers._mint_ms_teams_bot_connector_token", return_value="tok-1")
def test_command_reply_help_lists_supported_commands(mock_mint, mock_post):
    """Help must be self-explanatory about the app's value and its commands."""
    mock_post.return_value = MagicMock(status_code=201)

    send_ms_teams_bot_command_reply(_ms_teams_message_activity(), "help", None)

    card = _sent_card(mock_post)
    texts = " ".join(b.get("text", "") for b in card["body"])
    assert "Unify T-W1N" in texts
    assert "**Help**" in texts
    assert "**Hi** or **Hello**" in texts
    assert "**Anything else**" in texts
    # Bound workspace → no connect prompt anywhere.
    assert "Connect to Unify" not in texts
    assert [a["title"] for a in card["actions"]] == [
        "Sign up",
        "Help & docs",
        "Contact support",
    ]


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers._mint_ms_teams_bot_connector_token", return_value="tok-1")
def test_command_reply_unknown_offers_help(mock_mint, mock_post):
    """Invalid input gets a distinct "didn't understand" reply that points at
    help, rather than the same card a valid command returns."""
    mock_post.return_value = MagicMock(status_code=201)

    send_ms_teams_bot_command_reply(_ms_teams_message_activity(), "other", None)

    card = _sent_card(mock_post)
    texts = " ".join(b.get("text", "") for b in card["body"])
    assert "didn't understand" in texts
    assert "Type **Help**" in texts


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers._mint_ms_teams_bot_connector_token", return_value="tok-1")
def test_command_replies_are_all_distinct(mock_mint, mock_post):
    """The three command classes must not share a response (cert: "providing
    same response to both valid and invalid command")."""
    mock_post.return_value = MagicMock(status_code=201)

    rendered = []
    for command in ("greeting", "help", "other"):
        mock_post.reset_mock()
        send_ms_teams_bot_command_reply(
            _ms_teams_message_activity(),
            command,
            "https://console/x",
        )
        rendered.append(_sent_card(mock_post)["fallbackText"])

    assert len(set(rendered)) == 3


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers._mint_ms_teams_bot_connector_token")
def test_command_reply_noop_without_conversation(mock_mint, mock_post):
    """Missing conversation/service_url → skip before minting a token."""
    activity = {"type": "message", "channelData": {"tenant": {"id": "t"}}}
    send_ms_teams_bot_command_reply(activity, "greeting", "https://console/x")

    mock_mint.assert_not_called()
    mock_post.assert_not_called()


# --- _build_start_job_request_data ownership contract ---


def _start_job_assistant(**overrides) -> dict:
    assistant = {
        "api_key": "test_api_key",
        "assistant_id": "1406",
        "user_id": "user_1",
        "user_first_name": "Boss",
        "user_surname": "User",
        "user_email": "boss@example.com",
        "assistant_first_name": "Brain",
        "assistant_surname": "Operator",
        "assistant_age": "30",
        "assistant_nationality": "British",
        "assistant_about": "Ops assistant",
        "assistant_timezone": "UTC",
        "user_number": "+15555550001",
        "assistant_number": "+15555550000",
        "assistant_email": "brain@example.com",
        "user_whatsapp_number": "",
        "self_contact_id": TEST_SELF_CONTACT_ID,
        "boss_contact_id": TEST_BOSS_CONTACT_ID,
        "team_ids": [11],
        "owner_team_id": 11,
    }
    assistant.update(overrides)
    return assistant


def test_start_job_form_delivers_team_ownership_through_the_decoder():
    """Ownership must survive the adapters form -> /infra/job/start decode hop.

    Dropping the field defaults the Form to "" -> None, the bootstrap Secret
    delivers null, and a team-owned assistant's live session routes
    shared-scoped storage to the personal root.
    """
    data = _build_start_job_request_data(_start_job_assistant(), "api_message")

    assert data["owner_team_id"] == "11"
    assert _decode_owner_team_id_form(data["owner_team_id"]) == 11


def test_start_job_form_keeps_a_user_owned_assistant_personal():
    data = _build_start_job_request_data(
        _start_job_assistant(owner_team_id=None, team_ids=[]),
        "email",
    )

    assert data["owner_team_id"] == ""
    assert _decode_owner_team_id_form(data["owner_team_id"]) is None


def _stale_idle_job(*, job_name: str, image_hash: str) -> dict:
    """An idle pool member old enough for the sweep to consider it."""
    job = _stale_job(job_name=job_name, assistant_id="", status="idle")
    job["labels"]["unity-image-hash"] = image_hash
    return job


@patch.object(SETTINGS, "comms_url", "http://comms.test")
@patch("adapters.helpers.assistant_has_active_call", return_value=False)
@patch("adapters.helpers.requests.delete")
@patch("adapters.helpers.requests.post")
@patch("adapters.helpers.requests.get")
@patch("adapters.helpers._fetch_current_image_hash", return_value="current")
def test_expire_all_stale_jobs_reaps_only_unclaimable_idle_members(
    _mock_fetch_hash,
    mock_get,
    mock_post,
    mock_delete,
    _mock_active_call,
):
    """An idle pod on a superseded image is dead weight nothing else collects.

    The controller only claims idle Jobs whose image hash is current, so a
    stale-hash member will never be assigned; and an unassigned pod is exempt
    from the in-pod idle timer by design, because its lifetime belongs to the
    pool. Between the two, nothing reaped them: two ran in staging for
    nineteen days. A current-hash member is warm capacity however old it is,
    and deleting it would only make the pool build a replacement.
    """
    deleted = []

    def _get(url, *args, **kwargs):
        if url.endswith("/infra/jobs"):
            return _Response(
                200,
                {
                    "jobs": [
                        _stale_idle_job(job_name="unity-idle-stale", image_hash="old"),
                        _stale_idle_job(
                            job_name="unity-idle-current",
                            image_hash="current",
                        ),
                    ],
                },
            )
        if url.endswith("/infra/image"):
            return _Response(200, {"commit_hash": "current"})
        raise AssertionError(f"unexpected GET {url}")

    mock_get.side_effect = _get
    mock_post.side_effect = lambda url, *a, **k: _Response(200, {})
    mock_delete.side_effect = lambda url, *a, **k: (
        deleted.append(k["data"]["job_name"]),
        _Response(200, {}),
    )[1]

    result = expire_all_stale_jobs()

    assert deleted == ["unity-idle-stale"]
    assert result["cleaned_jobs"] == ["unity-idle-stale"]


@patch.object(SETTINGS, "comms_url", "http://comms.test")
@patch("adapters.helpers.assistant_has_active_call", return_value=False)
@patch("adapters.helpers.requests.delete")
@patch("adapters.helpers.requests.post")
@patch("adapters.helpers.requests.get")
@patch("adapters.helpers._fetch_current_image_hash", return_value=None)
def test_an_unreadable_image_hash_leaves_every_idle_member_alone(
    _mock_fetch_hash,
    mock_get,
    mock_post,
    mock_delete,
    _mock_active_call,
):
    """No hash means no evidence, and "matches nothing" would empty the pool."""
    deleted = []

    def _get(url, *args, **kwargs):
        if url.endswith("/infra/jobs"):
            return _Response(
                200,
                {
                    "jobs": [
                        _stale_idle_job(job_name="unity-idle-stale", image_hash="old"),
                    ],
                },
            )
        raise AssertionError(f"unexpected GET {url}")

    mock_get.side_effect = _get
    mock_post.side_effect = lambda url, *a, **k: _Response(200, {})
    mock_delete.side_effect = lambda url, *a, **k: (
        deleted.append(k["data"]["job_name"]),
        _Response(200, {}),
    )[1]

    expire_all_stale_jobs()

    assert deleted == []
