"""Tests for ``common.contacts.build_boss_contact``."""

from __future__ import annotations

from common.contacts import build_boss_contact


class TestBuildBossContact:
    def test_full_assistant_record(self):
        assistant = {
            "user_first_name": "Julia",
            "user_surname": "Boss",
            "user_email": "julia@contoso.com",
            "user_number": "+14155550001",
            "user_whatsapp_number": "+14155550001",
            "user_discord_id": "julia#1234",
        }
        result = build_boss_contact(assistant)
        assert result == {
            "contact_id": 1,
            "first_name": "Julia",
            "surname": "Boss",
            "email_address": "julia@contoso.com",
            "phone_number": "+14155550001",
            "whatsapp_number": "+14155550001",
            "discord_id": "julia#1234",
            "bio": "",
            "rolling_summary": "",
            "should_respond": True,
            "response_policy": "",
        }

    def test_missing_optional_fields_default_to_empty_string(self):
        assistant = {
            "user_first_name": "Julia",
            "user_email": "julia@contoso.com",
        }
        result = build_boss_contact(assistant)
        assert result["contact_id"] == 1
        assert result["first_name"] == "Julia"
        assert result["surname"] == ""
        assert result["phone_number"] == ""
        assert result["whatsapp_number"] == ""
        assert result["discord_id"] == ""

    def test_none_values_coerced_to_empty_string(self):
        # ``user_number=None`` should not serialise as ``None`` (breaks
        # downstream JSON consumers that expect string fields).
        assistant = {
            "user_first_name": None,
            "user_surname": None,
            "user_email": None,
            "user_number": None,
        }
        result = build_boss_contact(assistant)
        assert result["first_name"] == ""
        assert result["surname"] == ""
        assert result["email_address"] == ""
        assert result["phone_number"] == ""

    def test_contact_id_is_always_1(self):
        # contact_id=1 is the boss sentinel Unity expects; must not
        # collide with contact_id=0 (the assistant themselves) or any
        # matched-contact id from Orchestra.
        assistant = {"user_first_name": "X", "user_email": "x@y"}
        assert build_boss_contact(assistant)["contact_id"] == 1

    def test_should_respond_is_always_true(self):
        # The boss is the party whose utterances the assistant replies
        # to; ``should_respond=True`` wires that policy in every event.
        assistant = {"user_first_name": "X"}
        assert build_boss_contact(assistant)["should_respond"] is True
