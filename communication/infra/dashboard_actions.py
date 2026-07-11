"""Dashboard action dispatch for Communication.

Sibling to :mod:`task_activation` -- reuses the same offline runner
infrastructure (``offline_runner.py``, ``create_unity_job``,
``_create_or_adopt_task_run``) but with a different trigger source
(Console tile button click) and a simpler validation path (no
activation revision, no scheduler dedup).
"""

from __future__ import annotations

import json
import asyncio
import hashlib
import uuid
from typing import Any

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from common.assistant_lookup import get_assistant
from common.int_list_codec import encode_int_list_for_env
from common.team_summaries_codec import encode_team_summaries_for_env
from common.settings import SETTINGS

from .helpers import create_unity_job
from .runtime_clients import get_k8s_clients as _get_k8s_clients
from .task_activation import (
    _create_or_adopt_task_run,
    _normalize_task_id_component,
    _orchestra_admin_headers,
    _required_contact_id,
    _running_task_run_updates,
    _update_task_run,
    TASK_DUE_HTTP_TIMEOUT_SECONDS,
)

router = APIRouter()


class DashboardActionDispatchRequest(BaseModel):
    """Dispatch one dashboard action execution attempt."""

    assistant_id: str
    tile_token: str
    action_name: str
    payload: dict = {}


def _lookup_dashboard_action(
    *,
    assistant_id: str,
    tile_token: str,
    action_name: str,
) -> dict[str, Any]:
    """Fetch action metadata from the Dashboards/Actions context in Orchestra."""

    if not SETTINGS.orchestra_url or not SETTINGS.orchestra_admin_key:
        raise RuntimeError("ORCHESTRA_URL and ORCHESTRA_ADMIN_KEY must be configured")

    url = (
        f"{SETTINGS.orchestra_url}/v0/admin/dashboards/actions"
        f"/{tile_token}/{action_name}"
    )
    response = requests.get(
        url,
        headers=_orchestra_admin_headers(),
        timeout=TASK_DUE_HTTP_TIMEOUT_SECONDS,
    )
    if response.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail=f"Action '{action_name}' not found for tile '{tile_token}'",
        )
    response.raise_for_status()
    return response.json()


def _build_dashboard_action_run_key(request: DashboardActionDispatchRequest) -> str:
    """Build a unique run key for one dashboard action execution."""

    unique = uuid.uuid4().hex[:12]
    return (
        f"dashboard_action:{request.assistant_id}:"
        f"{request.tile_token}:{request.action_name}:{unique}"
    )


def _build_dashboard_action_run_payload(
    request: DashboardActionDispatchRequest,
    run_key: str,
    action_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Return the initial Orchestra payload for one dashboard action run."""

    return {
        "run_key": run_key,
        "assistant_id": request.assistant_id,
        "task_id": 0,
        "source_task_log_id": 0,
        "source_type": "dashboard_action",
        "execution_mode": "offline",
        "activation_revision": "",
        "task_name": request.action_name,
        "task_description": f"Dashboard action: {request.action_name}",
        "state": "pending",
    }


def _build_dashboard_action_env(
    *,
    request: DashboardActionDispatchRequest,
    action_metadata: dict[str, Any],
    assistant_data: dict[str, Any],
    run_key: str,
    job_name: str,
) -> dict[str, str]:
    """Build env vars for the headless Unity offline runner (dashboard action variant)."""

    team_ids = assistant_data.get("team_ids") or []
    team_summaries = assistant_data.get("team_summaries") or []
    self_contact_id = _required_contact_id(assistant_data, "self_contact_id")
    boss_contact_id = _required_contact_id(assistant_data, "boss_contact_id")
    return {
        "UNITY_OFFLINE_TASK_MODE": "function",
        "UNITY_OFFLINE_TASK_FUNCTION_ID": str(action_metadata["function_id"]),
        "UNITY_OFFLINE_TASK_REQUEST": action_metadata.get(
            "request",
            f"Execute dashboard action: {request.action_name}",
        ),
        "UNITY_OFFLINE_TASK_RUN_KEY": run_key,
        "UNITY_OFFLINE_TASK_JOB_NAME": job_name,
        "UNITY_OFFLINE_TASK_ID": "0",
        "UNITY_OFFLINE_TASK_SOURCE_TASK_LOG_ID": "0",
        "UNITY_OFFLINE_TASK_ACTIVATION_REVISION": "",
        "UNITY_OFFLINE_TASK_SOURCE_TYPE": "dashboard_action",
        "UNITY_OFFLINE_TASK_NAME": request.action_name,
        "UNITY_OFFLINE_TASK_DESCRIPTION": f"Dashboard action: {request.action_name}",
        "UNITY_OFFLINE_TASK_SCHEDULED_FOR": "",
        "UNITY_OFFLINE_TASK_SOURCE_REF": "",
        "UNITY_OFFLINE_TASK_SOURCE_MEDIUM": "",
        "UNITY_OFFLINE_TASK_SOURCE_CONTACT_ID": "",
        "EVENTBUS_PUBLISHING_ENABLED": "false",
        "EVENTBUS_PUBSUB_STREAMING": "false",
        "UNIFY_KEY": str(assistant_data.get("api_key") or ""),
        "ASSISTANT_ID": str(assistant_data.get("assistant_id") or request.assistant_id),
        "ASSISTANT_FIRST_NAME": str(assistant_data.get("assistant_first_name") or ""),
        "ASSISTANT_SURNAME": str(assistant_data.get("assistant_surname") or ""),
        "ASSISTANT_AGE": str(assistant_data.get("assistant_age") or ""),
        "ASSISTANT_NATIONALITY": str(assistant_data.get("assistant_nationality") or ""),
        "ASSISTANT_TIMEZONE": str(assistant_data.get("assistant_timezone") or "UTC"),
        "ASSISTANT_DEFAULT_MODEL": str(assistant_data.get("default_model") or ""),
        "ASSISTANT_DEFAULT_REASONING_EFFORT": str(
            assistant_data.get("default_reasoning_effort") or "",
        ),
        "ASSISTANT_SLOW_BRAIN_MODEL": str(assistant_data.get("slow_brain_model") or ""),
        "ASSISTANT_SLOW_BRAIN_REASONING_EFFORT": str(
            assistant_data.get("slow_brain_reasoning_effort") or "",
        ),
        "ASSISTANT_ABOUT": str(assistant_data.get("assistant_about") or ""),
        "ASSISTANT_JOB_TITLE": str(assistant_data.get("assistant_job_title") or ""),
        "ASSISTANT_NUMBER": str(assistant_data.get("assistant_number") or ""),
        "ASSISTANT_EMAIL": str(assistant_data.get("assistant_email") or ""),
        "ASSISTANT_WHATSAPP_NUMBER": str(
            assistant_data.get("assistant_whatsapp_number") or "",
        ),
        "SELF_CONTACT_ID": str(self_contact_id),
        "ASSISTANT_DESKTOP_MODE": "none",
        "ASSISTANT_USER_DESKTOPS": json.dumps(
            assistant_data.get("user_desktops") or [],
        ),
        "ASSISTANT_IS_COORDINATOR": "False",
        "USER_ID": str(assistant_data.get("user_id") or ""),
        "USER_FIRST_NAME": str(assistant_data.get("user_first_name") or ""),
        "USER_SURNAME": str(assistant_data.get("user_surname") or ""),
        "USER_NUMBER": str(assistant_data.get("user_number") or ""),
        "USER_EMAIL": str(assistant_data.get("user_email") or ""),
        "USER_WHATSAPP_NUMBER": str(assistant_data.get("user_whatsapp_number") or ""),
        "BOSS_CONTACT_ID": str(boss_contact_id),
        "VOICE_PROVIDER": str(assistant_data.get("voice_provider") or "cartesia"),
        "VOICE_ID": str(assistant_data.get("voice_id") or ""),
        "VOICE_MODE": "tts",
        "TEAM_IDS": encode_int_list_for_env(team_ids, field_name="team_ids"),
        "TEAM_SUMMARIES": encode_team_summaries_for_env(
            team_summaries,
            field_name="team_summaries",
        ),
        "ORG_ID": (
            str(assistant_data.get("org_id"))
            if assistant_data.get("org_id") is not None
            else ""
        ),
    }


def _build_dashboard_action_job_name(run_key: str) -> str:
    """Return the deterministic Kubernetes Job name for one dashboard action run."""

    digest = hashlib.sha256(run_key.encode("utf-8")).hexdigest()[:12]
    base_name = f"unity-dashboard-action-{digest}"
    suffix = SETTINGS.env_suffix.lstrip("-")
    return f"{base_name}-{suffix}" if suffix else base_name


def _launch_dashboard_action_job(
    *,
    batch_api: Any,
    request: DashboardActionDispatchRequest,
    action_metadata: dict[str, Any],
    assistant_data: dict[str, Any],
    run_key: str,
) -> tuple[str, bool]:
    """Create the Kubernetes Job that runs the headless Unity executor for a dashboard action."""

    job_name = _build_dashboard_action_job_name(run_key)
    job = create_unity_job(
        batch_api,
        job_name=job_name,
        namespace=SETTINGS.default_namespace,
        ttl_seconds_after_finished=SETTINGS.offline_task_job_ttl_seconds,
        active_deadline_seconds=SETTINGS.offline_task_job_active_deadline_seconds,
        unity_status="offline",
        priority_class_name="unity-idle",
        app_label="unity-dashboard-action",
        extra_labels={
            "assistant-id": _normalize_task_id_component(request.assistant_id)[:63],
            "unity-status": "offline",
            "action-name": _normalize_task_id_component(request.action_name)[:63],
        },
        extra_annotations={
            "unify.ai/dashboard-action-run-key": run_key,
            "unify.ai/tile-token": request.tile_token,
            "unify.ai/action-name": request.action_name,
        },
        extra_env=_build_dashboard_action_env(
            request=request,
            action_metadata=action_metadata,
            assistant_data=assistant_data,
            run_key=run_key,
            job_name=job_name,
        ),
    )
    return job_name, job is not None


@router.post("/dashboard-action/dispatch")
async def dispatch_dashboard_action(request: DashboardActionDispatchRequest):
    """Validate and launch one headless dashboard action execution."""

    try:
        action_metadata = await asyncio.to_thread(
            _lookup_dashboard_action,
            assistant_id=request.assistant_id,
            tile_token=request.tile_token,
            action_name=request.action_name,
        )

        assistant_data = await asyncio.to_thread(
            get_assistant,
            assistant_id=request.assistant_id,
        )
        if not assistant_data or not assistant_data.get("assistant_id"):
            raise HTTPException(
                status_code=404,
                detail=f"Assistant {request.assistant_id} not found",
            )

        run_key = _build_dashboard_action_run_key(request)

        await asyncio.to_thread(
            _create_or_adopt_task_run,
            _build_dashboard_action_run_payload(request, run_key, action_metadata),
        )

        batch_api, _, _, _ = await _get_k8s_clients()
        job_name, job_created = await asyncio.to_thread(
            _launch_dashboard_action_job,
            batch_api=batch_api,
            request=request,
            action_metadata=action_metadata,
            assistant_data=assistant_data,
            run_key=run_key,
        )

        await asyncio.to_thread(
            _update_task_run,
            assistant_id=request.assistant_id,
            run_key=run_key,
            updates=_running_task_run_updates(job_name),
        )

    except HTTPException:
        raise
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Dashboard action dispatch failed while talking to Orchestra: {exc}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to dispatch dashboard action: {exc}",
        ) from exc

    return {
        "success": True,
        "status": "launched",
        "run_key": run_key,
        "job_name": job_name,
    }
