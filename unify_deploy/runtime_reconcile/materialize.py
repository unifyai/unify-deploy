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


def _function_name_to_ids(fm: Any) -> dict[str, int]:
    """Resolve entrypoint names once, preferring a lean id-only listing."""

    list_ids = getattr(fm, "list_function_name_to_ids", None)
    if callable(list_ids):
        return {
            name: int(function_id)
            for name, function_id in list_ids().items()
            if function_id is not None
        }
    return {
        name: int(data["function_id"])
        for name, data in fm.list_functions().items()
        if data.get("function_id") is not None
    }


def _sync_custom_tasks_with_log(
    task_scheduler: Any,
    *,
    source_tasks: dict[str, dict[str, Any]],
    function_name_to_id: dict[str, int],
    assistant_id: str,
) -> bool:
    """Synchronize authored task definitions and expose the result in pod logs."""

    changed = task_scheduler.sync_custom(
        source_tasks=source_tasks,
        function_name_to_id=function_name_to_id,
    )
    logger.info(
        "Runtime reconcile custom task sync: assistant=%s changed=%s task_count=%d",
        assistant_id,
        changed,
        len(source_tasks),
    )
    return bool(changed)


def _run_named_phase(
    *,
    assistant_id: str,
    phase: str,
    fn: Any,
) -> tuple[Any, float]:
    """Run one reconcile sub-phase with start/complete timing logs."""

    from unify.common.sync_lease import SyncLeaseBusy

    started = perf_counter()
    logger.info(
        "Runtime reconcile phase starting: assistant=%s phase=%s",
        assistant_id,
        phase,
    )
    try:
        result = fn()
    except SyncLeaseBusy as exc:
        duration = perf_counter() - started
        logger.warning(
            "Runtime reconcile phase skipped (sync lease busy): assistant=%s "
            "phase=%s duration=%.2fs lease_key=%s held_by=%s",
            assistant_id,
            phase,
            duration,
            exc.lease_key,
            exc.held_by,
        )
        return False, duration
    except Exception:
        logger.exception(
            "Runtime reconcile phase failed: assistant=%s phase=%s duration=%.2fs",
            assistant_id,
            phase,
            perf_counter() - started,
        )
        raise
    duration = perf_counter() - started
    logger.info(
        "Runtime reconcile phase completed: assistant=%s phase=%s duration=%.2fs",
        assistant_id,
        phase,
        duration,
    )
    return result, duration


def _parallel_sync_wave(
    *,
    assistant_id: str,
    jobs: list[tuple[str, Any]],
) -> tuple[dict[str, tuple[Any, float]], list[tuple[str, BaseException]]]:
    """Run independent sync callables concurrently.

    Returns ``(phase→(result, duration), soft-failed phases)``. A failure in
    one phase does not abort the wave or raise — callers surface errors in
    status and continue offline/live setup.
    """

    import contextvars
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from unify.common.sync_lease import SyncLeaseBusy

    if not jobs:
        return {}, []
    if len(jobs) == 1:
        phase, fn = jobs[0]
        try:
            return (
                {
                    phase: _run_named_phase(
                        assistant_id=assistant_id,
                        phase=phase,
                        fn=fn,
                    ),
                },
                [],
            )
        except Exception as exc:
            if isinstance(exc, SyncLeaseBusy):
                raise
            logger.error(
                "Runtime reconcile parallel wave soft-failing for assistant=%s "
                "failed_phases=%s; continuing with successful phases",
                assistant_id,
                [phase],
            )
            return {}, [(phase, exc)]

    results: dict[str, tuple[Any, float]] = {}
    errors: list[tuple[str, BaseException]] = []
    wave_started = perf_counter()
    logger.info(
        "Runtime reconcile parallel wave starting: assistant=%s phases=%s",
        assistant_id,
        [phase for phase, _ in jobs],
    )
    with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        # One Context copy per job — a single Context cannot be entered from
        # multiple threads at once (`ctx.run` raises "already entered").
        future_to_phase = {
            executor.submit(
                contextvars.copy_context().run,
                _run_named_phase,
                assistant_id=assistant_id,
                phase=phase,
                fn=fn,
            ): phase
            for phase, fn in jobs
        }
        for future in as_completed(future_to_phase):
            phase = future_to_phase[future]
            exc = future.exception()
            if exc is not None:
                errors.append((phase, exc))
                continue
            results[phase] = future.result()
    logger.info(
        "Runtime reconcile parallel wave completed: assistant=%s duration=%.2fs phases=%d errors=%d",
        assistant_id,
        perf_counter() - wave_started,
        len(jobs),
        len(errors),
    )
    if errors:
        # Soft-fail: one broken phase (e.g. custom data / guidance) must not
        # abort the rest of offline/live setup. Failures are already logged
        # with traceback inside `_run_named_phase`.
        failed_phases = [phase for phase, _ in errors]
        logger.error(
            "Runtime reconcile parallel wave soft-failing for assistant=%s "
            "failed_phases=%s; continuing with successful phases",
            assistant_id,
            failed_phases,
        )
    return results, errors


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

    from unify.session_details import SESSION_DETAILS

    if identity.owner_team_id is not None:
        if SESSION_DETAILS.owner_team_id != identity.owner_team_id:
            raise RuntimeError(
                f"Refusing runtime materialize for assistant {identity.assistant_id}: "
                f"expected SESSION_DETAILS.owner_team_id={identity.owner_team_id}, "
                f"got {SESSION_DETAILS.owner_team_id!r}",
            )

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
        "deployment_venv_dirs=%d integration_venv_dirs=%d can_sync_custom=%s",
        identity.assistant_id,
        len(resolved.function_dirs),
        len(integration_function_dirs),
        len(resolved.venv_dirs),
        len(integration_venv_dirs),
        can_sync_custom,
    )
    if status is not None:
        status.update(
            phase="syncing_custom_functions",
            message="Preparing deployment-defined custom functions.",
            blocking_resources=("functions",),
            resources=function_resources,
            data_freshness="partial",
        )

    custom_changed = False
    function_name_to_id: dict[str, int] = {}
    functions_resource_state = "ready"
    functions_sync_error = ""
    if can_sync_custom:
        functions_phase_start = perf_counter()
        collect_start = perf_counter()
        source_functions = collect_functions_from_directories(function_dirs)
        source_venvs = collect_venvs_from_directories(venv_dirs)
        collect_duration = perf_counter() - collect_start
        logger.info(
            "Runtime reconcile custom function collect complete: assistant=%s "
            "duration=%.2fs function_dirs=%d venv_dirs=%d "
            "source_functions=%d source_venvs=%d",
            identity.assistant_id,
            collect_duration,
            len(function_dirs),
            len(venv_dirs),
            len(source_functions),
            len(source_venvs),
        )
        fm = ManagerRegistry.get_function_manager()
        sync_start = perf_counter()
        try:
            custom_changed = fm.sync_custom(
                source_functions=source_functions,
                source_venvs=source_venvs,
            )
        except Exception as exc:
            from unify.common.sync_lease import SyncLeaseBusy
            from unify.function_manager.custom_functions import (
                CustomFunctionSyncPartialFailure,
            )

            if isinstance(exc, SyncLeaseBusy):
                logger.warning(
                    "Runtime reconcile skipping custom function sync for assistant=%s; "
                    "sync lease busy (lease_key=%s held_by=%s)",
                    identity.assistant_id,
                    exc.lease_key,
                    exc.held_by,
                )
                custom_changed = False
                functions_resource_state = "skipped"
            elif isinstance(exc, CustomFunctionSyncPartialFailure):
                # Successful names already landed; keep going so unrelated
                # tasks (campaign runtime, etc.) are not blocked by one bad
                # function such as run_social_render_storyboards.
                custom_changed = True
                functions_resource_state = "failed"
                functions_sync_error = str(exc)
                logger.exception(
                    "Runtime reconcile custom function sync partially failed "
                    "for assistant=%s failed_functions=%s; continuing reconcile",
                    identity.assistant_id,
                    sorted(exc.failures),
                )
            else:
                custom_changed = False
                functions_resource_state = "failed"
                functions_sync_error = str(exc)
                logger.exception(
                    "Runtime reconcile custom function sync failed for "
                    "assistant=%s; continuing reconcile without aborting the "
                    "offline/live task",
                    identity.assistant_id,
                )
        sync_duration = perf_counter() - sync_start
        logger.info(
            "Runtime reconcile custom function sync_custom complete: assistant=%s "
            "duration=%.2fs custom_changed=%s functions_state=%s",
            identity.assistant_id,
            sync_duration,
            custom_changed,
            functions_resource_state,
        )
        function_name_to_id = _function_name_to_ids(fm)
        log_startup_timing(
            logger,
            "⏱️ [StartupTiming] runtime_reconcile.sync_custom_functions assistant=%s duration=%.2fs changed=%s",
            identity.assistant_id,
            perf_counter() - functions_phase_start,
            custom_changed,
        )
        logger.info(
            "Runtime reconcile phase completed: assistant=%s phase=syncing_custom_functions "
            "duration=%.2fs custom_changed=%s collect_duration=%.2fs sync_duration=%.2fs",
            identity.assistant_id,
            perf_counter() - functions_phase_start,
            custom_changed,
            collect_duration,
            sync_duration,
        )
    else:
        logger.warning(
            "Runtime reconcile skipping custom function sync for assistant=%s; "
            "enabled integration sources are unavailable",
            identity.assistant_id,
        )
        function_name_to_id = _function_name_to_ids(
            ManagerRegistry.get_function_manager(),
        )
        logger.info(
            "Runtime reconcile phase completed: assistant=%s phase=syncing_custom_functions "
            "duration=0.00s custom_changed=False skipped=True",
            identity.assistant_id,
        )

    function_resources = {
        **function_resources,
        "functions": functions_resource_state,
        "guidance": "syncing",
        "knowledge": "syncing",
        "secrets": "syncing",
        "contacts": "syncing",
    }
    if status is not None:
        status.update(
            phase="syncing_custom_state",
            message=(
                "Preparing deployment-defined runtime state."
                if not functions_sync_error
                else (
                    "Preparing deployment-defined runtime state after a "
                    f"custom-function sync problem: {functions_sync_error}"
                )
            ),
            error=functions_sync_error or None,
            blocking_resources=(),
            resources=function_resources,
            data_freshness="partial",
        )

    from unify.knowledge_manager.custom_knowledge import (
        collect_knowledge_from_directories,
    )
    from unify.data_manager.custom_data import collect_data_from_directories
    from unify.dashboard_manager.custom_dashboards import (
        collect_dashboards_from_directories,
    )
    from unify.task_scheduler.custom_tasks import collect_tasks_from_directories
    from unify.file_manager.custom_files import collect_files_from_directories

    # Eager manager construction on the main thread so ManagerRegistry stays
    # single-threaded; workers only call sync methods on already-built managers.
    gm = ManagerRegistry.get_guidance_manager()
    cmgr = ManagerRegistry.get_contact_manager()
    km = ManagerRegistry.get_knowledge_manager()
    dm = ManagerRegistry.get_data_manager()
    dbm = ManagerRegistry.get_dashboard_manager()
    tm = ManagerRegistry.get_task_scheduler()
    file_mgr = ManagerRegistry.get_file_manager()
    sm = ManagerRegistry.get_secret_manager()
    bm = ManagerRegistry.get_blacklist_manager()

    source_guidance = (
        collect_guidance_from_directories(guidance_dirs) if can_sync_custom else {}
    )
    source_contacts = collect_contacts_from_directories(
        _dedupe_paths(resolved.contacts_dirs),
    )
    source_knowledge = collect_knowledge_from_directories(
        _dedupe_paths(resolved.knowledge_dirs),
    )
    source_data = collect_data_from_directories(
        _dedupe_paths(resolved.custom_data_dirs),
    )
    source_dashboards = collect_dashboards_from_directories(
        _dedupe_paths(resolved.dashboards_dirs),
    )
    source_tasks = collect_tasks_from_directories(
        _dedupe_paths(resolved.tasks_dirs),
    )
    logger.info(
        "Runtime reconcile collected %d custom task definition(s) for assistant=%s: %s",
        len(source_tasks),
        identity.assistant_id,
        sorted(source_tasks),
    )
    source_files = collect_files_from_directories(
        _dedupe_paths(resolved.files_dirs),
    )
    source_secrets = collect_secrets_from_directories(
        _dedupe_paths(resolved.secrets_dirs),
    )
    source_secrets.update(collect_secrets_from_secret_models(resolved.secrets))
    source_blacklist = collect_blacklist_from_directories(
        _dedupe_paths(resolved.blacklist_dirs),
    )

    jobs: list[tuple[str, Any]] = []
    if can_sync_custom:
        jobs.append(
            (
                "syncing_custom_guidance",
                lambda: gm.sync_custom(
                    source_guidance=source_guidance,
                    function_name_to_id=function_name_to_id,
                ),
            ),
        )
    else:
        logger.warning(
            "Runtime reconcile skipping custom guidance sync for assistant=%s; "
            "enabled integration sources are unavailable",
            identity.assistant_id,
        )

    jobs.extend(
        [
            (
                "syncing_custom_contacts",
                lambda: cmgr.sync_custom(source_contacts=source_contacts),
            ),
            (
                "syncing_custom_knowledge",
                lambda: km.sync_custom(source_claims=source_knowledge),
            ),
            (
                "syncing_custom_data",
                lambda: dm.sync_custom(source_tables=source_data),
            ),
            (
                "syncing_custom_dashboards",
                lambda: dbm.sync_custom(source_entities=source_dashboards),
            ),
            ("syncing_custom_tasks", lambda: _sync_custom_tasks_with_log(
                tm,
                source_tasks=source_tasks,
                function_name_to_id=function_name_to_id,
                assistant_id=identity.assistant_id,
            )),
            (
                "syncing_custom_files",
                lambda: file_mgr.sync_custom(source_files=source_files),
            ),
            (
                "syncing_custom_secrets",
                lambda: (
                    sm.sync_custom(source_secrets=source_secrets),
                    sm._sync_dotenv(),
                )[0],
            ),
            (
                "syncing_custom_blacklist",
                lambda: bm.sync_custom(source_blacklist=source_blacklist),
            ),
        ],
    )

    wave, wave_errors = _parallel_sync_wave(
        assistant_id=identity.assistant_id,
        jobs=jobs,
    )

    def _changed(phase: str) -> bool:
        packed = wave.get(phase)
        return bool(packed[0]) if packed is not None else False

    def _duration(phase: str) -> float:
        packed = wave.get(phase)
        return float(packed[1]) if packed is not None else 0.0

    guidance_changed = _changed("syncing_custom_guidance")
    contacts_changed = _changed("syncing_custom_contacts")
    knowledge_changed = _changed("syncing_custom_knowledge")
    custom_data_changed = _changed("syncing_custom_data")
    dashboards_changed = _changed("syncing_custom_dashboards")
    tasks_changed = _changed("syncing_custom_tasks")
    files_changed = _changed("syncing_custom_files")
    secrets_changed = _changed("syncing_custom_secrets")
    blacklist_changed = _changed("syncing_custom_blacklist")

    wave_failed_phases = {phase for phase, _ in wave_errors}
    wave_error_text = (
        "; ".join(f"{phase}: {type(exc).__name__}: {exc}" for phase, exc in wave_errors)
        if wave_errors
        else ""
    )
    combined_sync_error = "; ".join(
        part for part in (functions_sync_error, wave_error_text) if part
    )

    phase_to_resource = {
        "syncing_custom_guidance": "guidance",
        "syncing_custom_contacts": "contacts",
        "syncing_custom_knowledge": "knowledge",
        "syncing_custom_data": "data",
        "syncing_custom_dashboards": "dashboards",
        "syncing_custom_tasks": "tasks",
        "syncing_custom_files": "files",
        "syncing_custom_secrets": "secrets",
        "syncing_custom_blacklist": "blacklist",
    }

    for phase_name, changed_flag, timing_key in (
        ("syncing_custom_guidance", guidance_changed, "sync_custom_guidance"),
        ("syncing_custom_contacts", contacts_changed, "sync_custom_contacts"),
        ("syncing_custom_knowledge", knowledge_changed, "sync_custom_knowledge"),
        ("syncing_custom_data", custom_data_changed, "sync_custom_data"),
        ("syncing_custom_dashboards", dashboards_changed, "sync_custom_dashboards"),
        ("syncing_custom_tasks", tasks_changed, "sync_custom_tasks"),
        ("syncing_custom_files", files_changed, "sync_custom_files"),
        ("syncing_custom_secrets", secrets_changed, "sync_custom_secrets"),
        ("syncing_custom_blacklist", blacklist_changed, "sync_custom_blacklist"),
    ):
        if phase_name not in wave and phase_name == "syncing_custom_guidance":
            continue
        log_startup_timing(
            logger,
            "⏱️ [StartupTiming] runtime_reconcile.%s assistant=%s duration=%.2fs changed=%s",
            timing_key,
            identity.assistant_id,
            _duration(phase_name),
            changed_flag,
        )

    if status is not None:
        functions_ready = functions_resource_state == "ready"
        resource_states = {
            "contacts": "ready",
            "guidance": "ready",
            "knowledge": "ready",
            "data": "ready",
            "dashboards": "ready",
            "tasks": "ready",
            "files": "ready",
            "secrets": "ready",
            "functions": functions_resource_state,
        }
        for phase_name in wave_failed_phases:
            resource_name = phase_to_resource.get(phase_name)
            if resource_name is not None:
                resource_states[resource_name] = "failed"
        setup_ready = functions_ready and not wave_failed_phases
        status.update(
            phase="complete",
            message=(
                "Background assistant setup is complete. Deployment-defined "
                "data, guidance, secrets, and custom tools are ready."
                if setup_ready
                else (
                    "Background assistant setup finished with sync problems: "
                    f"{combined_sync_error or 'partial failure'}. "
                    "Successful phases remain ready; failed ones retry on the "
                    "next reconcile."
                )
            ),
            error=combined_sync_error or None,
            blocking_resources=(),
            resources=resource_states,
            data_freshness="ready" if setup_ready else "partial",
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
        secrets_changed=secrets_changed,
        contacts_changed=contacts_changed,
        knowledge_changed=knowledge_changed,
        custom_data_changed=custom_data_changed,
        dashboards_changed=dashboards_changed,
        tasks_changed=tasks_changed,
        files_changed=files_changed,
        blacklist_changed=blacklist_changed,
    )
