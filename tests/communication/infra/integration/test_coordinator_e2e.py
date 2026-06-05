"""Opt-in end-to-end coverage for the live Coordinator contract."""

from collections.abc import Callable
import json
import os
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import pytest
import requests

from communication.infra.assistant_sessions import read_bootstrap_secret

from .conftest import (
    ADAPTERS_URL,
    ADMIN_KEY,
    NAMESPACE,
    ORCHESTRA_URL,
    UNIFY_KEY,
    _admin_record_to_data,
    poll_until,
    pull_outbound_messages,
    stop_assistant_runtime,
    wait_for_assistant_container_ready,
)

pytestmark = [pytest.mark.integration]

RUN_COORDINATOR_E2E = os.getenv("TEST_RUN_COORDINATOR_E2E") == "1"
REQUIRED_ROUTE_ENV = (
    "TEST_ORCHESTRA_URL",
    "TEST_ADAPTERS_URL",
    "TEST_COMMS_APP_URL",
)
COORDINATOR_SIDE_EFFECT_TIMEOUT_SECONDS = 180
COORDINATOR_SIDE_EFFECT_NUDGE_TIMEOUT_SECONDS = 180
COORDINATOR_RUNTIME_READY_ATTEMPTS = 3
COORDINATOR_RUNTIME_READY_REPLY_TIMEOUT_SECONDS = 90
ASSISTANTS_PROJECT_NAME = "Assistants"


def _require_preview_routing() -> None:
    """Ensure the opt-in run is pointed at one explicit deployment set."""

    missing_routes = [name for name in REQUIRED_ROUTE_ENV if not os.getenv(name)]
    assert not missing_routes, (
        "Coordinator e2e requires explicit service URLs so it does not "
        f"accidentally hit default staging routes. Missing: {', '.join(missing_routes)}"
    )
    route_slugs = {name: _preview_slug(os.environ[name]) for name in REQUIRED_ROUTE_ENV}
    missing_slugs = [name for name, slug in route_slugs.items() if not slug]
    assert not missing_slugs, (
        "Coordinator e2e must target preview-tagged service URLs. "
        f"Non-preview routes: {', '.join(missing_slugs)}"
    )
    unique_slugs = {slug for slug in route_slugs.values() if slug}
    assert len(unique_slugs) == 1, (
        "Coordinator e2e service URLs must share one preview slug. "
        f"Resolved slugs: {route_slugs}"
    )
    assert UNIFY_KEY, "UNIFY_KEY is required to create the disposable organization"
    assert ADMIN_KEY, "ORCHESTRA_ADMIN_KEY is required for admin assistant lookup"


def _preview_slug(url: str) -> str | None:
    """Return the preview slug encoded in a tagged service URL."""

    host = urlparse(url).hostname or ""
    if "---" in host:
        return host.split("---", 1)[0] or None
    if host.endswith("internal.example.com"):
        return host.split(".", 1)[0] or None
    return None


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _fallback_admin_orchestra_url() -> str | None:
    """Return one admin-route fallback when preview Orchestra admin endpoints fail."""

    override = os.getenv("TEST_ORCHESTRA_ADMIN_URL")
    if override:
        return override.rstrip("/")
    parsed = urlparse(ORCHESTRA_URL)
    host = parsed.hostname or ""
    if host.endswith("internal.example.com"):
        path = parsed.path.rstrip("/") or "/v0"
        return f"{parsed.scheme}://internal.example.com{path}"
    return None


def _read_field(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = payload.get(name)
        if value is not None:
            return value
    return None


def _unwrap_info(response: requests.Response) -> dict[str, Any]:
    payload = response.json()
    info = payload.get("info", payload)
    assert isinstance(info, dict), f"Expected object response, got: {payload}"
    return info


def _unwrap_info_list(response: requests.Response) -> list[dict[str, Any]]:
    payload = response.json()
    info = payload.get("info", payload)
    assert isinstance(info, list), f"Expected list response, got: {payload}"
    return info


def _fetch_user_basic_info(api_key: str) -> dict[str, Any]:
    response = requests.get(
        f"{ORCHESTRA_URL}/user/basic-info",
        headers=_auth_headers(api_key),
        timeout=30,
    )
    assert (
        response.status_code == 200
    ), f"user/basic-info lookup failed: {response.status_code} {response.text}"
    return _unwrap_info(response)


def _fetch_user_api_key(user_id: str) -> str:
    response = requests.get(
        f"{ORCHESTRA_URL}/admin/user/by-user-id",
        params={"user_id": user_id},
        headers=_auth_headers(ADMIN_KEY),
        timeout=30,
    )
    assert (
        response.status_code == 200
    ), f"admin user lookup failed: {response.status_code} {response.text}"
    api_key = response.json().get("api_key", "")
    assert api_key, f"admin user lookup missing api_key for user_id={user_id}"
    return str(api_key)


def _wait_for_personal_coordinator_assistant_id(
    user_id: str,
    timeout: int = 120,
) -> str:
    def _snapshot():
        response = requests.get(
            f"{ORCHESTRA_URL}/admin/assistant/user/{user_id}",
            headers=_auth_headers(ADMIN_KEY),
            timeout=30,
        )
        return {
            "status_code": response.status_code,
            "body": response.json() if response.content else response.text,
        }

    def _lookup():
        response = requests.get(
            f"{ORCHESTRA_URL}/admin/assistant/user/{user_id}",
            headers=_auth_headers(ADMIN_KEY),
            timeout=30,
        )
        if response.status_code != 200:
            return None
        info = response.json().get("info", [])
        assistant_rows = info if isinstance(info, list) else [info]
        for row in assistant_rows:
            if row.get("is_coordinator") is not True:
                continue
            if _read_field(row, "organization_id", "organizationId") is not None:
                continue
            if _read_field(row, "self_contact_id", "selfContactId") is None:
                continue
            if _read_field(row, "boss_contact_id", "bossContactId") is None:
                continue
            return str(_read_field(row, "agent_id", "agentId"))
        return None

    return poll_until(
        _lookup,
        timeout=timeout,
        interval=5,
        description=f"personal Coordinator assistant for owner user {user_id}",
        failure_snapshot=_snapshot,
    )


def _create_organization() -> dict[str, Any]:
    organization_name = f"coord-e2e-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    response = requests.post(
        f"{ORCHESTRA_URL}/organizations",
        json={"name": organization_name},
        headers=_auth_headers(UNIFY_KEY),
        timeout=90,
    )
    assert (
        response.status_code == 201
    ), f"Organization create failed: {response.status_code} {response.text}"
    info = _unwrap_info(response)
    organization_id = _read_field(info, "id", "organization_id", "organizationId")
    organization_api_key = _read_field(info, "api_key", "apiKey")
    assert organization_api_key, f"Organization create omitted api_key: {info}"
    owner_info = _fetch_user_basic_info(str(organization_api_key or ""))
    owner_user_id = _read_field(owner_info, "id", "user_id", "userId")
    assert owner_user_id, (
        "Organization owner lookup omitted user id: "
        f"org_info={info} basic_info={owner_info}"
    )
    owner_api_key = _fetch_user_api_key(str(owner_user_id))
    coordinator_id = _wait_for_personal_coordinator_assistant_id(str(owner_user_id))
    return {
        "name": organization_name,
        "organization_id": str(organization_id or ""),
        "owner_user_id": str(owner_user_id),
        "owner_api_key": owner_api_key,
        "coordinator_id": str(coordinator_id or ""),
        "api_key": str(organization_api_key or ""),
    }


def _delete_organization(organization_id: str, api_key: str) -> requests.Response:
    response: requests.Response | None = None
    for attempt in range(5):
        response = requests.delete(
            f"{ORCHESTRA_URL}/organizations/{organization_id}",
            headers=_auth_headers(api_key),
            timeout=90,
        )
        if response.status_code in (204, 404):
            return response
        if response.status_code >= 500 and attempt < 4:
            time.sleep(3)
            continue
        return response
    assert response is not None
    return response


def _ensure_organization_credits(
    organization_id: str,
    api_key: str,
    min_credits: float,
) -> None:
    response = requests.get(
        f"{ORCHESTRA_URL}/credits",
        headers=_auth_headers(api_key),
        timeout=30,
    )
    assert (
        response.status_code == 200
    ), f"Organization credits lookup failed: {response.status_code} {response.text}"
    balance = float(response.json().get("credits", 0))
    if balance >= min_credits:
        return

    top_up_amount = min_credits - balance
    recharge_payload = {
        "organization_id": int(organization_id),
        "quantity": top_up_amount,
        "type": "promo",
    }
    top_up_response = requests.post(
        f"{ORCHESTRA_URL}/admin/create_recharge",
        json=recharge_payload,
        headers=_auth_headers(ADMIN_KEY),
        timeout=30,
    )
    if top_up_response.status_code not in (200, 201):
        fallback_url = _fallback_admin_orchestra_url()
        if fallback_url and fallback_url.rstrip("/") != ORCHESTRA_URL.rstrip("/"):
            top_up_response = requests.post(
                f"{fallback_url}/admin/create_recharge",
                json=recharge_payload,
                headers=_auth_headers(ADMIN_KEY),
                timeout=30,
            )
    assert top_up_response.status_code in (200, 201), (
        "Organization credit top-up failed. "
        f"org_id={organization_id} balance={balance} "
        f"status={top_up_response.status_code} body={top_up_response.text}"
    )


def _fetch_admin_assistant(assistant_id: str) -> dict[str, Any]:
    response = requests.get(
        f"{ORCHESTRA_URL}/admin/assistant",
        params={"agent_id": assistant_id},
        headers=_auth_headers(ADMIN_KEY),
        timeout=30,
    )
    assert (
        response.status_code == 200
    ), f"Admin assistant lookup failed: {response.status_code} {response.text}"
    payload = response.json()
    info = payload.get("info")
    assert info is not None, f"Admin assistant lookup omitted info payload: {payload}"
    return info[0] if isinstance(info, list) else info


def _seed_transcript(coordinator_id: str, api_key: str, content: str) -> int:
    response = requests.post(
        f"{ORCHESTRA_URL}/assistant/{coordinator_id}/transcript-seed",
        json={"content": content},
        headers=_auth_headers(api_key),
        timeout=30,
    )
    if response.status_code == 409:
        detail = None
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        if detail == "coordinator_transcript_not_empty":
            _reset_coordinator(coordinator_id, api_key)
            response = requests.post(
                f"{ORCHESTRA_URL}/assistant/{coordinator_id}/transcript-seed",
                json={"content": content},
                headers=_auth_headers(api_key),
                timeout=30,
            )
    assert (
        response.status_code == 200
    ), f"Transcript seed failed: {response.status_code} {response.text}"
    info = _unwrap_info(response)
    log_event_id = _read_field(info, "log_event_id", "logEventId")
    assert isinstance(log_event_id, int), f"Missing log_event_id in response: {info}"
    return log_event_id


def _reset_coordinator(coordinator_id: str, api_key: str) -> None:
    response = requests.post(
        f"{ORCHESTRA_URL}/assistant/{coordinator_id}/reset",
        headers=_auth_headers(api_key),
        timeout=30,
    )
    assert (
        response.status_code == 200
    ), f"Coordinator reset failed: {response.status_code} {response.text}"


def _post_wakeup(coordinator_id: str) -> None:
    response = requests.post(
        f"{ADAPTERS_URL}/assistant/wakeup",
        data={"assistant_id": coordinator_id},
        headers=_auth_headers(ADMIN_KEY),
        timeout=30,
    )
    assert (
        response.status_code == 200
    ), f"Coordinator wakeup failed: {response.status_code} {response.text}"


def _send_runtime_message(assistant: dict[str, Any], body: str) -> None:
    response = requests.post(
        f"{ADAPTERS_URL}/unify/message",
        json={
            "assistant_id": str(assistant["assistant_id"]),
            "contact_id": int(assistant["boss_contact_id"]),
            "body": body,
        },
        headers=_auth_headers(ADMIN_KEY),
        timeout=30,
    )
    assert (
        response.status_code == 200
    ), f"Coordinator message failed: {response.status_code} {response.text}"


def _pull_reply_with_token(
    pubsub_subscriber,
    coordinator_id: str,
    token: str,
    *,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    def _matching_reply() -> dict[str, Any] | None:
        messages = pull_outbound_messages(pubsub_subscriber, coordinator_id, timeout=10)
        for message in messages:
            if token in json.dumps(message, sort_keys=True):
                return message
        return None

    return poll_until(
        _matching_reply,
        timeout=timeout_seconds,
        interval=5,
        description=f"Coordinator outbound reply containing {token}",
    )


def _ensure_coordinator_tools_ready(
    assistant: dict[str, Any],
    pubsub_subscriber,
    coordinator_id: str,
) -> None:
    """Ensure the live runtime can answer prompts before side-effect requests."""

    token = f"coord-ready-{uuid.uuid4().hex[:12]}"
    prompt = (
        "Please reply with this exact preview readiness token when your coordinator "
        f"tools are ready: {token}"
    )
    for attempt in range(1, COORDINATOR_RUNTIME_READY_ATTEMPTS + 1):
        pull_outbound_messages(pubsub_subscriber, coordinator_id, timeout=1)
        _send_runtime_message(assistant, prompt)
        try:
            _pull_reply_with_token(
                pubsub_subscriber,
                coordinator_id,
                token,
                timeout_seconds=COORDINATOR_RUNTIME_READY_REPLY_TIMEOUT_SECONDS,
            )
            return
        except TimeoutError:
            if attempt == COORDINATOR_RUNTIME_READY_ATTEMPTS:
                raise


def _list_admin_assistants_for_readback() -> list[dict[str, Any]]:
    response = requests.get(
        f"{ORCHESTRA_URL}/admin/assistant",
        params={
            "from_fields": (
                "agent_id,first_name,surname,organization_id,is_coordinator"
            ),
        },
        headers=_auth_headers(ADMIN_KEY),
        timeout=30,
    )
    assert (
        response.status_code == 200
    ), f"Admin assistant list failed: {response.status_code} {response.text}"
    return _unwrap_info_list(response)


def _find_assistant_by_name(
    first_name: str,
    surname: str,
) -> dict[str, Any] | None:
    for assistant in _list_admin_assistants_for_readback():
        if (
            _read_field(assistant, "first_name", "firstName") == first_name
            and _read_field(assistant, "surname", "last_name", "lastName") == surname
        ):
            return assistant
    return None


def _list_spaces(api_key: str) -> list[dict[str, Any]]:
    response = requests.get(
        f"{ORCHESTRA_URL}/spaces",
        headers=_auth_headers(api_key),
        timeout=30,
    )
    assert (
        response.status_code == 200
    ), f"Space list failed: {response.status_code} {response.text}"
    payload = response.json()
    assert isinstance(payload, list), f"Expected space list, got: {payload}"
    return payload


def _find_space_by_name(
    api_key: str,
    name: str,
) -> dict[str, Any] | None:
    for space in _list_spaces(api_key):
        if _read_field(space, "name") == name:
            return space
    return None


def _create_org_space(
    api_key: str,
    *,
    organization_id: str,
    name: str,
    description: str,
) -> dict[str, Any]:
    response = requests.post(
        f"{ORCHESTRA_URL}/spaces",
        json={
            "name": name,
            "description": description,
            "organization_id": int(organization_id),
        },
        headers=_auth_headers(api_key),
        timeout=30,
    )
    assert (
        response.status_code == 201
    ), f"Space create failed: {response.status_code} {response.text}"
    payload = response.json()
    assert isinstance(payload, dict), f"Expected created space payload, got: {payload}"
    return payload


def _add_space_member(
    api_key: str,
    *,
    space_id: str,
    assistant_id: str,
) -> None:
    response = requests.post(
        f"{ORCHESTRA_URL}/spaces/{space_id}/members",
        json={"assistant_id": int(assistant_id)},
        headers=_auth_headers(api_key),
        timeout=30,
    )
    assert response.status_code in {
        200,
        201,
    }, f"Add member failed: {response.status_code} {response.text}"


def _list_space_members(api_key: str, space_id: str) -> list[dict[str, Any]]:
    response = requests.get(
        f"{ORCHESTRA_URL}/spaces/{space_id}/members",
        headers=_auth_headers(api_key),
        timeout=30,
    )
    assert (
        response.status_code == 200
    ), f"Space member list failed: {response.status_code} {response.text}"
    payload = response.json()
    assert isinstance(payload, list), f"Expected space member list, got: {payload}"
    return payload


def _list_assistant_spaces(api_key: str, assistant_id: str) -> list[dict[str, Any]]:
    response = requests.get(
        f"{ORCHESTRA_URL}/assistants/{assistant_id}/spaces",
        headers=_auth_headers(api_key),
        timeout=30,
    )
    assert (
        response.status_code == 200
    ), f"Assistant space list failed: {response.status_code} {response.text}"
    payload = response.json()
    assert isinstance(payload, list), f"Expected assistant space list, got: {payload}"
    return payload


def _space_has_member(api_key: str, space_id: str, assistant_id: str) -> bool:
    return any(
        str(_read_field(member, "assistant_id", "assistantId")) == assistant_id
        for member in _list_space_members(api_key, space_id)
    )


def _assistant_has_space(api_key: str, assistant_id: str, space_id: str) -> bool:
    return any(
        str(_read_field(space, "space_id", "spaceId")) == space_id
        for space in _list_assistant_spaces(api_key, assistant_id)
    )


def _list_context_logs(
    api_key: str,
    *,
    context: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    response = requests.get(
        f"{ORCHESTRA_URL}/logs",
        params={
            "project_name": ASSISTANTS_PROJECT_NAME,
            "context": context,
            "limit": limit,
        },
        headers=_auth_headers(api_key),
        timeout=30,
    )
    if response.status_code == 404:
        return []
    assert (
        response.status_code == 200
    ), f"Context log list failed: {response.status_code} {response.text}"
    payload = response.json()
    logs = payload.get("logs", [])
    assert isinstance(logs, list), f"Expected logs list, got: {payload}"
    return logs


def _find_space_guidance_log_with_token(
    api_key: str,
    *,
    space_id: str,
    token: str,
) -> dict[str, Any] | None:
    context = f"Spaces/{space_id}/Guidance"
    for log_row in _list_context_logs(api_key, context=context, limit=50):
        entries = log_row.get("entries") or {}
        if token not in json.dumps(entries, sort_keys=True):
            continue
        return log_row
    return None


def _side_effect_failure_snapshot(
    *,
    messages: list[dict[str, Any]],
    prompt: str,
    nudge_prompt: str,
) -> dict[str, Any]:
    return {
        "prompt": prompt,
        "nudge_prompt": nudge_prompt,
        "latest_outbound_messages": messages[-5:],
    }


def _send_and_poll_for_side_effect(
    *,
    assistant: dict[str, Any],
    pubsub_subscriber,
    coordinator_id: str,
    prompt: str,
    nudge_prompt: str,
    condition: Callable[[], Any],
    description: str,
) -> Any:
    latest_messages: list[dict[str, Any]] = []
    failure_snapshot = lambda: _side_effect_failure_snapshot(
        messages=latest_messages,
        prompt=prompt,
        nudge_prompt=nudge_prompt,
    )
    pull_outbound_messages(pubsub_subscriber, coordinator_id, timeout=1)
    _send_runtime_message(assistant, prompt)

    def _check_condition() -> Any:
        result = condition()
        if result:
            return result
        latest_messages.extend(
            pull_outbound_messages(pubsub_subscriber, coordinator_id, timeout=1),
        )
        if len(latest_messages) > 20:
            del latest_messages[:-20]
        return None

    def _poll(timeout_seconds: int, description_text: str) -> Any:
        return poll_until(
            _check_condition,
            timeout=timeout_seconds,
            interval=5,
            description=description_text,
            failure_snapshot=failure_snapshot,
        )

    try:
        return _poll(
            COORDINATOR_SIDE_EFFECT_TIMEOUT_SECONDS,
            description,
        )
    except TimeoutError:
        _send_runtime_message(assistant, nudge_prompt)
        return _poll(
            COORDINATOR_SIDE_EFFECT_NUDGE_TIMEOUT_SECONDS,
            f"{description} after confirmation nudge",
        )


@pytest.mark.skipif(
    not RUN_COORDINATOR_E2E,
    reason="TEST_RUN_COORDINATOR_E2E not set",
)
@pytest.mark.timeout(1200)
def test_coordinator_contract_end_to_end(batch_api, core_api, pubsub_subscriber):
    """Exercise the deployed Coordinator contract through the public services."""

    _require_preview_routing()

    organization: dict[str, Any] | None = None
    coordinator_id: str | None = None
    organization_deleted = False

    try:
        organization = _create_organization()
        coordinator_id = organization["coordinator_id"]
        organization_api_key = organization["api_key"]
        coordinator_runtime_api_key = organization_api_key
        coordinator_owner_api_key = organization["owner_api_key"]
        assert organization[
            "organization_id"
        ], "Organization create response omitted id"
        assert (
            organization_api_key
        ), "Organization create response omitted owner API key"
        assert coordinator_id, (
            "Organization create response omitted Coordinator id; "
            f"org_id={organization['organization_id']}"
        )

        admin_record = _fetch_admin_assistant(coordinator_id)
        assert admin_record["is_coordinator"] is True
        assert _read_field(admin_record, "organization_id", "organizationId") is None
        assistant = _admin_record_to_data(admin_record)

        opener_id = _seed_transcript(
            coordinator_id,
            coordinator_runtime_api_key,
            "Welcome. I can help shape your Unify team when you are ready.",
        )
        duplicate_opener_id = _seed_transcript(
            coordinator_id,
            coordinator_runtime_api_key,
            "A different opener should not create another transcript row.",
        )
        assert isinstance(duplicate_opener_id, int)
        if duplicate_opener_id != opener_id:
            assert duplicate_opener_id > opener_id

        _post_wakeup(coordinator_id)
        ready_session = wait_for_assistant_container_ready(
            coordinator_id,
            batch_api=batch_api,
            core_api=core_api,
            timeout=420,
            interval=10,
        )
        secret_name = str(
            (ready_session.get("spec") or {}).get("startupSecretRef", "") or "",
        )
        assert secret_name, f"Ready session omitted startupSecretRef: {ready_session}"
        bootstrap_payload = read_bootstrap_secret(core_api, NAMESPACE, secret_name)
        assert bootstrap_payload["is_coordinator"] is True
        assert bootstrap_payload["org_id"] is None

        _ensure_coordinator_tools_ready(
            assistant,
            pubsub_subscriber,
            coordinator_id,
        )

        _reset_coordinator(coordinator_id, coordinator_runtime_api_key)
        reseeded_id = _seed_transcript(
            coordinator_id,
            coordinator_runtime_api_key,
            "The Coordinator reset succeeded and this opener starts the next run.",
        )
        assert reseeded_id != opener_id

        delete_response = requests.delete(
            f"{ORCHESTRA_URL}/assistant/{coordinator_id}",
            headers=_auth_headers(coordinator_owner_api_key),
            timeout=30,
        )
        assert delete_response.status_code == 409, (
            "Direct Coordinator deletion should be blocked: "
            f"{delete_response.status_code} {delete_response.text}"
        )
        assert delete_response.json().get("detail") == "cannot_delete_coordinator"

        stop_assistant_runtime(
            coordinator_id,
            batch_api=batch_api,
            timeout=300,
            strict=True,
            context="coordinator-e2e",
        )
        coordinator_id = None
        delete_org_response = _delete_organization(
            organization["organization_id"],
            organization_api_key,
        )
        assert delete_org_response.status_code == 204, (
            "Disposable organization cleanup failed. "
            f"org_id={organization['organization_id']} "
            f"coordinator_id={coordinator_id} "
            f"status={delete_org_response.status_code} body={delete_org_response.text}"
        )
        organization_deleted = True
    finally:
        if coordinator_id:
            stop_assistant_runtime(
                coordinator_id,
                batch_api=batch_api,
                timeout=180,
                context="coordinator-e2e-finally",
            )
        if organization and not organization_deleted:
            cleanup_response = _delete_organization(
                organization["organization_id"],
                organization["api_key"],
            )
            if cleanup_response.status_code not in (204, 404):
                print(
                    "Coordinator e2e cleanup left a disposable organization behind: "
                    f"org_id={organization['organization_id']} "
                    f"coordinator_id={organization['coordinator_id']} "
                    f"status={cleanup_response.status_code} body={cleanup_response.text}",
                )


@pytest.mark.skipif(
    not RUN_COORDINATOR_E2E,
    reason="TEST_RUN_COORDINATOR_E2E not set",
)
@pytest.mark.timeout(1800)
def test_coordinator_builds_colleague_and_space_end_to_end(
    batch_api,
    core_api,
    pubsub_subscriber,
):
    """Ask the live Coordinator to persist a colleague, space, and membership."""

    _require_preview_routing()

    organization: dict[str, Any] | None = None
    coordinator_id: str | None = None
    colleague_id: str | None = None
    organization_deleted = False

    try:
        organization = _create_organization()
        organization_id = organization["organization_id"]
        coordinator_id = organization["coordinator_id"]
        organization_api_key = organization["api_key"]
        coordinator_runtime_api_key = organization_api_key
        assert organization_id, "Organization create response omitted id"
        assert (
            organization_api_key
        ), "Organization create response omitted owner API key"
        assert coordinator_id, (
            "Organization create response omitted Coordinator id; "
            f"org_id={organization_id}"
        )
        _ensure_organization_credits(
            organization_id,
            organization_api_key,
            min_credits=25.0,
        )

        admin_record = _fetch_admin_assistant(coordinator_id)
        assert admin_record["is_coordinator"] is True
        assert _read_field(admin_record, "organization_id", "organizationId") is None
        assistant = _admin_record_to_data(admin_record)

        _seed_transcript(
            coordinator_id,
            coordinator_runtime_api_key,
            "We are ready to set up this organization's first colleague and workspace.",
        )
        _post_wakeup(coordinator_id)
        ready_session = wait_for_assistant_container_ready(
            coordinator_id,
            batch_api=batch_api,
            core_api=core_api,
            timeout=420,
            interval=10,
        )
        secret_name = str(
            (ready_session.get("spec") or {}).get("startupSecretRef", "") or "",
        )
        assert secret_name, f"Ready session omitted startupSecretRef: {ready_session}"
        bootstrap_payload = read_bootstrap_secret(core_api, NAMESPACE, secret_name)
        assert bootstrap_payload["is_coordinator"] is True
        assert bootstrap_payload["org_id"] is None
        _ensure_coordinator_tools_ready(
            assistant,
            pubsub_subscriber,
            coordinator_id,
        )

        implicit_org_named_spaces = [
            space
            for space in _list_assistant_spaces(organization_api_key, coordinator_id)
            if (
                str(_read_field(space, "organization_id", "organizationId"))
                == organization_id
                and _read_field(space, "name") == organization["name"]
            )
        ]
        assert not implicit_org_named_spaces, (
            "Coordinator should not auto-join an implicit org-wide space. "
            f"coordinator_id={coordinator_id} org_id={organization_id}"
        )

        token = uuid.uuid4().hex[:8]
        colleague_first_name = f"Scenario{token}"
        colleague_surname = "Colleague"
        colleague_full_name = f"{colleague_first_name} {colleague_surname}"
        space_name = f"Scenario Space {token}"
        space_description = (
            f"Shared coordination workspace for preview scenario {token}."
        )

        colleague = _send_and_poll_for_side_effect(
            assistant=assistant,
            pubsub_subscriber=pubsub_subscriber,
            coordinator_id=coordinator_id,
            prompt=(
                "Call your list_accessible_organizations tool, resolve the target "
                f"org id as {organization_id}, and then call create_assistant with "
                f"organization_id {organization_id}. Use first_name "
                f"{colleague_first_name} and surname {colleague_surname}. Use config "
                "values age 30, nationality United States, timezone "
                "America/New_York, job_title Preview E2E Colleague, and about "
                "'Created by the Coordinator preview scenario test.' No extra "
                "confirmation is needed."
            ),
            nudge_prompt=(
                f"Call create_assistant now with organization_id {organization_id}, "
                f"first_name {colleague_first_name}, surname {colleague_surname}, "
                "and the same config from my previous message."
            ),
            condition=lambda: _find_assistant_by_name(
                colleague_first_name,
                colleague_surname,
            ),
            description=f"persisted colleague {colleague_full_name}",
        )
        colleague_id = str(_read_field(colleague, "agent_id", "agentId"))
        assert colleague_id, f"Created colleague omitted agent_id: {colleague}"
        assert colleague_id != coordinator_id
        assert not bool(colleague.get("is_coordinator", False))
        colleague_org_id = _read_field(colleague, "organization_id", "organizationId")
        if colleague_org_id is not None:
            assert str(colleague_org_id) == organization_id

        space = _send_and_poll_for_side_effect(
            assistant=assistant,
            pubsub_subscriber=pubsub_subscriber,
            coordinator_id=coordinator_id,
            prompt=(
                "Call your list_accessible_organizations tool, resolve the target org "
                f"id as {organization_id}, and then call create_space with that "
                f"organization_id. Use name {space_name} and description "
                f"{space_description}. No extra confirmation is needed."
            ),
            nudge_prompt=(
                f"Call create_space now with organization_id {organization_id}, "
                f"name {space_name}, and description {space_description}."
            ),
            condition=lambda: _find_space_by_name(
                organization_api_key,
                space_name,
            ),
            description=f"persisted team space {space_name}",
        )
        space_id = str(_read_field(space, "space_id", "spaceId"))
        assert space_id, f"Created space omitted space_id: {space}"
        space_org_id = _read_field(space, "organization_id", "organizationId")
        assert (
            space_org_id is not None
        ), f"Created space omitted organization_id: {space}"
        assert str(space_org_id) == organization_id
        space_membership_api_key = organization_api_key
        if "kind" in space:
            assert space["kind"] == "team"

        _send_and_poll_for_side_effect(
            assistant=assistant,
            pubsub_subscriber=pubsub_subscriber,
            coordinator_id=coordinator_id,
            prompt=(
                "Call your add_space_member tool now to add colleague "
                f"{colleague_full_name} with assistant_id {colleague_id} to the "
                f"team workspace {space_name} with space_id {space_id}. No extra "
                f"confirmation is needed. Include organization_id {organization_id}."
            ),
            nudge_prompt=(
                f"Call add_space_member now with space_id {space_id} and "
                f"assistant_id {colleague_id} and organization_id {organization_id}."
            ),
            condition=lambda: _space_has_member(
                space_membership_api_key,
                space_id,
                colleague_id,
            ),
            description=(
                f"membership for colleague {colleague_id} in team space {space_id}"
            ),
        )
        assert _assistant_has_space(space_membership_api_key, colleague_id, space_id)

        stop_assistant_runtime(
            colleague_id,
            batch_api=batch_api,
            timeout=180,
            context="coordinator-scenario-colleague",
        )
        colleague_id = None
        stop_assistant_runtime(
            coordinator_id,
            batch_api=batch_api,
            timeout=300,
            strict=True,
            context="coordinator-scenario",
        )
        coordinator_id = None
        delete_org_response = _delete_organization(
            organization_id,
            organization_api_key,
        )
        assert delete_org_response.status_code == 204, (
            "Disposable organization cleanup failed. "
            f"org_id={organization_id} coordinator_id={coordinator_id} "
            f"colleague_id={colleague_id} space_id={space_id} "
            f"status={delete_org_response.status_code} body={delete_org_response.text}"
        )
        organization_deleted = True
    finally:
        if colleague_id:
            stop_assistant_runtime(
                colleague_id,
                batch_api=batch_api,
                timeout=180,
                context="coordinator-scenario-colleague-finally",
            )
        if coordinator_id:
            stop_assistant_runtime(
                coordinator_id,
                batch_api=batch_api,
                timeout=180,
                context="coordinator-scenario-finally",
            )
        if organization and not organization_deleted:
            cleanup_response = _delete_organization(
                organization["organization_id"],
                organization["api_key"],
            )
            if cleanup_response.status_code not in (204, 404):
                print(
                    "Coordinator scenario cleanup left a disposable organization behind: "
                    f"org_id={organization['organization_id']} "
                    f"coordinator_id={organization['coordinator_id']} "
                    f"colleague_id={colleague_id} "
                    f"status={cleanup_response.status_code} body={cleanup_response.text}",
                )


@pytest.mark.skipif(
    not RUN_COORDINATOR_E2E,
    reason="TEST_RUN_COORDINATOR_E2E not set",
)
@pytest.mark.timeout(1800)
def test_coordinator_act_writes_to_shared_space_end_to_end(
    batch_api,
    core_api,
    pubsub_subscriber,
):
    """Ask the live Coordinator to run act and persist output into one shared space."""

    _require_preview_routing()

    organization: dict[str, Any] | None = None
    coordinator_id: str | None = None
    organization_deleted = False

    try:
        organization = _create_organization()
        organization_id = organization["organization_id"]
        coordinator_id = organization["coordinator_id"]
        organization_api_key = organization["api_key"]
        coordinator_runtime_api_key = organization_api_key
        coordinator_owner_api_key = organization["owner_api_key"]
        assert organization_id, "Organization create response omitted id"
        assert (
            organization_api_key
        ), "Organization create response omitted owner API key"
        assert coordinator_id, (
            "Organization create response omitted Coordinator id; "
            f"org_id={organization_id}"
        )
        _ensure_organization_credits(
            organization_id,
            organization_api_key,
            min_credits=25.0,
        )

        admin_record = _fetch_admin_assistant(coordinator_id)
        assert admin_record["is_coordinator"] is True
        assert _read_field(admin_record, "organization_id", "organizationId") is None
        assistant = _admin_record_to_data(admin_record)

        _seed_transcript(
            coordinator_id,
            coordinator_runtime_api_key,
            "Route setup instructions to a shared workspace when asked.",
        )
        _post_wakeup(coordinator_id)
        ready_session = wait_for_assistant_container_ready(
            coordinator_id,
            batch_api=batch_api,
            core_api=core_api,
            timeout=420,
            interval=10,
        )
        secret_name = str(
            (ready_session.get("spec") or {}).get("startupSecretRef", "") or "",
        )
        assert secret_name, f"Ready session omitted startupSecretRef: {ready_session}"
        bootstrap_payload = read_bootstrap_secret(core_api, NAMESPACE, secret_name)
        assert bootstrap_payload["is_coordinator"] is True
        assert bootstrap_payload["org_id"] is None
        _ensure_coordinator_tools_ready(
            assistant,
            pubsub_subscriber,
            coordinator_id,
        )

        setup_space_name = f"Coordinator Act Space {uuid.uuid4().hex[:8]}"
        setup_space = _create_org_space(
            organization_api_key,
            organization_id=organization_id,
            name=setup_space_name,
            description=(
                "Explicit workspace for Coordinator act integration validation."
            ),
        )
        space_id = str(_read_field(setup_space, "space_id", "spaceId") or "")
        assert space_id, f"Created setup space omitted id: {setup_space}"
        _add_space_member(
            organization_api_key,
            space_id=space_id,
            assistant_id=coordinator_id,
        )
        assert _space_has_member(
            organization_api_key,
            space_id,
            coordinator_id,
        )

        token = f"coord-act-space-{uuid.uuid4().hex[:12]}"
        guidance_log = _send_and_poll_for_side_effect(
            assistant=assistant,
            pubsub_subscriber=pubsub_subscriber,
            coordinator_id=coordinator_id,
            prompt=(
                "Call your act tool now. Use a query that writes exactly one "
                f"Guidance entry containing token {token} to destination "
                f"space:{space_id}. Include the destination explicitly and do not "
                "write to any colleague-owned context. No extra confirmation is needed."
            ),
            nudge_prompt=(
                "Call act now and write one Guidance entry containing token "
                f"{token} to destination space:{space_id}."
            ),
            condition=lambda: (
                _find_space_guidance_log_with_token(
                    organization_api_key,
                    space_id=space_id,
                    token=token,
                )
                or _find_space_guidance_log_with_token(
                    coordinator_owner_api_key,
                    space_id=space_id,
                    token=token,
                )
            ),
            description=(
                f"Guidance log containing token {token} in shared space {space_id}"
            ),
        )
        entries = guidance_log.get("entries") or {}
        assert token in json.dumps(entries, sort_keys=True)
        destination = str(entries.get("destination") or "")
        if destination:
            assert destination == f"space:{space_id}"
        assert str(entries.get("authoring_assistant_id") or "") == coordinator_id

        stop_assistant_runtime(
            coordinator_id,
            batch_api=batch_api,
            timeout=300,
            strict=True,
            context="coordinator-act-space",
        )
        coordinator_id = None
        delete_org_response = _delete_organization(
            organization_id,
            organization_api_key,
        )
        assert delete_org_response.status_code == 204, (
            "Disposable organization cleanup failed. "
            f"org_id={organization_id} coordinator_id={coordinator_id} "
            f"space_id={space_id} status={delete_org_response.status_code} "
            f"body={delete_org_response.text}"
        )
        organization_deleted = True
    finally:
        if coordinator_id:
            stop_assistant_runtime(
                coordinator_id,
                batch_api=batch_api,
                timeout=180,
                context="coordinator-act-space-finally",
            )
        if organization and not organization_deleted:
            cleanup_response = _delete_organization(
                organization["organization_id"],
                organization["api_key"],
            )
            if cleanup_response.status_code not in (204, 404):
                print(
                    "Coordinator act-space cleanup left a disposable organization behind: "
                    f"org_id={organization['organization_id']} "
                    f"coordinator_id={organization['coordinator_id']} "
                    f"status={cleanup_response.status_code} body={cleanup_response.text}",
                )
