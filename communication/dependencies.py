import asyncio
import logging
import secrets
from dataclasses import dataclass, field

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from starlette import status

from common.settings import SETTINGS

logger = logging.getLogger(__name__)

security = HTTPBearer()

VM_IDENTITY_AUDIENCE = "unity-comms-vm"
VM_SA_EMAIL = f"pool-vm-sa@{SETTINGS.vm_project_id}.iam.gserviceaccount.com"


def auth_admin_key(
    request_fastapi: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> None:
    admin_key = credentials.credentials

    expected_key = SETTINGS.orchestra_admin_key
    if expected_key and secrets.compare_digest(admin_key, expected_key):
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin access unauthorized.",
    )


async def authenticate_user_api_key(api_key: str) -> dict:
    """
    Validate a user API key against Orchestra's /user/basic-info endpoint.

    Returns the user info dict (contains user_id, email, etc.) on success.
    Raises HTTPException(401) on failure.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{SETTINGS.orchestra_url}/user/basic-info",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )

    if response.status_code != 200:
        logger.warning(f"API key authentication failed: {response.status_code}")
        raise HTTPException(status_code=401, detail="Invalid API key.")

    return response.json()


def extract_api_key(request: Request) -> str:
    """Extract the Bearer token from the Authorization header."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    raise HTTPException(status_code=401, detail="Missing API key.")


@dataclass(frozen=True)
class AssistantSessionIdentity:
    """Server-authoritative identity for an assistant session caller.

    Populated from the bootstrap secret (``startup.json``) of the assistant's
    live ``AssistantSession`` after the caller's ``UNIFY_KEY`` has been verified
    against that session. All fields are the server's stored values, never
    client-supplied query params.
    """

    assistant_id: int
    api_key: str
    org_id: int | None = None
    user_id: str | None = None
    team_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class CallerContext:
    """Result of :func:`authorize_admin_or_assistant`.

    ``is_admin`` marks a trusted control-plane caller (session controller,
    adapters, reconcile) presenting the platform admin key. Otherwise
    ``identity`` holds the verified per-assistant session identity, and the
    caller may only act on its own assistant.
    """

    is_admin: bool
    identity: AssistantSessionIdentity | None = None


def _is_admin_key(candidate: str) -> bool:
    expected_key = SETTINGS.orchestra_admin_key
    return bool(expected_key) and secrets.compare_digest(candidate, expected_key)


async def authorize_admin_or_assistant(
    request: Request,
    *,
    assistant_id: int | str,
    requested_binding_id: str | None = None,
) -> CallerContext:
    """Accept either the platform admin key or the assistant's own session key.

    Control-plane callers keep using ``ORCHESTRA_ADMIN_KEY`` (unchanged). A pod
    acting on its own behalf presents its ``UNIFY_KEY``; it is verified against
    the ``AssistantSession`` for *assistant_id* so it can only ever act on
    itself. This lets pod-callable ``/infra/*`` routes drop their reliance on
    the shared admin key without widening access for other assistants.
    """
    token = extract_api_key(request)
    if _is_admin_key(token):
        return CallerContext(is_admin=True)
    try:
        assistant_id_int = int(assistant_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="A numeric assistant_id is required for assistant-scoped auth.",
        )
    identity = await verify_assistant_session(
        api_key=token,
        assistant_id=assistant_id_int,
        requested_binding_id=requested_binding_id,
    )
    return CallerContext(is_admin=False, identity=identity)


async def verify_assistant_identity_from_key(
    *,
    api_key: str,
    assistant_id: int,
) -> AssistantSessionIdentity:
    """Authorize a caller that holds the assistant's own Orchestra API key.

    Used by read-only bootstrap endpoints (notably ``/infra/client-bundle``)
    that must work for headless offline jobs as well as live pods. Offline
    jobs carry ``UNIFY_KEY`` but never create an ``AssistantSession``, so
    session-bound auth would 409 them.

    Trust model:
    1. The bearer must be a valid Orchestra user API key.
    2. That key must ``compare_digest``-match the server-loaded assistant
       record for *assistant_id* (looked up with the platform admin key).
    3. Identity fields (``org_id``, ``user_id``, ``team_ids``) come only from
       that Orchestra record — never from client query params.

    A leaked ``ORCHESTRA_ADMIN_KEY`` fails step 1 (it is not a user key).
    Assistant A's key with ``assistant_id=B`` fails step 2.
    """
    from common.assistant_lookup import get_assistant

    await authenticate_user_api_key(api_key)

    assistant_data = await asyncio.to_thread(
        get_assistant,
        assistant_id=str(assistant_id),
    )
    if not assistant_data or not assistant_data.get("assistant_id"):
        raise HTTPException(
            status_code=404,
            detail="Assistant not found.",
        )

    expected_api_key = str(assistant_data.get("api_key") or "")
    if not expected_api_key or not secrets.compare_digest(api_key, expected_api_key):
        raise HTTPException(
            status_code=401,
            detail="API key does not match the requested assistant.",
        )

    raw_team_ids = assistant_data.get("team_ids") or []
    team_ids = [int(t) for t in raw_team_ids if str(t).strip()]
    raw_org = assistant_data.get("org_id")
    raw_user = assistant_data.get("user_id")
    return AssistantSessionIdentity(
        assistant_id=int(assistant_data["assistant_id"]),
        api_key=expected_api_key,
        org_id=int(raw_org) if raw_org not in (None, "") else None,
        user_id=str(raw_user) if raw_user not in (None, "") else None,
        team_ids=team_ids,
    )


async def verify_assistant_session(
    *,
    api_key: str,
    assistant_id: int,
    requested_binding_id: str | None = None,
) -> AssistantSessionIdentity:
    """Authorize a pod acting as its own assistant.

    Mirrors the ``/infra/vm/ready`` pattern: authenticate the bearer as an
    Orchestra user key, load the ``AssistantSession`` for *assistant_id*, and
    require that the caller's key equals the ``api_key`` stored in that
    session's bootstrap secret. When *requested_binding_id* is supplied it must
    match the session's current binding. Identity fields are then derived from
    the server-stored bootstrap payload so callers cannot spoof another tenant
    by passing a different ``org_id``/``assistant_id``.

    Raises ``HTTPException`` (401/409/500) on any mismatch or missing session.
    Prefer :func:`verify_assistant_identity_from_key` for read-only bootstrap
    that must also serve headless offline jobs (no live session).
    """
    from communication.infra.assistant_sessions import (
        binding_id as binding_id_of,
        get_assistant_session,
        get_custom_objects_api,
        read_bootstrap_secret,
        session_binding,
    )
    from communication.infra.runtime_clients import get_k8s_clients

    await authenticate_user_api_key(api_key)

    custom_api = await asyncio.to_thread(get_custom_objects_api)
    if custom_api is None:
        raise HTTPException(
            status_code=500,
            detail="Failed to initialize AssistantSession API client",
        )
    _, core_api, _, _ = await get_k8s_clients()

    session = await asyncio.to_thread(
        get_assistant_session,
        custom_api,
        SETTINGS.default_namespace,
        str(assistant_id),
    )
    if session is None:
        raise HTTPException(
            status_code=409,
            detail="No active AssistantSession for the requested assistant.",
        )

    secret_name = session.get("spec", {}).get("startupSecretRef", "")
    if not secret_name:
        raise HTTPException(
            status_code=409,
            detail="AssistantSession has no bootstrap secret reference.",
        )
    startup_payload = await asyncio.to_thread(
        read_bootstrap_secret,
        core_api,
        SETTINGS.default_namespace,
        secret_name,
    )

    expected_api_key = str(startup_payload.get("api_key", ""))
    if not expected_api_key or not secrets.compare_digest(api_key, expected_api_key):
        raise HTTPException(
            status_code=401,
            detail="API key does not match the active AssistantSession.",
        )

    if requested_binding_id is not None:
        current_binding_id = binding_id_of(session_binding(session))
        if current_binding_id and current_binding_id != requested_binding_id:
            raise HTTPException(
                status_code=409,
                detail="Binding id does not match the active AssistantSession.",
            )

    raw_team_ids = startup_payload.get("team_ids") or []
    team_ids = [int(t) for t in raw_team_ids if str(t).strip()]
    raw_org = startup_payload.get("org_id")
    raw_user = startup_payload.get("user_id")
    return AssistantSessionIdentity(
        assistant_id=int(startup_payload.get("assistant_id", assistant_id)),
        api_key=expected_api_key,
        org_id=int(raw_org) if raw_org not in (None, "") else None,
        user_id=str(raw_user) if raw_user not in (None, "") else None,
        team_ids=team_ids,
    )


def authenticate_vm_identity(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """Verify a GCP identity token from a pool VM.

    Returns the verified claims dict containing:
      - email: service account email
      - google.compute_engine.instance_name
      - google.compute_engine.project_id
      - google.compute_engine.zone
    """
    token = credentials.credentials
    try:
        claims = google_id_token.verify_token(
            token,
            google_requests.Request(),
            audience=VM_IDENTITY_AUDIENCE,
        )
    except Exception:
        raise HTTPException(status_code=403, detail="Invalid VM identity token.")

    if claims.get("email") != VM_SA_EMAIL:
        raise HTTPException(
            status_code=403,
            detail="Token is not from an authorized pool VM service account.",
        )

    gce = claims.get("google", {}).get("compute_engine", {})
    if not gce.get("instance_name"):
        raise HTTPException(
            status_code=403,
            detail="Token missing compute engine identity claims.",
        )

    return claims
