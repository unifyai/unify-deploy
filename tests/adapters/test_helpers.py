"""
Unit tests for the adapters helper functions.

These tests verify contact handling logic after the whatsapp_number field
was removed from the contact schema.
"""

from adapters.helpers import (
    get_default_contacts,
    check_contact_details,
)

# --- get_default_contacts tests ---


def test_get_default_contacts_does_not_include_whatsapp_number():
    """Verify contacts returned don't include whatsapp_number field."""
    assistant_data = {
        "assistant_first_name": "Test",
        "assistant_surname": "Assistant",
        "assistant_email": "test@example.com",
        "assistant_number": "+1234567890",
        "user_name": "Test User",
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
        "user_name": "Test User",
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
