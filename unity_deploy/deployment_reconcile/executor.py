"""Executor for deploy-time reconciliation work items."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import threading
from typing import Any

from unity_deploy.deployment_reconcile.types import (
    DeploymentWorkItem,
    DeploymentWorkResult,
)

_RUNTIME_STATE_LOCK = threading.Lock()


class AssistantNotFoundError(RuntimeError):
    """Raised when an assistant-scoped deployment target no longer exists."""


def _resolve_runtime_identity(item: DeploymentWorkItem):
    """Resolve explicit assistant identity for runtime repair/prewarm runs."""

    from unity_deploy.runtime_reconcile import RuntimeIdentity

    info = _fetch_assistant_info(item.target.assistant_id)

    user_id = (
        info.get("user_id")
        or info.get("owner_user_id")
        or (info.get("user") or {}).get("id")
    )
    if not isinstance(user_id, str) or not user_id:
        raise RuntimeError(
            f"Assistant {item.target.assistant_id} metadata has no user_id",
        )

    api_key = info.get("api_key")
    if not isinstance(api_key, str) or not api_key:
        raise RuntimeError(
            f"Assistant {item.target.assistant_id} metadata has no api_key",
        )

    org_id = info.get("org_id") or info.get("organization_id")
    try:
        org_id = int(org_id) if org_id is not None else None
    except (TypeError, ValueError):
        org_id = None
    raw_team_ids = info.get("team_ids") or info.get("teams") or []
    team_ids: list[int] = []
    if isinstance(raw_team_ids, list):
        for raw in raw_team_ids:
            if isinstance(raw, dict):
                raw = raw.get("id")
            try:
                team_ids.append(int(raw))
            except (TypeError, ValueError):
                continue
    return RuntimeIdentity(
        assistant_id=item.target.assistant_id,
        user_id=user_id,
        org_id=org_id,
        team_ids=tuple(team_ids),
        client_name=item.target.client_name,
        deployment=item.target.deployment,
        api_key=api_key,
    )


def _fetch_assistant_info(assistant_id: str) -> dict[str, Any]:
    """Fetch assistant metadata from Orchestra admin API."""

    import httpx
    from unify.settings import SETTINGS

    orchestra_url = os.environ.get("ORCHESTRA_URL") or SETTINGS.ORCHESTRA_URL
    admin_key = os.environ.get("ORCHESTRA_ADMIN_KEY") or (
        SETTINGS.ORCHESTRA_ADMIN_KEY.get_secret_value()
        if hasattr(SETTINGS.ORCHESTRA_ADMIN_KEY, "get_secret_value")
        else SETTINGS.ORCHESTRA_ADMIN_KEY
    )
    if not orchestra_url or not admin_key:
        raise RuntimeError("ORCHESTRA_URL and ORCHESTRA_ADMIN_KEY are required")

    response = httpx.get(
        f"{orchestra_url.rstrip('/')}/admin/assistant",
        params={"agent_id": assistant_id},
        headers={"Authorization": f"Bearer {admin_key}"},
        timeout=30.0,
    )
    if response.status_code == 404:
        raise AssistantNotFoundError(f"No assistant found for agent_id={assistant_id}")
    response.raise_for_status()
    body = response.json()
    assistants = body.get("info") if isinstance(body, dict) else None
    if not isinstance(assistants, list) or not assistants:
        raise AssistantNotFoundError(f"No assistant found for agent_id={assistant_id}")
    if not isinstance(assistants[0], dict):
        raise RuntimeError(f"Unexpected assistant payload for {assistant_id}")
    return assistants[0]


def apply_work_item(item: DeploymentWorkItem) -> DeploymentWorkResult:
    """Apply one deployment work item."""

    try:
        if item.plane == "control-plane":
            from unity_deploy.deployment_reconcile.control_plane import apply_operations

            if not item.target.control_plane_operations:
                return DeploymentWorkResult(
                    item=item,
                    status="skipped-current",
                    message="no control-plane operations",
                )
            responses = apply_operations(list(item.target.control_plane_operations))
            if responses and all(
                response.get("status") == "skipped-missing" for response in responses
            ):
                return DeploymentWorkResult(
                    item=item,
                    status="skipped-missing",
                    message="assistant target is missing",
                )
            return DeploymentWorkResult(
                item=item,
                status="applied",
                message=f"applied {len(item.target.control_plane_operations)} operation(s)",
            )

        from unity_deploy.runtime_reconcile import (
            activate_runtime_context,
            materialize_runtime_state,
        )

        with _RUNTIME_STATE_LOCK:
            identity = _resolve_runtime_identity(item)
            activate_runtime_context(identity)
            result = materialize_runtime_state(
                item.target.resolved,
                identity,
                revision=item.revision,
            )
        return DeploymentWorkResult(
            item=item,
            status="applied",
            message=(
                f"seed_changed={result.seed_changed} "
                f"custom_changed={result.custom_changed}"
            ),
        )
    except AssistantNotFoundError as exc:
        if item.target.missing_ok:
            return DeploymentWorkResult(
                item=item,
                status="skipped-missing",
                message=str(exc),
            )
        return DeploymentWorkResult(
            item=item,
            status="failed",
            error=str(exc),
        )
    except Exception as exc:
        return DeploymentWorkResult(
            item=item,
            status="failed",
            error=str(exc),
        )


def execute_work_items(
    items: list[DeploymentWorkItem],
    *,
    apply: bool,
    concurrency: int = 8,
    fail_fast: bool = False,
) -> list[DeploymentWorkResult]:
    """Execute deploy-time work items with bounded parallelism."""

    if not apply:
        return [
            DeploymentWorkResult(item=item, status="planned", message="dry-run")
            for item in items
        ]
    if not items:
        return []

    max_workers = max(1, concurrency)
    results: list[DeploymentWorkResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_item = {
            executor.submit(apply_work_item, item): item for item in items
        }
        for future in as_completed(future_to_item):
            result = future.result()
            results.append(result)
            if fail_fast and result.status == "failed":
                for pending in future_to_item:
                    pending.cancel()
                break
    return results


def format_work_item(item: DeploymentWorkItem) -> str:
    return (
        f"plane={item.plane} target={item.target_key} "
        f"revision={item.revision[:16]} idempotency_key={item.idempotency_key}"
    )


def format_work_result(result: DeploymentWorkResult) -> str:
    suffix = result.error or result.message
    return f"{result.status} {format_work_item(result.item)} {suffix}".rstrip()
