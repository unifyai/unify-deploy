"""Billing-gate auto-reply helpers: owner matching and fail-open lookup."""

from unittest.mock import patch

from adapters.helpers import check_comms_gate, is_owner_sender


class TestIsOwnerSender:
    def test_sms_owner_matches_normalized(self):
        data = {"user_number": "+1 (415) 555-0100", "user_whatsapp_number": ""}
        assert is_owner_sender(data, "msg", "+14155550100")

    def test_whatsapp_prefix_stripped(self):
        data = {"user_number": "", "user_whatsapp_number": "+447700900123"}
        assert is_owner_sender(data, "whatsapp", "whatsapp:+447700900123")

    def test_third_party_contact_is_not_owner(self):
        data = {"user_number": "+14155550100", "user_whatsapp_number": ""}
        assert not is_owner_sender(data, "msg", "+15005550006")

    def test_email_owner_case_insensitive(self):
        data = {"user_email": "Owner@Example.com"}
        assert is_owner_sender(data, "email", "owner@example.com ")

    def test_email_non_owner(self):
        data = {"user_email": "owner@example.com"}
        assert not is_owner_sender(data, "email", "stranger@example.com")

    def test_missing_owner_identifiers_never_match(self):
        assert not is_owner_sender({}, "msg", "+14155550100")
        assert not is_owner_sender({}, "email", "owner@example.com")


class TestCheckCommsGate:
    def test_gated_response_passed_through(self):
        payload = {"gated": True, "reason": "card_required", "message": "Add a card."}
        with patch("adapters.helpers.requests.get") as mock_get:
            mock_get.return_value.json.return_value = payload
            assert check_comms_gate(123) == payload

    def test_not_gated_returns_none(self):
        with patch("adapters.helpers.requests.get") as mock_get:
            mock_get.return_value.json.return_value = {"gated": False}
            assert check_comms_gate(123) is None

    def test_lookup_error_fails_open(self):
        with patch(
            "adapters.helpers.requests.get",
            side_effect=ConnectionError("orchestra down"),
        ):
            assert check_comms_gate(123) is None

    def test_missing_assistant_id_fails_open(self):
        assert check_comms_gate(None) is None
        assert check_comms_gate("") is None
