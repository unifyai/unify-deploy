import logging

import requests

from common.assistant_lookup import (
    ADMIN_CONTACT_LOOKUP_FROM_FIELDS,
    _assistant_payload,
    assistant_may_start_runtime,
    get_assistant,
)


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


def test_assistant_payload_carries_default_model_fields():
    assistant = {
        "agent_id": 7,
        "user_id": "user-7",
        "api_key": "key",
        "user_first_name": "Ada",
        "user_last_name": "Lovelace",
        "first_name": "Model",
        "surname": "Tester",
        "age": 30,
        "nationality": "GB",
        "about": "x",
        "job_title": None,
        "timezone": "UTC",
        "phone": None,
        "email": None,
        "user_phone": None,
        "user_email": "ada@example.com",
        "voice_provider": "cartesia",
        "voice_id": "voice-1",
        "default_model": "claude-fable-5@anthropic",
        "default_reasoning_effort": "high",
        "slow_brain_model": "gpt-5.6-terra@openai",
        "slow_brain_reasoning_effort": "high",
        "self_contact_id": 1,
        "boss_contact_id": 2,
    }

    payload = _assistant_payload(assistant)

    assert payload["default_model"] == "claude-fable-5@anthropic"
    assert payload["default_reasoning_effort"] == "high"
    assert payload["slow_brain_model"] == "gpt-5.6-terra@openai"
    assert payload["slow_brain_reasoning_effort"] == "high"
    assert "default_model" in ADMIN_CONTACT_LOOKUP_FROM_FIELDS
    assert "default_reasoning_effort" in ADMIN_CONTACT_LOOKUP_FROM_FIELDS
    assert "slow_brain_model" in ADMIN_CONTACT_LOOKUP_FROM_FIELDS
    assert "slow_brain_reasoning_effort" in ADMIN_CONTACT_LOOKUP_FROM_FIELDS


def test_get_assistant_skips_universal_coordinator_email_lookup(monkeypatch):
    monkeypatch.setattr(
        "common.assistant_lookup.SETTINGS.unity_coordinator_email_address",
        "staging-twin@unify.ai",
    )

    def fail_get(*_args, **_kwargs):
        raise AssertionError("Orchestra should not be called for universal email")

    monkeypatch.setattr("common.assistant_lookup.requests.get", fail_get)

    result = get_assistant(email_address="staging-twin@unify.ai")

    assert result["assistant_id"] is None


def test_get_assistant_passes_from_fields_for_email_lookup(monkeypatch):
    calls = []

    class Response:
        def json(self):
            return {"detail": "not found"}

    def fake_get(url, *, params, headers, timeout=None):
        calls.append({"params": params})
        return Response()

    monkeypatch.setattr(
        "common.assistant_lookup.SETTINGS.orchestra_url",
        "https://api.test",
    )
    monkeypatch.setattr(
        "common.assistant_lookup.SETTINGS.orchestra_admin_key",
        "admin-key",
    )
    monkeypatch.setattr("common.assistant_lookup.requests.get", fake_get)

    get_assistant(email_address="byod@example.com")

    assert calls[0]["params"] == {
        "email": "byod@example.com",
        "from_fields": ADMIN_CONTACT_LOOKUP_FROM_FIELDS,
    }


def test_get_assistant_does_not_log_sensitive_response_fields(monkeypatch, caplog):
    class Response:
        def json(self):
            return {
                "info": [
                    {
                        "agent_id": "assistant-123",
                        "api_key": "secret-api-key",
                        "desktop_filesync_sshkey": "secret-ssh-key",
                    },
                ],
            }

    monkeypatch.setattr(
        "common.assistant_lookup.SETTINGS.orchestra_url",
        "https://api.test",
    )
    monkeypatch.setattr(
        "common.assistant_lookup.SETTINGS.orchestra_admin_key",
        "admin-key",
    )
    monkeypatch.setattr(
        "common.assistant_lookup.requests.get",
        lambda *_args, **_kwargs: Response(),
    )
    monkeypatch.setattr(
        "common.assistant_lookup._assistant_payload",
        lambda _assistant: {},
    )
    caplog.set_level(logging.INFO, logger="common.assistant_lookup")

    get_assistant(assistant_id="assistant-123")

    logged = caplog.text
    assert "secret-api-key" not in logged
    assert "secret-ssh-key" not in logged
    assert "result_count=1" in logged


class _AccessResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_runtime_access_refused_for_account_on_free_credit(monkeypatch):
    captured: dict = {}

    def _get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _AccessResponse({"allowed": False, "reason": "payment_required"})

    monkeypatch.setattr("common.assistant_lookup.requests.get", _get)

    allowed = assistant_may_start_runtime(
        {"user_id": "user-123", "organization_id": 42},
    )

    assert allowed is False
    assert captured["url"].endswith("/admin/billing/runtime-access")
    assert captured["params"] == {"user_id": "user-123", "organization_id": "42"}


def test_runtime_access_allows_a_paying_account(monkeypatch):
    monkeypatch.setattr(
        "common.assistant_lookup.requests.get",
        lambda *a, **k: _AccessResponse({"allowed": True, "reason": None}),
    )

    assert assistant_may_start_runtime({"user_id": "user-123"}) is True


def test_runtime_access_allows_when_orchestra_is_unreachable(monkeypatch):
    """The spend gate in the pod is the backstop; this one must not
    stop every customer's schedule during an unrelated outage."""

    def _boom(*_args, **_kwargs):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr("common.assistant_lookup.requests.get", _boom)

    assert assistant_may_start_runtime({"user_id": "user-123"}) is True


def test_runtime_access_allows_an_assistant_with_no_owner(monkeypatch):
    """Local and fixture assistants carry no user; they are not customers."""

    def _unreached(*_args, **_kwargs):
        raise AssertionError("no lookup should happen without an owner")

    monkeypatch.setattr("common.assistant_lookup.requests.get", _unreached)

    assert assistant_may_start_runtime({"assistant_id": "local-assistant"}) is True
