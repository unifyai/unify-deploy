"""
Unit tests for the adapters helper functions.

These tests verify:
- Contact handling logic (WhatsApp is validated via Orchestra's resolve endpoint,
  not through check_contact_details)
- Demo ID propagation for demo assistants (passed as string to Comms)
"""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from adapters.helpers import (
    build_webhook_context,
    cleanup_idle_pool,
    expire_all_stale_jobs,
    get_default_contacts,
    get_unity_jobs_inventory,
    check_contact_details,
    replenish_idle_pool,
    start_unity_job,
)
from common.settings import SETTINGS

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
    }
    contacts = get_default_contacts(assistant_data)

    assert len(contacts) == 2
    assert contacts[0]["phone_number"] == "+1234567890"  # assistant
    assert contacts[1]["phone_number"] == "+0987654321"  # user


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


# --- start_unity_job demo mode tests ---


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
        "user_desktop_mode": None,
        "user_desktop_filesys_sync": False,
        "user_desktop_url": None,
        "demo_id": demo_id,
        "is_local": False,
    }


@patch("adapters.helpers.requests.post")
@patch.dict("os.environ", {"ORCHESTRA_ADMIN_KEY": "test-key"})
def test_start_unity_job_passes_demo_id_for_demo_assistant(mock_post):
    """Verify demo_id is passed as string when assistant has demo_id."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response

    assistant_data = _create_mock_assistant_data(demo_id=42)

    start_unity_job(assistant_data, "phone")

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
def test_start_unity_job_passes_empty_demo_id_for_regular_assistant(mock_post):
    """Verify demo_id is empty string when assistant has no demo_id."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response

    assistant_data = _create_mock_assistant_data(demo_id=None)

    start_unity_job(assistant_data, "phone")

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
def test_start_unity_job_passes_empty_demo_id_when_key_missing(mock_post):
    """Verify demo_id is empty string when demo_id key is missing from assistant data."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response

    # Create assistant data without demo_id key
    assistant_data = _create_mock_assistant_data(demo_id=None)
    del assistant_data["demo_id"]

    start_unity_job(assistant_data, "phone")

    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data")

    # Verify demo_id is empty string
    assert (
        data["demo_id"] == ""
    ), f"Expected demo_id='' when key missing, got '{data.get('demo_id')}'"


@patch("adapters.helpers.requests.post")
@patch.dict("os.environ", {"ORCHESTRA_ADMIN_KEY": "test-key"})
def test_start_unity_job_demo_id_with_different_mediums(mock_post):
    """Verify demo_id is passed correctly for different communication mediums."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response

    assistant_data = _create_mock_assistant_data(demo_id=99)

    for medium in ["phone", "email", "whatsapp", "msg"]:
        mock_post.reset_mock()
        start_unity_job(assistant_data, medium)

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data")

        assert data["demo_id"] == "99", f"Expected demo_id='99' for medium={medium}"
        assert data["medium"] == medium, f"Expected medium={medium}"


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
@patch("adapters.helpers.get_unity_jobs_inventory")
def test_cleanup_idle_pool_uses_explicit_lookback_for_idle_listing(
    mock_get_unity_jobs_inventory,
    mock_get_target_idle_count,
    mock_requests_get,
):
    mock_get_unity_jobs_inventory.return_value = {"running": [], "idle": []}
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


# --- build_webhook_context local assistant tests ---


@patch("adapters.helpers.replenish_idle_pool")
@patch("adapters.helpers.start_unity_job")
@patch("adapters.helpers._resolve_contacts", return_value=([], True))
def test_build_webhook_context_skips_job_start_for_local_assistant(
    _mock_resolve,
    mock_start,
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
    mock_start.assert_not_called()
    assert ctx["is_valid_contact"] is True


@patch("adapters.helpers.replenish_idle_pool")
@patch("adapters.helpers.start_unity_job")
@patch("adapters.helpers._resolve_contacts", return_value=([], True))
def test_build_webhook_context_starts_job_for_non_local_assistant(
    _mock_resolve,
    mock_start,
    _mock_replenish,
):
    """When is_local=False, job start should proceed normally.

    The adapter unconditionally calls start_unity_job (which hits
    /infra/job/start). Deduplication is handled atomically by the
    comms app via K8s Leases, not by the adapter.
    """
    assistant_data = _create_mock_assistant_data()
    ctx = build_webhook_context(
        channel="whatsapp",
        destination="+0987654321",
        sender="whatsapp:+1234567890",
        assistant_data=assistant_data,
    )
    mock_start.assert_called_once()


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
                        _stale_job(job_name="unity-job-bound", assistant_id="aid-1"),
                        _stale_job(job_name="unity-job-orphan", assistant_id="aid-2"),
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
