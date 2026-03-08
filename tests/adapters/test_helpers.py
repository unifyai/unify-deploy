"""
Unit tests for the adapters helper functions.

These tests verify:
- Contact handling logic after the whatsapp_number field was removed
- Demo ID propagation for demo assistants (passed as string to Comms)
"""

from unittest.mock import patch, MagicMock
from adapters.helpers import (
    build_webhook_context,
    get_default_contacts,
    check_contact_details,
    start_unity_job,
)

# --- get_default_contacts tests ---


def test_get_default_contacts_does_not_include_whatsapp_number():
    """Verify contacts returned don't include whatsapp_number field."""
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

    for contact in contacts:
        assert (
            "whatsapp_number" not in contact
        ), f"Contact should not have whatsapp_number field: {contact}"


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


def test_check_contact_details_whatsapp_uses_user_whatsapp_number():
    """Verify WhatsApp matching uses user_whatsapp_number parameter."""
    result = check_contact_details(
        phone_number="+1111111111",
        medium="whatsapp",
        user_number="+2222222222",  # Different from phone_number
        user_whatsapp_number="+1111111111",  # Matches phone_number
    )
    assert result is True


def test_check_contact_details_whatsapp_does_not_match_user_number():
    """Verify WhatsApp doesn't match against user_number."""
    result = check_contact_details(
        phone_number="+1111111111",
        medium="whatsapp",
        user_number="+1111111111",  # Matches phone_number but shouldn't be used
        user_whatsapp_number="+2222222222",  # Different
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


# --- build_webhook_context local assistant tests ---


@patch("adapters.helpers.create_job")
@patch("adapters.helpers.start_unity_job")
@patch("adapters.helpers.mark_job_running")
@patch("adapters.helpers.is_job_running", return_value=False)
@patch("adapters.helpers.check_valid_contact", return_value=([], True))
def test_build_webhook_context_skips_job_start_for_local_assistant(
    _mock_check,
    _mock_running,
    mock_mark,
    mock_start,
    mock_create,
):
    """When is_local=True in assistant data, job start should be skipped
    even though the job is not running and the contact is valid."""
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
    mock_mark.assert_not_called()
    mock_start.assert_not_called()
    mock_create.assert_not_called()
    assert ctx["is_valid_contact"] is True


@patch("adapters.helpers.create_job")
@patch("adapters.helpers.start_unity_job")
@patch("adapters.helpers.mark_job_running")
@patch("adapters.helpers.is_job_running", return_value=False)
@patch("adapters.helpers.check_valid_contact", return_value=([], True))
def test_build_webhook_context_starts_job_for_non_local_assistant(
    _mock_check,
    _mock_running,
    mock_mark,
    mock_start,
    mock_create,
):
    """When is_local=False, job start should proceed normally."""
    assistant_data = _create_mock_assistant_data()
    ctx = build_webhook_context(
        channel="whatsapp",
        destination="+0987654321",
        sender="whatsapp:+1234567890",
        assistant_data=assistant_data,
    )
    mock_mark.assert_called_once()
    mock_start.assert_called_once()
    mock_create.assert_called_once()
