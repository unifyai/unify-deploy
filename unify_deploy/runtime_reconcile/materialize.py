"""Assistant-scoped runtime state materialization."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
from time import perf_counter
from typing import Any

from unify_deploy.assistant_deployments.clients import ResolvedAssistantDeployment
from unify_deploy.runtime_reconcile.context import RuntimeIdentity
from unify_deploy.runtime_reconcile.status import RuntimeReconcileStatusHandle
from unify_deploy.timing import log_startup_timing

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeStateResult:
    """Summary of runtime-state materialization."""

    identity: RuntimeIdentity
    revision: str
    integration_registry_changed: bool = False
    custom_changed: bool = False
    guidance_changed: bool = False
    secrets_changed: bool = False
    contacts_changed: bool = False
    knowledge_changed: bool = False
    custom_data_changed: bool = False
    dashboards_changed: bool = False
    tasks_changed: bool = False
    files_changed: bool = False
    blacklist_changed: bool = False


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
        "contacts_dirs": [
            {"path": str(path), "digest": _hash_path(path)}
            for path in resolved.contacts_dirs
        ],
        "knowledge_dirs": [
            {"path": str(path), "digest": _hash_path(path)}
            for path in resolved.knowledge_dirs
        ],
        "custom_data_dirs": [
            {"path": str(path), "digest": _hash_path(path)}
            for path in resolved.custom_data_dirs
        ],
        "dashboards_dirs": [
            {"path": str(path), "digest": _hash_path(path)}
            for path in resolved.dashboards_dirs
        ],
        "tasks_dirs": [
            {"path": str(path), "digest": _hash_path(path)}
            for path in resolved.tasks_dirs
        ],
        "files_dirs": [
            {"path": str(path), "digest": _hash_path(path)}
            for path in resolved.files_dirs
        ],
        "blacklist_dirs": [
            {"path": str(path), "digest": _hash_path(path)}
            for path in resolved.blacklist_dirs
        ],
        "secrets_dirs": [
            {"path": str(path), "digest": _hash_path(path)}
            for path in resolved.secrets_dirs
        ],
        "supplemental_secrets": _supplemental_secret_fingerprints(resolved.secrets),
        "integration_registry": _integration_registry_fingerprints(
            resolved.integration_registry,
        ),
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
        "guidance_dirs": [
            {"path": str(path), "digest": _hash_path(path)}
            for path in resolved.guidance_dirs
        ],
    }
    return _hash_payload(payload)


def _integration_registry_fingerprints(
    rows: list[Any],
) -> list[dict[str, str]]:
    from unify.integration_registry.custom_integration_registry import (
        collect_integration_registry_from_rows,
    )

    return [
        {"slug": slug, "digest": data["custom_hash"]}
        for slug, data in sorted(
            collect_integration_registry_from_rows(rows).items(),
        )
    ]


def _supplemental_secret_fingerprints(secrets: list[Any]) -> list[dict[str, str]]:
    from unify.secret_manager.custom_secrets import collect_secrets_from_secret_models

    return [
        {"name": name, "digest": data["custom_hash"]}
        for name, data in sorted(
            collect_secrets_from_secret_models(secrets).items(),
        )
    ]


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


def _enabled_integration_source_dirs() -> (
    tuple[list[Path], list[Path], list[Path]] | None
):
    """Return enabled integration function/venv/guidance dirs, or None if unknown.

    ``FunctionManager.sync_custom`` and ``GuidanceManager.sync_custom`` treat
    their inputs as the authoritative set of source-defined rows. Runtime
    reconciliation therefore needs to include enabled integration packages in
    the same sets before deleting stale deployment-owned rows. If integration
    discovery cannot be read, return ``None`` so callers can avoid destructive
    empty syncs based on incomplete information.
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
    guidance_dirs: list[Path] = []
    for package in enabled.values():
        function_dir = package.get("function_dir")
        if function_dir is not None:
            function_dirs.append(Path(function_dir))

        root_dir = package.get("root_dir")
        if root_dir is not None:
            venv_dir = Path(root_dir) / "venvs"
            if venv_dir.is_dir():
                venv_dirs.append(venv_dir)

        guidance_dir = package.get("guidance_dir")
        if guidance_dir is not None:
            guidance_dirs.append(Path(guidance_dir))

    return (
        _dedupe_paths(function_dirs),
        _dedupe_paths(venv_dirs),
        _dedupe_paths(guidance_dirs),
    )


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
    from unify.blacklist_manager.custom_blacklist import (
        collect_blacklist_from_directories,
    )
    from unify.contact_manager.custom_contacts import (
        collect_contacts_from_directories,
    )
    from unify.secret_manager.custom_secrets import (
        collect_secrets_from_directories,
        collect_secrets_from_secret_models,
    )
    from unify.guidance_manager.custom_guidance import (
        collect_guidance_from_directories,
    )
    from unify.integration_registry import (
        collect_integration_registry_from_rows,
        sync_custom_integration_registry,
    )
    from unify_deploy.assistant_deployments.integrations.catalog_projection import (
        sync_integrations,
    )
    from unify.manager_registry import ManagerRegistry

    revision = revision or compute_runtime_state_fingerprint(resolved)
    logger.info(
        "Runtime reconcile phase starting: assistant=%s phase=syncing_integration_registry revision=%s",
        identity.assistant_id,
        revision[:16],
    )
    if status is not None:
        status.update(
            phase="syncing_integration_registry",
            message="Preparing deployment-defined integration registry.",
            blocking_resources=(),
            resources={
                "contacts": "pending",
                "guidance": "pending",
                "knowledge": "pending",
                "secrets": "pending",
                "functions": "pending",
            },
            data_freshness="partial",
        )
    integration_registry_start = perf_counter()
    source_registry = collect_integration_registry_from_rows(
        resolved.integration_registry,
    )
    integration_registry_changed = sync_custom_integration_registry(
        source_registry=source_registry,
    )
    if resolved.integration_registry:
        try:
            catalog_start = perf_counter()
            sync_integrations(resolved.integration_registry)
            log_startup_timing(
                logger,
                "⏱️ [StartupTiming] runtime_reconcile.native_integration_catalog assistant=%s duration=%.2fs",
                identity.assistant_id,
                perf_counter() - catalog_start,
            )
        except Exception:
            logger.exception("Failed to publish native integration app catalog")
    log_startup_timing(
        logger,
        "⏱️ [StartupTiming] runtime_reconcile.sync_custom_integration_registry assistant=%s duration=%.2fs changed=%s",
        identity.assistant_id,
        perf_counter() - integration_registry_start,
        integration_registry_changed,
    )
    logger.info(
        "Runtime reconcile phase completed: assistant=%s phase=syncing_integration_registry duration=%.2fs integration_registry_changed=%s",
        identity.assistant_id,
        perf_counter() - integration_registry_start,
        integration_registry_changed,
    )

    function_resources = {
        "contacts": "ready",
        "guidance": "pending",
        "knowledge": "pending",
        "secrets": "ready",
        "functions": "syncing",
    }
    integration_source_dirs = _enabled_integration_source_dirs()
    integration_function_dirs: list[Path] = []
    integration_venv_dirs: list[Path] = []
    integration_guidance_dirs: list[Path] = []
    can_sync_custom = integration_source_dirs is not None
    if can_sync_custom:
        (
            integration_function_dirs,
            integration_venv_dirs,
            integration_guidance_dirs,
        ) = integration_source_dirs

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
    guidance_dirs = (
        _dedupe_paths([*resolved.guidance_dirs, *integration_guidance_dirs])
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
    guidance_changed = False
    contacts_changed = False
    secrets_changed = False
    knowledge_changed = False
    custom_data_changed = False
    dashboards_changed = False
    tasks_changed = False
    files_changed = False
    blacklist_changed = False
    custom_start = perf_counter()
    if can_sync_custom:
        collect_start = perf_counter()
        source_fns = collect_functions_from_directories(function_dirs)
        source_venvs = collect_venvs_from_directories(venv_dirs)
        source_guidance = collect_guidance_from_directories(guidance_dirs)
        log_startup_timing(
            logger,
            "⏱️ [StartupTiming] runtime_reconcile.collect_custom_sources assistant=%s duration=%.2fs functions=%d venvs=%d guidance=%d",
            identity.assistant_id,
            perf_counter() - collect_start,
            len(source_fns),
            len(source_venvs),
            len(source_guidance),
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

        function_name_to_id = {
            name: data["function_id"]
            for name, data in fm.list_functions().items()
            if data.get("function_id") is not None
        }
        if status is not None:
            status.update(
                phase="syncing_custom_functions",
                message="Preparing deployment-defined custom guidance.",
                blocking_resources=("guidance",),
                resources={
                    **function_resources,
                    "functions": "ready",
                    "guidance": "syncing",
                },
                data_freshness="partial",
            )
        guidance_start = perf_counter()
        gm = ManagerRegistry.get_guidance_manager()
        guidance_changed = gm.sync_custom(
            source_guidance=source_guidance,
            function_name_to_id=function_name_to_id,
        )
        log_startup_timing(
            logger,
            "⏱️ [StartupTiming] runtime_reconcile.sync_custom_guidance assistant=%s duration=%.2fs changed=%s",
            identity.assistant_id,
            perf_counter() - guidance_start,
            guidance_changed,
        )
    else:
        logger.warning(
            "Runtime reconcile skipped custom function sync for assistant=%s "
            "because enabled integration sources were unavailable",
            identity.assistant_id,
        )

    contacts_dirs = _dedupe_paths(resolved.contacts_dirs)
    source_contacts = collect_contacts_from_directories(contacts_dirs)
    contacts_start = perf_counter()
    cm = ManagerRegistry.get_contact_manager()
    contacts_changed = cm.sync_custom(source_contacts=source_contacts)
    log_startup_timing(
        logger,
        "⏱️ [StartupTiming] runtime_reconcile.sync_custom_contacts assistant=%s duration=%.2fs changed=%s",
        identity.assistant_id,
        perf_counter() - contacts_start,
        contacts_changed,
    )

    knowledge_dirs = _dedupe_paths(resolved.knowledge_dirs)
    from unify.knowledge_manager.custom_knowledge import (
        collect_knowledge_from_directories,
    )

    source_knowledge = collect_knowledge_from_directories(knowledge_dirs)
    knowledge_start = perf_counter()
    km = ManagerRegistry.get_knowledge_manager()
    knowledge_changed = km.sync_custom(source_tables=source_knowledge)
    log_startup_timing(
        logger,
        "⏱️ [StartupTiming] runtime_reconcile.sync_custom_knowledge assistant=%s duration=%.2fs changed=%s",
        identity.assistant_id,
        perf_counter() - knowledge_start,
        knowledge_changed,
    )

    custom_data_dirs = _dedupe_paths(resolved.custom_data_dirs)
    from unify.data_manager.custom_data import collect_data_from_directories

    source_data = collect_data_from_directories(custom_data_dirs)
    custom_data_start = perf_counter()
    dm = ManagerRegistry.get_data_manager()
    custom_data_changed = dm.sync_custom(source_tables=source_data)
    log_startup_timing(
        logger,
        "⏱️ [StartupTiming] runtime_reconcile.sync_custom_data assistant=%s duration=%.2fs changed=%s",
        identity.assistant_id,
        perf_counter() - custom_data_start,
        custom_data_changed,
    )

    dashboards_dirs = _dedupe_paths(resolved.dashboards_dirs)
    from unify.dashboard_manager.custom_dashboards import (
        collect_dashboards_from_directories,
    )

    source_dashboards = collect_dashboards_from_directories(dashboards_dirs)
    dashboards_start = perf_counter()
    dash_mgr = ManagerRegistry.get_dashboard_manager()
    dashboards_changed = dash_mgr.sync_custom(source_entities=source_dashboards)
    log_startup_timing(
        logger,
        "⏱️ [StartupTiming] runtime_reconcile.sync_custom_dashboards assistant=%s duration=%.2fs changed=%s",
        identity.assistant_id,
        perf_counter() - dashboards_start,
        dashboards_changed,
    )

    tasks_dirs = _dedupe_paths(resolved.tasks_dirs)
    from unify.task_scheduler.custom_tasks import collect_tasks_from_directories

    source_tasks = collect_tasks_from_directories(tasks_dirs)
    fm_for_tasks = ManagerRegistry.get_function_manager()
    function_name_to_id_for_tasks = {
        name: data["function_id"]
        for name, data in fm_for_tasks.list_functions().items()
        if data.get("function_id") is not None
    }
    tasks_start = perf_counter()
    ts = ManagerRegistry.get_task_scheduler()
    tasks_changed = ts.sync_custom(
        source_tasks=source_tasks,
        function_name_to_id=function_name_to_id_for_tasks,
    )
    log_startup_timing(
        logger,
        "⏱️ [StartupTiming] runtime_reconcile.sync_custom_tasks assistant=%s duration=%.2fs changed=%s",
        identity.assistant_id,
        perf_counter() - tasks_start,
        tasks_changed,
    )

    files_dirs = _dedupe_paths(resolved.files_dirs)
    from unify.file_manager.custom_files import collect_files_from_directories

    source_files = collect_files_from_directories(files_dirs)
    files_start = perf_counter()
    file_mgr = ManagerRegistry.get_file_manager()
    files_changed = file_mgr.sync_custom(source_files=source_files)
    log_startup_timing(
        logger,
        "⏱️ [StartupTiming] runtime_reconcile.sync_custom_files assistant=%s duration=%.2fs changed=%s",
        identity.assistant_id,
        perf_counter() - files_start,
        files_changed,
    )

    secrets_dirs = _dedupe_paths(resolved.secrets_dirs)
    source_secrets = collect_secrets_from_directories(secrets_dirs)
    source_secrets.update(collect_secrets_from_secret_models(resolved.secrets))
    secrets_start = perf_counter()
    sm = ManagerRegistry.get_secret_manager()
    secrets_changed = sm.sync_custom(source_secrets=source_secrets)
    log_startup_timing(
        logger,
        "⏱️ [StartupTiming] runtime_reconcile.sync_custom_secrets assistant=%s duration=%.2fs changed=%s",
        identity.assistant_id,
        perf_counter() - secrets_start,
        secrets_changed,
    )

    blacklist_dirs = _dedupe_paths(resolved.blacklist_dirs)
    source_blacklist = collect_blacklist_from_directories(blacklist_dirs)
    blacklist_start = perf_counter()
    bm = ManagerRegistry.get_blacklist_manager()
    blacklist_changed = bm.sync_custom(source_blacklist=source_blacklist)
    log_startup_timing(
        logger,
        "⏱️ [StartupTiming] runtime_reconcile.sync_custom_blacklist assistant=%s duration=%.2fs changed=%s",
        identity.assistant_id,
        perf_counter() - blacklist_start,
        blacklist_changed,
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
                "data": "ready",
                "dashboards": "ready",
                "tasks": "ready",
                "files": "ready",
                "secrets": "ready",
                "functions": "ready",
            },
            data_freshness="ready",
        )
    logger.info(
        "Runtime reconcile complete: assistant=%s revision=%s integration_registry_changed=%s custom_changed=%s guidance_changed=%s contacts_changed=%s knowledge_changed=%s custom_data_changed=%s dashboards_changed=%s tasks_changed=%s files_changed=%s secrets_changed=%s blacklist_changed=%s",
        identity.assistant_id,
        revision[:16],
        integration_registry_changed,
        custom_changed,
        guidance_changed,
        contacts_changed,
        knowledge_changed,
        custom_data_changed,
        dashboards_changed,
        tasks_changed,
        files_changed,
        secrets_changed,
        blacklist_changed,
    )

    return RuntimeStateResult(
        identity=identity,
        revision=revision,
        integration_registry_changed=integration_registry_changed,
        custom_changed=custom_changed,
        guidance_changed=guidance_changed,
        contacts_changed=contacts_changed,
        knowledge_changed=knowledge_changed,
        custom_data_changed=custom_data_changed,
        dashboards_changed=dashboards_changed,
        tasks_changed=tasks_changed,
        files_changed=files_changed,
        secrets_changed=secrets_changed,
        blacklist_changed=blacklist_changed,
    )
