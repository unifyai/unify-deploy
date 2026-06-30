"""Assistant-scoped runtime state materialization."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
from time import perf_counter
from typing import Any

from unity_deploy.assistant_deployments.clients import ResolvedAssistantDeployment
from unity_deploy.runtime_reconcile.context import RuntimeIdentity
from unity_deploy.runtime_reconcile.status import RuntimeReconcileStatusHandle
from unity_deploy.timing import log_startup_timing

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeStateResult:
    """Summary of runtime-state materialization."""

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


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    """Preserve path order while removing duplicates."""

    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        resolved = str(Path(path).resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(Path(path))
    return result


def _enabled_integration_source_dirs() -> tuple[list[Path], list[Path]] | None:
    """Return enabled integration function/venv dirs, or None if unknown.

    ``FunctionManager.sync_custom`` treats its input as the authoritative set of
    source-defined custom functions. Runtime reconciliation therefore needs to
    include enabled integration packages in the same set before deleting stale
    deployment functions. If integration discovery cannot be read, return
    ``None`` so callers can avoid destructive empty syncs based on incomplete
    information.
    """

    try:
        from unify.integration_status import get_enabled_integrations
    except Exception:
        logger.warning(
            "Runtime reconcile could not import integration status; "
            "custom function cleanup will use deployment dirs only",
            exc_info=True,
        )
        return None

    try:
        enabled = get_enabled_integrations()
    except Exception:
        logger.warning(
            "Runtime reconcile could not read enabled integrations; "
            "skipping destructive empty custom function cleanup",
            exc_info=True,
        )
        return None

    function_dirs: list[Path] = []
    venv_dirs: list[Path] = []
    for package in enabled.values():
        function_dir = package.get("function_dir")
        if function_dir is not None:
            function_dirs.append(Path(function_dir))

        root_dir = package.get("root_dir")
        if root_dir is not None:
            venv_dir = Path(root_dir) / "venvs"
            if venv_dir.is_dir():
                venv_dirs.append(venv_dir)

    return _dedupe_paths(function_dirs), _dedupe_paths(venv_dirs)


def materialize_runtime_state(
    resolved: ResolvedAssistantDeployment,
    identity: RuntimeIdentity,
    *,
    revision: str | None = None,
    status: RuntimeReconcileStatusHandle | None = None,
) -> RuntimeStateResult:
    """Apply side-effectful runtime state for a resolved assistant deployment."""

    from unify.function_manager.custom_functions import (
        collect_functions_from_directories,
        collect_venvs_from_directories,
    )
    from unify.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.seed_sync import sync_all_seed_data

    revision = revision or compute_runtime_state_fingerprint(resolved)
    logger.info(
        "Runtime reconcile phase starting: assistant=%s phase=syncing_seed_data revision=%s",
        identity.assistant_id,
        revision[:16],
    )
    if status is not None:
        status.update(
            phase="syncing_seed_data",
            message=(
                "Preparing deployment-defined contacts, guidance, knowledge, "
                "secrets, and blacklist."
            ),
            blocking_resources=("contacts", "guidance", "knowledge", "secrets"),
            resources={
                "contacts": "syncing",
                "guidance": "syncing",
                "knowledge": "syncing",
                "secrets": "syncing",
                "functions": "pending",
            },
            data_freshness="partial",
        )
    seed_start = perf_counter()
    seed_changed = sync_all_seed_data(resolved)
    logger.info(
        "Runtime reconcile phase completed: assistant=%s phase=syncing_seed_data duration=%.2fs seed_changed=%s",
        identity.assistant_id,
        perf_counter() - seed_start,
        seed_changed,
    )

    function_resources = {
        "contacts": "ready",
        "guidance": "ready",
        "knowledge": "ready",
        "secrets": "ready",
        "functions": "syncing",
    }
    integration_source_dirs = _enabled_integration_source_dirs()
    integration_function_dirs: list[Path] = []
    integration_venv_dirs: list[Path] = []
    can_sync_custom = integration_source_dirs is not None
    if can_sync_custom:
        integration_function_dirs, integration_venv_dirs = integration_source_dirs

    function_dirs = (
        _dedupe_paths([*resolved.function_dirs, *integration_function_dirs])
        if can_sync_custom
        else []
    )
    venv_dirs = (
        _dedupe_paths([*resolved.venv_dirs, *integration_venv_dirs])
        if can_sync_custom
        else []
    )

    logger.info(
        "Runtime reconcile phase starting: assistant=%s phase=syncing_custom_functions "
        "deployment_function_dirs=%d integration_function_dirs=%d "
        "deployment_venv_dirs=%d integration_venv_dirs=%d",
        identity.assistant_id,
        len(resolved.function_dirs),
        len(integration_function_dirs),
        len(resolved.venv_dirs),
        len(integration_venv_dirs),
    )
    if status is not None:
        status.update(
            phase="syncing_custom_functions",
            message="Preparing deployment-defined custom tools.",
            blocking_resources=("functions",),
            resources=function_resources,
            data_freshness="partial",
        )

    custom_changed = False
    custom_start = perf_counter()
    if can_sync_custom:
        collect_start = perf_counter()
        source_fns = collect_functions_from_directories(function_dirs)
        source_venvs = collect_venvs_from_directories(venv_dirs)
        log_startup_timing(
            logger,
            "⏱️ [StartupTiming] runtime_reconcile.collect_custom_sources assistant=%s duration=%.2fs functions=%d venvs=%d",
            identity.assistant_id,
            perf_counter() - collect_start,
            len(source_fns),
            len(source_venvs),
        )
        fm_start = perf_counter()
        fm = ManagerRegistry.get_function_manager()
        log_startup_timing(
            logger,
            "⏱️ [StartupTiming] runtime_reconcile.get_function_manager assistant=%s duration=%.2fs",
            identity.assistant_id,
            perf_counter() - fm_start,
        )
        sync_start = perf_counter()
        custom_changed = fm.sync_custom(
            source_functions=source_fns,
            source_venvs=source_venvs,
        )
        log_startup_timing(
            logger,
            "⏱️ [StartupTiming] runtime_reconcile.sync_custom assistant=%s duration=%.2fs changed=%s",
            identity.assistant_id,
            perf_counter() - sync_start,
            custom_changed,
        )
    else:
        logger.warning(
            "Runtime reconcile skipped custom function sync for assistant=%s "
            "because enabled integration sources were unavailable",
            identity.assistant_id,
        )
    logger.info(
        "Runtime reconcile phase completed: assistant=%s phase=syncing_custom_functions duration=%.2fs custom_changed=%s",
        identity.assistant_id,
        perf_counter() - custom_start,
        custom_changed,
    )

    if status is not None:
        status.update(
            phase="complete",
            message=(
                "Background assistant setup is complete. Deployment-defined "
                "data, guidance, secrets, and custom tools are ready."
            ),
            blocking_resources=(),
            resources={
                "contacts": "ready",
                "guidance": "ready",
                "knowledge": "ready",
                "secrets": "ready",
                "functions": "ready",
            },
            data_freshness="ready",
        )
    logger.info(
        "Runtime reconcile complete: assistant=%s revision=%s seed_changed=%s custom_changed=%s",
        identity.assistant_id,
        revision[:16],
        seed_changed,
        custom_changed,
    )

    return RuntimeStateResult(
        identity=identity,
        revision=revision,
        seed_changed=seed_changed,
        custom_changed=custom_changed,
    )
