"""Shared assistant lookup helpers for Communication runtimes."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from common.metrics import ORCHESTRA_GET_ASSISTANT_DURATION
from common.coordinator_voice import resolve_runtime_voice
from common.settings import SETTINGS

logger = logging.getLogger(__name__)

NO_DESKTOP_MODE = "none"
ASSISTANT_LOOKUP_TIMEOUT_SECONDS = 30
ADMIN_CONTACT_LOOKUP_FROM_FIELDS = (
    "agent_id,api_key,secrets,email,email_provider,phone,user_id,user_email,"
    "user_first_name,user_last_name,user_phone,user_whatsapp_number,"
    "assistant_whatsapp_number,self_contact_id,boss_contact_id,team_ids,"
    "is_coordinator,is_multiplayer,organization_id,voice_id,voice_provider,first_name,"
    "surname,deploy_env,desktop_mode,managed_desktop_status,user_desktops,is_local,"
    "assistant_discord_bot_id,assistant_slack_bot_user_id,assistant_slack_team_id,"
    "assistant_has_ms_teams_bot,assistant_ms_teams_tenant_id,"
    "age,nationality,"
    "about,job_title,timezone,default_model,default_reasoning_effort,"
    "slow_brain_model,slow_brain_reasoning_effort"
)


def managed_desktop_entitled(assistant: dict[str, Any]) -> bool:
    """Return whether the assistant may receive a managed pool VM."""
    desktop_mode = assistant.get("desktop_mode")
    return (
        desktop_mode in ("ubuntu", "windows")
        and assistant.get("managed_desktop_status") == "active"
    )


def _resolve_desktop_mode(assistant: dict[str, Any]) -> str:
    if managed_desktop_entitled(assistant):
        return str(assistant["desktop_mode"])
    return NO_DESKTOP_MODE


def _runtime_str(value: object) -> str:
    return "" if value is None else str(value)


def _local_assistant_data() -> dict[str, Any]:
    return {
        "assistant_id": "local-assistant",
        "deploy_env": None,
        "user_id": "local-user",
        "voice_provider": "cartesia",
        "voice_id": None,
        "default_model": None,
        "default_reasoning_effort": None,
        "slow_brain_model": None,
        "slow_brain_reasoning_effort": None,
        "api_key": "",
        "user_first_name": "",
        "user_surname": "",
        "assistant_first_name": "Local",
        "assistant_surname": "Assistant",
        "assistant_age": "20",
        "assistant_nationality": "United States",
        "assistant_about": "Local Assistant",
        "assistant_job_title": "",
        "assistant_timezone": "UTC",
        "assistant_email": "unify.agent@unify.ai",
        "user_email": "unify.agent@unify.ai",
        "user_number": "",
        "assistant_number": "",
        "user_whatsapp_number": "",
        "assistant_whatsapp_number": "",
        "assistant_discord_bot_id": "",
        "assistant_has_ms_teams_bot": False,
        "assistant_ms_teams_tenant_id": "",
        "desktop_mode": NO_DESKTOP_MODE,
        "user_desktops": [],
        "is_local": True,
        "team_ids": [],
        "team_summaries": [],
        "self_contact_id": 0,
        "boss_contact_id": 1,
        "is_coordinator": False,
    }


def _assistant_payload(assistant: dict[str, Any]) -> dict[str, Any]:
    is_coordinator = assistant.get("is_coordinator", False)
    voice_provider, voice_id = resolve_runtime_voice(
        is_coordinator=is_coordinator,
        voice_provider=assistant.get("voice_provider"),
        voice_id=assistant.get("voice_id"),
    )
    return {
        "assistant_id": _runtime_str(assistant["agent_id"]),
        "deploy_env": assistant.get("deploy_env"),
        "user_id": _runtime_str(assistant["user_id"]),
        "api_key": _runtime_str(assistant["api_key"]),
        "user_first_name": _runtime_str(assistant["user_first_name"]),
        "user_surname": _runtime_str(assistant["user_last_name"]),
        "assistant_first_name": _runtime_str(assistant["first_name"]),
        "assistant_surname": _runtime_str(assistant["surname"]),
        "assistant_age": _runtime_str(assistant.get("age")),
        "assistant_nationality": _runtime_str(assistant["nationality"]),
        "assistant_about": _runtime_str(assistant["about"]),
        "assistant_job_title": _runtime_str(assistant.get("job_title")),
        "assistant_timezone": _runtime_str(assistant.get("timezone") or "UTC"),
        "assistant_number": assistant["phone"] or "",
        "assistant_whatsapp_number": assistant.get("assistant_whatsapp_number") or "",
        "assistant_discord_bot_id": _runtime_str(
            assistant.get("assistant_discord_bot_id"),
        ),
        "assistant_slack_bot_user_id": _runtime_str(
            assistant.get("assistant_slack_bot_user_id"),
        ),
        "assistant_slack_team_id": _runtime_str(
            assistant.get("assistant_slack_team_id"),
        ),
        "assistant_has_ms_teams_bot": bool(
            assistant.get("assistant_has_ms_teams_bot"),
        ),
        "assistant_ms_teams_tenant_id": _runtime_str(
            assistant.get("assistant_ms_teams_tenant_id"),
        ),
        "assistant_email": assistant["email"] or "",
        "assistant_email_provider": assistant.get("email_provider")
        or "google_workspace",
        "user_number": assistant["user_phone"] or "",
        "user_whatsapp_number": assistant.get("user_whatsapp_number") or "",
        "user_email": assistant["user_email"] or "",
        "voice_provider": voice_provider,
        "voice_id": voice_id,
        "default_model": assistant.get("default_model"),
        "default_reasoning_effort": assistant.get("default_reasoning_effort"),
        "slow_brain_model": assistant.get("slow_brain_model"),
        "slow_brain_reasoning_effort": assistant.get(
            "slow_brain_reasoning_effort",
        ),
        "secrets": assistant.get("secrets", {}),
        "desktop_mode": _resolve_desktop_mode(assistant),
        "managed_desktop_status": assistant.get("managed_desktop_status"),
        "user_desktops": assistant.get("user_desktops", []),
        "is_local": assistant.get("is_local", False),
        "team_ids": assistant.get("team_ids", []),
        "team_summaries": assistant.get("team_summaries", []),
        # Team-owned assistants have no personal root: the runtime routes
        # shared-scoped tables (Data, Tasks, Contacts, …) to Teams/{owner}/…
        # via SESSION_DETAILS.owner_team_id. Dropping this field silently
        # routes offline work to the personal root instead.
        "owner_team_id": assistant.get("owner_team_id"),
        "self_contact_id": assistant.get("self_contact_id") or 0,
        "boss_contact_id": assistant.get("boss_contact_id") or 1,
        "is_coordinator": assistant.get("is_coordinator", False),
        "org_id": assistant.get("organization_id"),
    }


def get_assistant(
    email_address: str | None = None,
    phone_number: str | None = None,
    assistant_id: str | None = None,
    *,
    from_fields: str | None = None,
) -> dict[str, Any]:
    """Return assistant/user runtime configuration from Orchestra."""

    params: dict[str, str] = {}
    if assistant_id:
        params["agent_id"] = assistant_id
    elif email_address:
        normalized_email = email_address.strip().lower()
        if normalized_email == SETTINGS.unity_coordinator_email_address.strip().lower():
            return {**_local_assistant_data(), "assistant_id": None}
        params["email"] = email_address
        params["from_fields"] = from_fields or ADMIN_CONTACT_LOOKUP_FROM_FIELDS
    elif phone_number:
        params["phone"] = phone_number
        params["from_fields"] = from_fields or ADMIN_CONTACT_LOOKUP_FROM_FIELDS
    elif from_fields:
        params["from_fields"] = from_fields

    email_check = email_address or ""
    phone_check = phone_number or ""
    local_assistant_data = _local_assistant_data()
    if "+15550100002" in phone_check or assistant_id == "local-assistant":
        return local_assistant_data
    if (
        "+0123456789" in phone_check
        or "local-test-assistant@unify.ai" in email_check
        or assistant_id == "local-test-assistant"
    ):
        return {
            **local_assistant_data,
            "assistant_id": "local-test-assistant",
            "user_first_name": "Test",
            "user_surname": "User",
            "user_number": "+9876543210",
            "user_email": "test@unify.ai",
            "assistant_first_name": "Test",
            "assistant_surname": "Assistant",
            "assistant_number": "+0123456789",
            "assistant_email": "local-test-assistant@unify.ai",
            "user_whatsapp_number": "+9876543210",
        }

    lookup_type = "id" if assistant_id else ("email" if email_address else "phone")
    start = time.perf_counter()
    status = "error"
    try:
        response = requests.get(
            f"{SETTINGS.orchestra_url}/admin/assistant",
            params=params,
            headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
            timeout=ASSISTANT_LOOKUP_TIMEOUT_SECONDS,
        ).json()
        status = "error" if "detail" in response else "success"
    finally:
        ORCHESTRA_GET_ASSISTANT_DURATION.labels(
            lookup_type=lookup_type,
            status=status,
        ).observe(time.perf_counter() - start)

    # The assistant record includes bearer credentials and SSH material. Log
    # only query/result metadata; never serialize the request selectors or
    # response payload into Cloud Logging.
    logger.info(
        "get_assistant completed lookup_type=%s status=%s result_count=%d",
        lookup_type,
        status,
        len(response.get("info", [])) if isinstance(response, dict) else 0,
    )

    if "detail" in response:
        return {**local_assistant_data, "assistant_id": None}
    assistants = response["info"]
    if len(assistants) == 0:
        return {**local_assistant_data, "assistant_id": None}

    return _assistant_payload(assistants[0])
