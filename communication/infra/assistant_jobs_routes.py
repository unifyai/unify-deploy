"""Infra proxies for AssistantJobs audit writes.

Assistant pods must not hold ``ORCHESTRA_ADMIN_KEY`` or a shared user key.
They call these ownership-scoped ``/infra/assistant-jobs/*`` routes with their
own ``UNIFY_KEY``; the comms app writes to the system ``AssistantJobs`` project
as ``__system__`` via the admin key.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import requests
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from common.settings import SETTINGS
from communication.dependencies import authorize_admin_or_assistant
from communication.infra.self_router import assistant_self_router

logger = logging.getLogger(__name__)

PROJECT_NAME = "AssistantJobs"
CONTEXT_NAME = "startup_events"
ORCHESTRA_TIMEOUT_SECONDS = 30


class AssistantJobsStartupRequest(BaseModel):
    """Create one ``startup_events`` audit row for this assistant session."""

    assistant_id: str
    user_id: str
    job_name: str
    timestamp: Optional[str] = None
    medium: str = ""
    user_name: str = ""
    assistant_name: str = ""
    user_number: str = ""
    assistant_number: str = ""
    user_email: str = ""
    assistant_email: str = ""


class AssistantJobsLiveviewRequest(BaseModel):
    """Set ``liveview_url`` on this assistant session's startup audit row."""

    assistant_id: str
    job_name: str
    liveview_url: str = Field(..., min_length=1)


def _orchestra_admin_headers() -> dict[str, str]:
    if not SETTINGS.orchestra_admin_key:
        raise RuntimeError(
            "ORCHESTRA_ADMIN_KEY is not configured on the communication service.",
        )
    return {
        "Authorization": f"Bearer {SETTINGS.orchestra_admin_key}",
        "Accept": "application/json",
    }


def _orchestra_base() -> str:
    if not SETTINGS.orchestra_url:
        raise RuntimeError(
            "ORCHESTRA_URL is not configured on the communication service.",
        )
    return SETTINGS.orchestra_url.rstrip("/")


def _ensure_assistant_jobs_context() -> None:
    """Create the startup_events context if missing (idempotent)."""
    response = requests.post(
        f"{_orchestra_base()}/project/{PROJECT_NAME}/contexts",
        headers=_orchestra_admin_headers(),
        json={
            "name": CONTEXT_NAME,
            "description": "Fleet audit and Console liveview discovery",
            "is_versioned": False,
            "allow_duplicates": True,
            "unique_keys": None,
        },
        timeout=ORCHESTRA_TIMEOUT_SECONDS,
    )
    if response.status_code in (200, 400):
        # 400 typically means the context already exists.
        return
    logger.warning(
        "AssistantJobs context ensure returned %s: %s",
        response.status_code,
        response.text[:300],
    )


def _create_startup_log(entries: dict[str, Any]) -> dict[str, Any]:
    _ensure_assistant_jobs_context()
    response = requests.post(
        f"{_orchestra_base()}/logs",
        headers=_orchestra_admin_headers(),
        json={
            "project_name": PROJECT_NAME,
            "context": CONTEXT_NAME,
            "entries": entries,
        },
        timeout=ORCHESTRA_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Orchestra AssistantJobs startup write failed "
            f"({response.status_code}): {response.text[:500]}",
        )
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(
            "Unexpected Orchestra response for AssistantJobs startup write.",
        )
    return body


def _find_startup_log_id(*, assistant_id: str, job_name: str) -> int | None:
    row_filter = f"assistant_id == '{assistant_id}' and job_name == '{job_name}'"
    response = requests.get(
        f"{_orchestra_base()}/logs",
        headers=_orchestra_admin_headers(),
        params={
            "project_name": PROJECT_NAME,
            "context": CONTEXT_NAME,
            "filter": row_filter,
            "limit": 1,
        },
        timeout=ORCHESTRA_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Orchestra AssistantJobs lookup failed "
            f"({response.status_code}): {response.text[:500]}",
        )
    body = response.json()
    logs = body.get("logs") if isinstance(body, dict) else None
    if not logs:
        return None
    log_id = logs[0].get("id")
    return int(log_id) if log_id is not None else None


def _update_liveview_url(*, log_id: int, liveview_url: str) -> None:
    response = requests.put(
        f"{_orchestra_base()}/logs",
        headers=_orchestra_admin_headers(),
        json={
            "logs": [log_id],
            "context": CONTEXT_NAME,
            "entries": {"liveview_url": liveview_url},
            "overwrite": True,
        },
        timeout=ORCHESTRA_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Orchestra AssistantJobs liveview update failed "
            f"({response.status_code}): {response.text[:500]}",
        )


@assistant_self_router.post("/assistant-jobs/startup")
async def create_assistant_jobs_startup(
    request: Request,
    body: AssistantJobsStartupRequest,
) -> dict[str, Any]:
    """Create the AssistantJobs startup audit row for this session."""
    await authorize_admin_or_assistant(request, assistant_id=body.assistant_id)
    entries = body.model_dump(exclude_none=True)
    try:
        result = await asyncio.to_thread(_create_startup_log, entries)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "success": True,
        "project": PROJECT_NAME,
        "context": CONTEXT_NAME,
        "log_event_ids": result.get("log_event_ids"),
    }


@assistant_self_router.patch("/assistant-jobs/liveview")
async def patch_assistant_jobs_liveview(
    request: Request,
    body: AssistantJobsLiveviewRequest,
) -> dict[str, Any]:
    """Persist liveview_url on this session's AssistantJobs startup row."""
    await authorize_admin_or_assistant(request, assistant_id=body.assistant_id)
    try:
        log_id = await asyncio.to_thread(
            _find_startup_log_id,
            assistant_id=body.assistant_id,
            job_name=body.job_name,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if log_id is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No AssistantJobs startup row for assistant_id={body.assistant_id} "
                f"job_name={body.job_name}"
            ),
        )
    try:
        await asyncio.to_thread(
            _update_liveview_url,
            log_id=log_id,
            liveview_url=body.liveview_url,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "success": True,
        "log_id": log_id,
        "liveview_url": body.liveview_url,
    }
