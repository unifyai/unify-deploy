"""
Unit tests for the adapters helper functions.

These tests verify:
- Contact handling logic (WhatsApp is validated via Orchestra's resolve endpoint,
  not through check_contact_details)
- Demo ID propagation for demo assistants (passed as string to Comms)
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
    expire_all_stale_jobs,
    get_default_contacts,
    get_assistant,
    get_droid_jobs_inventory,
    check_contact_details,
    dispatch_droid_start_intent,
    replenish_idle_pool,
    start_droid_job,
)
from common.settings import SETTINGS

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


# --- start_droid_job demo mode tests ---


def _create_mock_assistant_data(demo_id=None, desktop_mode="none"):
    """Create mock assistant data for testing.

    Args:
        demo_id: Optional demo ID (None for regular assistants)
        desktop_mode: Desktop mode - use "none" to skip VM start call in tests
    """
    return {
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
        "demo_id": demo_id,
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
        "demo_id": None,
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
    assert assistant_data["desktop_mode"] == "ubuntu"


@patch("adapters.helpers.requests.post")
@patch.dict("os.environ", {"ORCHESTRA_ADMIN_KEY": "test-key"})
def test_start_droid_job_passes_demo_id_for_demo_assistant(mock_post):
    """Verify demo_id is passed as string when assistant has demo_id."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response

    assistant_data = _create_mock_assistant_data(demo_id=42)

    start_droid_job(assistant_data, "phone")

    # Check that requests.post was called
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args

    # Get the data parameter
    data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data")

    # Verify demo_id is passed as string "42"
    assert (
        data["demo_id"] == "42"
    ), f"Expected demo_id='42' for demo assistant, got '{data.get('demo_id')}'"


@patch("adapters.helpers.requests.post")
@patch.dict("os.environ", {"ORCHESTRA_ADMIN_KEY": "test-key"})
def test_start_droid_job_passes_empty_demo_id_for_regular_assistant(mock_post):
    """Verify demo_id is empty string when assistant has no demo_id."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response

    assistant_data = _create_mock_assistant_data(demo_id=None)

    start_droid_job(assistant_data, "phone")

    # Check that requests.post was called
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args

    # Get the data parameter
    data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data")

    # Verify demo_id is empty string
    assert (
        data["demo_id"] == ""
    ), f"Expected demo_id='' for regular assistant, got '{data.get('demo_id')}'"


@patch("adapters.helpers.requests.post")
@patch.dict("os.environ", {"ORCHESTRA_ADMIN_KEY": "test-key"})
def test_start_droid_job_passes_empty_demo_id_when_key_missing(mock_post):
    """Verify demo_id is empty string when demo_id key is missing from assistant data."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response

    # Create assistant data without demo_id key
    assistant_data = _create_mock_assistant_data(demo_id=None)
    del assistant_data["demo_id"]

    start_droid_job(assistant_data, "phone")

    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data")

    # Verify demo_id is empty string
    assert (
        data["demo_id"] == ""
    ), f"Expected demo_id='' when key missing, got '{data.get('demo_id')}'"


@patch("adapters.helpers.requests.post")
@patch.dict("os.environ", {"ORCHESTRA_ADMIN_KEY": "test-key"})
def test_start_droid_job_demo_id_with_different_mediums(mock_post):
    """Verify demo_id is passed correctly for different communication mediums."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response

    assistant_data = _create_mock_assistant_data(demo_id=99)

    for medium in ["phone", "email", "whatsapp", "msg"]:
        mock_post.reset_mock()
        start_droid_job(assistant_data, medium)

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data")

        assert data["demo_id"] == "99", f"Expected demo_id='99' for medium={medium}"
        assert data["medium"] == medium, f"Expected medium={medium}"


@patch("adapters.helpers.requests.post")
@patch.dict("os.environ", {"ORCHESTRA_ADMIN_KEY": "test-key"})
def test_dispatch_droid_start_intent_includes_wake_reasons(mock_post):
    """Wake reasons should be serialized onto the start-intent form payload."""

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response
    assistant_data = _create_mock_assistant_data(demo_id=7)
    assistant_data["desktop_mode"] = None
    assistant_data["is_coordinator"] = True
    wake_reasons = [{"type": "task_due", "task_id": 101}]

    response = dispatch_droid_start_intent(
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
    assert call_kwargs["data"]["desktop_mode"] == "ubuntu"


@patch("adapters.helpers.requests.post")
@patch.dict("os.environ", {"ORCHESTRA_ADMIN_KEY": "test-key"})
def test_dispatch_droid_start_intent_encodes_team_ids_for_form(mock_post):
    """Start-intent form payloads carry memberships as JSON strings."""

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response
    assistant_data = _create_mock_assistant_data()

    response = dispatch_droid_start_intent(assistant_data, "api_message")

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
def test_dispatch_droid_start_intent_returns_none_without_api_key(mock_post):
    """Assistants without API keys should not dispatch start intent requests."""

    assistant_data = _create_mock_assistant_data()
    assistant_data["api_key"] = ""

    response = dispatch_droid_start_intent(assistant_data, "api_message")

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
                        "demo_id": None,
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
def test_get_droid_jobs_inventory_uses_explicit_lookback(mock_fetch_infra_jobs):
    mock_response = MagicMock()
    mock_response.json.return_value = {"jobs": []}
    mock_fetch_infra_jobs.return_value = mock_response

    inventory = get_droid_jobs_inventory()

    assert inventory == {"running": [], "idle": []}
    mock_fetch_infra_jobs.assert_called_once()
    params = mock_fetch_infra_jobs.call_args.args[0]
    assert params["hours"] == SETTINGS.job_inventory_lookback_hours
    assert params["label_selector"] == "app=droid,droid-status!=done"


@patch("adapters.helpers.requests.post")
@patch("adapters.helpers.requests.get")
@patch("adapters.helpers.get_target_idle_count")
@patch("adapters.helpers.get_droid_jobs_inventory")
def test_replenish_idle_pool_honors_extra_demand(
    mock_get_droid_jobs_inventory,
    mock_get_target_idle_count,
    mock_requests_get,
    mock_requests_post,
):
    mock_get_droid_jobs_inventory.return_value = {
        "running": [{"job_name": "running-1"}],
        "idle": [{"job_name": "idle-1"}],
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


@patch("adapters.helpers.requests.get")
@patch("adapters.helpers.get_target_idle_count")
@patch("adapters.helpers.get_droid_jobs_inventory")
def test_cleanup_idle_pool_uses_explicit_lookback_for_idle_listing(
    mock_get_droid_jobs_inventory,
    mock_get_target_idle_count,
    mock_requests_get,
):
    mock_get_droid_jobs_inventory.return_value = {"running": [], "idle": []}
    mock_get_target_idle_count.return_value = SimpleNamespace(target=0)
    mock_response = MagicMock()
    mock_response.json.return_value = {"jobs": []}
    mock_requests_get.return_value = mock_response

    result = cleanup_idle_pool()

    assert result["deleted"] == 0
    assert result["running"] == 0
    mock_requests_get.assert_called_once()
    assert (
        mock_requests_get.call_args.kwargs["params"]["hours"]
        == SETTINGS.job_inventory_lookback_hours
    )


@patch("adapters.helpers.requests.post")
@patch.dict("os.environ", {"ORCHESTRA_ADMIN_KEY": "test-key"})
def test_start_droid_job_passes_whatsapp_numbers(mock_post):
    """Both user_whatsapp_number and assistant_whatsapp_number are forwarded."""
    mock_post.return_value = MagicMock(status_code=200)

    assistant_data = _create_mock_assistant_data()
    start_droid_job(assistant_data, "whatsapp")

    data = mock_post.call_args.kwargs.get("data") or mock_post.call_args[1]["data"]
    assert data["user_whatsapp_number"] == "+1234567890"
    assert data["assistant_whatsapp_number"] == "+18501234567"


@patch("adapters.helpers.requests.post")
@patch.dict("os.environ", {"ORCHESTRA_ADMIN_KEY": "test-key"})
def test_start_droid_job_defaults_missing_assistant_whatsapp(mock_post):
    """assistant_whatsapp_number defaults to empty when absent from assistant data."""
    mock_post.return_value = MagicMock(status_code=200)

    assistant_data = _create_mock_assistant_data()
    del assistant_data["assistant_whatsapp_number"]
    start_droid_job(assistant_data, "phone")

    data = mock_post.call_args.kwargs.get("data") or mock_post.call_args[1]["data"]
    assert data["assistant_whatsapp_number"] == ""


@patch("adapters.helpers.logger.info")
@patch("adapters.helpers.requests.post")
@patch.dict("os.environ", {"ORCHESTRA_ADMIN_KEY": "test-key"})
def test_start_droid_job_timeout_is_best_effort_dispatch_only(
    mock_post,
    mock_logger_info,
):
    """Timeouts intentionally preserve webhook latency, not durable acceptance."""

    mock_post.side_effect = requests.exceptions.Timeout

    assistant_data = _create_mock_assistant_data()
    start_droid_job(assistant_data, "phone")

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

    The adapter schedules ``start_droid_job`` on the webhook pool, then returns
    legacy compatibility flags immediately. Comms acceptance remains async.
    """
    assistant_data = _create_mock_assistant_data()
    ctx = build_webhook_context(
        channel="whatsapp",
        destination="+0987654321",
        sender="whatsapp:+1234567890",
        assistant_data=assistant_data,
    )
    mock_submit.assert_called_once_with(start_droid_job, assistant_data, "whatsapp")
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
        "labels": {"droid-status": status},
        "creation_timestamp": created_at,
    }


@patch.object(SETTINGS, "comms_url", "http://comms.test")
@patch("adapters.helpers.requests.delete")
@patch("adapters.helpers.requests.post")
@patch("adapters.helpers.requests.get")
def test_expire_all_stale_jobs_stops_bound_session_before_deleting_orphans(
    mock_get,
    mock_post,
    mock_delete,
):
    events = []

    def _get(url, *args, **kwargs):
        if url.endswith("/infra/jobs"):
            return _Response(
                200,
                {
                    "jobs": [
                        _stale_job(job_name="droid-job-bound", assistant_id="aid-1"),
                        _stale_job(job_name="droid-job-orphan", assistant_id="aid-2"),
                    ],
                },
            )
        if url.endswith("/infra/session/aid-1"):
            return _Response(
                200,
                {
                    "spec": {"desiredState": "Running"},
                    "status": {
                        "phase": "Active",
                        "binding": {"jobRef": {"name": "droid-job-bound"}},
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
    assert result["cleaned_jobs"] == ["droid-job-orphan"]
    assert result["deferred_jobs"] == ["droid-job-bound"]
    assert ("delete", "droid-job-bound") not in events
    assert events == [
        ("stop", "http://comms.test/infra/session/aid-1/stop"),
        ("delete", "droid-job-orphan"),
    ]


@patch.object(SETTINGS, "comms_url", "http://comms.test")
@patch("adapters.helpers.requests.delete")
@patch("adapters.helpers.requests.post")
@patch("adapters.helpers.requests.get")
def test_expire_all_stale_jobs_defers_current_binding_already_stopping(
    mock_get,
    mock_post,
    mock_delete,
):
    def _get(url, *args, **kwargs):
        if url.endswith("/infra/jobs"):
            return _Response(
                200,
                {
                    "jobs": [
                        _stale_job(job_name="droid-job-bound", assistant_id="aid-1"),
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
                        "binding": {"jobRef": {"name": "droid-job-bound"}},
                    },
                },
            )
        raise AssertionError(f"unexpected GET {url}")

    mock_get.side_effect = _get

    result = expire_all_stale_jobs()

    assert result["stopped_assistants"] == []
    assert result["cleaned_jobs"] == []
    assert result["deferred_jobs"] == ["droid-job-bound"]
    mock_post.assert_not_called()
    mock_delete.assert_not_called()
