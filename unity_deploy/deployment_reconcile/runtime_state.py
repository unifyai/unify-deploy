"""Runtime-state materialization for deploy-time reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from unity_deploy.assistant_deployments.clients import ResolvedAssistantDeployment


@dataclass(frozen=True)
class RuntimeIdentity:
    """Concrete assistant identity for runtime-state materialization."""

    assistant_id: str
    user_id: str
    org_id: int | None = None
    team_ids: tuple[int, ...] = ()
    client_name: str | None = None
    deployment: str | None = None
    api_key: str | None = None

    @property
    def target_key(self) -> str:
        return f"assistant/{self.assistant_id}"


@dataclass(frozen=True)
class RuntimeStateResult:
    """Summary of deploy-time runtime-state materialization."""

    identity: RuntimeIdentity
    revision: str
    seed_changed: bool = False
    custom_changed: bool = False


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items())}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _jsonable(model_dump(mode="json"))
    if hasattr(value, "__dict__"):
        return _jsonable(value.__dict__)
    return value


def _hash_payload(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(_jsonable(payload), sort_keys=True, default=str).encode("utf-8"),
    ).hexdigest()


def _hash_path(path: Path) -> str:
    """Return a deterministic digest for a file or directory tree."""

    path = Path(path)
    if not path.exists():
        return hashlib.sha256(f"missing:{path}".encode("utf-8")).hexdigest()
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()

    parts: list[str] = []
    for child in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = child.relative_to(path).as_posix()
        digest = hashlib.sha256(child.read_bytes()).hexdigest()
        parts.append(f"{rel}:{digest}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def compute_runtime_state_fingerprint(
    resolved: ResolvedAssistantDeployment,
) -> str:
    """Compute a deterministic fingerprint for side-effectful runtime state."""

    payload = {
        "contacts": resolved.contacts,
        "guidance": resolved.guidance,
        "knowledge": resolved.knowledge,
        "blacklist": resolved.blacklist,
        "secrets": resolved.secrets,
        "integrations": resolved.integrations,
        "mcp_configs": resolved.mcp_configs,
        "url_mappings": resolved.url_mappings,
        "function_dirs": [
            {"path": str(path), "digest": _hash_path(path)}
            for path in resolved.function_dirs
        ],
        "venv_dirs": [
            {"path": str(path), "digest": _hash_path(path)}
            for path in resolved.venv_dirs
        ],
    }
    return _hash_payload(payload)


def activate_runtime_context(identity: RuntimeIdentity) -> None:
    """Activate Unity runtime state for deploy-time materialization."""

    if not identity.user_id:
        raise ValueError("Runtime state materialization requires a concrete user_id")
    if not identity.assistant_id:
        raise ValueError(
            "Runtime state materialization requires a concrete assistant_id",
        )

    if identity.api_key:
        os.environ["UNIFY_KEY"] = identity.api_key

    from unity.session_details import SESSION_DETAILS
    from unity_deploy.infra.workers.worker_utils import activate_unify_context

    SESSION_DETAILS.populate(
        agent_id=(
            int(identity.assistant_id) if str(identity.assistant_id).isdigit() else None
        ),
        user_id=identity.user_id,
        org_id=identity.org_id,
        team_ids=list(identity.team_ids),
    )
    SESSION_DETAILS.export_to_env()
    activate_unify_context(
        user_id=identity.user_id,
        assistant_id=identity.assistant_id,
    )


def materialize_runtime_state(
    resolved: ResolvedAssistantDeployment,
    identity: RuntimeIdentity,
    *,
    revision: str | None = None,
) -> RuntimeStateResult:
    """Apply side-effectful runtime state for a resolved assistant deployment."""

    from unity.function_manager.custom_functions import (
        collect_functions_from_directories,
        collect_venvs_from_directories,
    )
    from unity.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.seed_sync import sync_all_seed_data

    revision = revision or compute_runtime_state_fingerprint(resolved)
    seed_changed = sync_all_seed_data(resolved)
    custom_changed = False
    if resolved.function_dirs or resolved.venv_dirs:
        source_fns = collect_functions_from_directories(resolved.function_dirs)
        source_venvs = collect_venvs_from_directories(resolved.venv_dirs)
        if source_fns or source_venvs:
            fm = ManagerRegistry.get_function_manager()
            custom_changed = fm.sync_custom(
                source_functions=source_fns,
                source_venvs=source_venvs,
            )

    return RuntimeStateResult(
        identity=identity,
        revision=revision,
        seed_changed=seed_changed,
        custom_changed=custom_changed,
    )
