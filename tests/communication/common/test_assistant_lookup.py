from common.assistant_lookup import _assistant_payload


def test_assistant_payload_coerces_nullable_runtime_strings():
    assistant = {
        "agent_id": 110,
        "deploy_env": None,
        "user_id": "user-123",
        "api_key": "key",
        "user_first_name": None,
        "user_last_name": None,
        "first_name": "T-W1N",
        "surname": None,
        "age": None,
        "nationality": None,
        "about": None,
        "job_title": None,
        "timezone": None,
        "phone": None,
        "assistant_whatsapp_number": None,
        "assistant_discord_bot_id": None,
        "email": None,
        "email_provider": None,
        "user_phone": None,
        "user_whatsapp_number": "+4915550100009",
        "user_email": "dan@unify.ai",
        "voice_provider": None,
        "voice_id": None,
        "secrets": {},
        "desktop_mode": None,
        "user_desktops": [],
        "demo_id": None,
        "is_local": False,
        "team_ids": [],
        "team_summaries": [],
        "self_contact_id": None,
        "boss_contact_id": None,
        "is_coordinator": True,
        "organization_id": None,
    }

    payload = _assistant_payload(assistant)

    assert payload["assistant_id"] == "110"
    assert payload["user_first_name"] == ""
    assert payload["user_surname"] == ""
    assert payload["assistant_surname"] == ""
    assert payload["assistant_age"] == ""
    assert payload["assistant_nationality"] == ""
    assert payload["assistant_about"] == ""
    assert payload["assistant_job_title"] == ""
    assert payload["assistant_timezone"] == "UTC"
    assert payload["assistant_number"] == ""
    assert payload["assistant_whatsapp_number"] == ""
    assert payload["assistant_discord_bot_id"] == ""
    assert payload["assistant_email"] == ""
    assert payload["assistant_email_provider"] == "google_workspace"
    assert payload["user_number"] == ""
    assert payload["user_whatsapp_number"] == "+4915550100009"
    assert payload["voice_provider"] == ""
    assert payload["voice_id"] == ""
    assert payload["self_contact_id"] == 0
    assert payload["boss_contact_id"] == 1
