"""Shared assistant lookup helpers for Communication runtimes."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from common.metrics import ORCHESTRA_GET_ASSISTANT_DURATION
from common.settings import SETTINGS

logger = logging.getLogger(__name__)

NO_DESKTOP_MODE = "none"
COORDINATOR_DEFAULT_DESKTOP_MODE = "ubuntu"
ASSISTANT_LOOKUP_TIMEOUT_SECONDS = 30


def _resolve_desktop_mode(assistant: dict[str, Any]) -> str:
    desktop_mode = assistant.get("desktop_mode")
    if desktop_mode:
        return str(desktop_mode)
    if assistant.get("is_coordinator", False):
        return COORDINATOR_DEFAULT_DESKTOP_MODE
    return NO_DESKTOP_MODE


def _local_assistant_data() -> dict[str, Any]:
    return {
        "assistant_id": "local-assistant",
        "deploy_env": None,
        "user_id": "local-user",
        "voice_provider": "cartesia",
        "voice_id": None,
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
        "assistant_email": "unity.agent@unify.ai",
        "user_email": "unity.agent@unify.ai",
        "user_number": "",
        "assistant_number": "",
        "user_whatsapp_number": "",
        "assistant_whatsapp_number": "",
        "assistant_discord_bot_id": "",
        "desktop_mode": COORDINATOR_DEFAULT_DESKTOP_MODE,
        "user_desktops": [],
        "is_local": True,
        "team_ids": [],
        "team_summaries": [],
        "self_contact_id": 0,
        "boss_contact_id": 1,
        "is_coordinator": False,
    }


def _assistant_payload(assistant: dict[str, Any]) -> dict[str, Any]:
    return {
        "assistant_id": assistant["agent_id"],
        "deploy_env": assistant.get("deploy_env"),
        "user_id": assistant["user_id"],
        "api_key": assistant["api_key"],
        "user_first_name": assistant["user_first_name"],
        "user_surname": assistant["user_last_name"],
        "assistant_first_name": assistant["first_name"],
        "assistant_surname": assistant["surname"],
        "assistant_age": str(assistant.get("age", "")),
        "assistant_nationality": assistant["nationality"],
        "assistant_about": assistant["about"],
        "assistant_job_title": assistant.get("job_title") or "",
        "assistant_timezone": assistant.get("timezone", "UTC"),
        "assistant_number": assistant["phone"] or "",
        "assistant_whatsapp_number": assistant.get("assistant_whatsapp_number") or "",
        "assistant_discord_bot_id": assistant.get("assistant_discord_bot_id", ""),
        "assistant_email": assistant["email"] or "",
        "assistant_email_provider": assistant.get("email_provider")
        or "google_workspace",
        "user_number": assistant["user_phone"] or "",
        "user_whatsapp_number": assistant.get("user_whatsapp_number") or "",
        "user_email": assistant["user_email"] or "",
        "voice_provider": assistant["voice_provider"],
        "voice_id": assistant["voice_id"],
        "secrets": assistant.get("secrets", {}),
        "desktop_mode": _resolve_desktop_mode(assistant),
        "user_desktops": assistant.get("user_desktops", []),
        "demo_id": assistant.get("demo_id"),
        "is_local": assistant.get("is_local", False),
        "team_ids": assistant.get("team_ids", []),
        "team_summaries": assistant.get("team_summaries", []),
        "self_contact_id": assistant.get("self_contact_id", 0),
        "boss_contact_id": assistant.get("boss_contact_id", 1),
        "is_coordinator": assistant.get("is_coordinator", False),
        "org_id": assistant.get("organization_id"),
    }


def get_assistant(
    email_address: str | None = None,
    phone_number: str | None = None,
    assistant_id: str | None = None,
) -> dict[str, Any]:
    """Return assistant/user runtime configuration from Orchestra."""

    params = {}
    if email_address:
        params["email"] = email_address
    if phone_number:
        params["phone"] = phone_number
    if assistant_id:
        params["agent_id"] = assistant_id

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

    logger.info("get_assistant params: %s", params)
    logger.info("get_assistant response: %s", response)

    if "detail" in response:
        return {**local_assistant_data, "assistant_id": None}
    assistants = response["info"]
    if len(assistants) == 0:
        return {**local_assistant_data, "assistant_id": None}

    return _assistant_payload(assistants[0])
