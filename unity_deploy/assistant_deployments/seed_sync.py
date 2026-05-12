"""
Generic hash-based seed data sync utility.

Syncs code-defined records to DB-backed state manager contexts.
Each manager provides a thin adapter describing its natural key,
ID field, and CRUD operations.  The generic ``sync_seed_data()``
handles hash comparison, diffing, and create/update/delete.
"""

from __future__ import annotations

import hashlib
import json
import logging
from time import perf_counter
from typing import Any, Callable, TYPE_CHECKING

import unify

from unity.common.hierarchical_logger import ICONS
from unity.guidance_manager.types.guidance import Guidance
from unity.secret_manager.types import Secret
from unity_deploy.timing import log_startup_timing

if TYPE_CHECKING:
    from unity_deploy.assistant_deployments.clients import ResolvedAssistantDeployment

logger = logging.getLogger(__name__)
_ICON = ICONS.get("assistant_deployments", "")


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


def _record_hash(record: dict, exclude_fields: set[str]) -> str:
    payload = {k: v for k, v in sorted(record.items()) if k not in exclude_fields}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode(),
    ).hexdigest()[:16]


def _aggregate_hash(
    records: list[dict],
    natural_key_fn: Callable[[dict], str],
    exclude_fields: set[str],
) -> str:
    if not records:
        return ""
    pairs = sorted(
        (natural_key_fn(r), _record_hash(r, exclude_fields)) for r in records
    )
    combined = "|".join(f"{k}:{h}" for k, h in pairs)
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Meta store (persists per-manager hashes to DB)
# ---------------------------------------------------------------------------


class SeedMetaStore:
    """Read/write per-manager seed hashes in a ``SeedData/Meta`` context."""

    def __init__(self) -> None:
        self._ctx: str | None = None

    def _ensure_ctx(self) -> str:
        if self._ctx is not None:
            return self._ctx

        active = unify.get_active_context()["read"]
        self._ctx = f"{active}/SeedData/Meta"
        try:
            unify.create_context(self._ctx)
        except Exception:
            pass
        return self._ctx

    def get_hash(self, manager_key: str) -> str:
        ctx = self._ensure_ctx()
        logs = unify.get_logs(
            context=ctx,
            filter=f"manager_key == '{manager_key}'",
            limit=1,
        )
        if not logs:
            return ""
        return logs[0].entries.get("seed_hash", "")

    def set_hash(self, manager_key: str, seed_hash: str) -> None:
        ctx = self._ensure_ctx()
        logs = unify.get_logs(
            context=ctx,
            filter=f"manager_key == '{manager_key}'",
            limit=1,
        )
        if logs:
            unify.update_logs(
                logs=[logs[0].id],
                context=ctx,
                entries=[{"seed_hash": seed_hash}],
                overwrite=True,
            )
        else:
            unify.log(context=ctx, manager_key=manager_key, seed_hash=seed_hash)


# ---------------------------------------------------------------------------
# Generic sync
# ---------------------------------------------------------------------------


def _manager_api(manager: Any, method_name: str) -> Callable[..., Any]:
    """Resolve a manager API method across public/private naming variants."""

    method_names = (method_name, f"_{method_name}")
    for name in method_names:
        method = getattr(manager, name, None)
        if callable(method):
            if name != method_name:
                log_startup_timing(
                    logger,
                    "⏱️ [StartupTiming] seed_sync.manager_api fallback manager=%s method=%s resolved=%s",
                    type(manager).__name__,
                    method_name,
                    name,
                )
            return method

    manager_name = type(manager).__name__
    candidates = ", ".join(method_names)
    raise AttributeError(
        f"{manager_name} has none of the expected methods: {candidates}",
    )


def sync_seed_data(
    *,
    manager_key: str,
    source_records: list[dict],
    natural_key_fn: Callable[[dict], str],
    get_existing_fn: Callable[[], list[dict]],
    create_fn: Callable[[dict], Any],
    update_fn: Callable[[int, dict], Any] | None,
    delete_fn: Callable[[int], Any] | None,
    id_field: str,
    meta_store: SeedMetaStore,
    exclude_fields: set[str] | None = None,
) -> bool:
    """Sync code-defined records to a DB-backed state manager.

    Returns True if any changes were made.
    """
    exclude_fields = {id_field, *(exclude_fields or set())}
    total_start = perf_counter()
    expected_hash = _aggregate_hash(source_records, natural_key_fn, exclude_fields)
    meta_start = perf_counter()
    current_hash = meta_store.get_hash(manager_key)
    log_startup_timing(
        logger,
        "⏱️ [StartupTiming] seed_sync.%s.get_hash duration=%.2fs records=%d",
        manager_key,
        perf_counter() - meta_start,
        len(source_records),
    )

    if current_hash == expected_hash:
        logger.debug("%s Seed data for %s unchanged, skipping sync", _ICON, manager_key)
        return False

    logger.info(
        "%s Seed data for %s changed (current=%s, expected=%s), syncing...",
        _ICON,
        manager_key,
        current_hash,
        expected_hash,
    )

    existing_start = perf_counter()
    existing = get_existing_fn()
    log_startup_timing(
        logger,
        "⏱️ [StartupTiming] seed_sync.%s.get_existing duration=%.2fs existing=%d",
        manager_key,
        perf_counter() - existing_start,
        len(existing),
    )
    existing_by_key: dict[str, dict] = {}
    for rec in existing:
        try:
            existing_by_key[natural_key_fn(rec)] = rec
        except Exception:
            pass

    source_by_key = {natural_key_fn(r): r for r in source_records}
    processed_keys: set[str] = set()

    create_count = 0
    update_count = 0
    delete_count = 0
    apply_start = perf_counter()
    for key, src in source_by_key.items():
        processed_keys.add(key)
        if key in existing_by_key:
            db_rec = existing_by_key[key]
            src_hash = _record_hash(src, exclude_fields)
            db_hash = _record_hash(
                {k: v for k, v in db_rec.items() if k not in exclude_fields},
                exclude_fields,
            )
            if src_hash != db_hash and update_fn is not None:
                db_id = db_rec.get(id_field)
                if db_id is not None:
                    logger.info("%s Updating %s record: %s", _ICON, manager_key, key)
                    update_fn(db_id, src)
                    update_count += 1
        else:
            logger.info("%s Creating %s record: %s", _ICON, manager_key, key)
            create_fn(src)
            create_count += 1

    if delete_fn is not None:
        for key, db_rec in existing_by_key.items():
            if key not in processed_keys:
                db_id = db_rec.get(id_field)
                if db_id is not None:
                    logger.info("%s Deleting %s record: %s", _ICON, manager_key, key)
                    delete_fn(db_id)
                    delete_count += 1
    log_startup_timing(
        logger,
        "⏱️ [StartupTiming] seed_sync.%s.apply_changes duration=%.2fs creates=%d updates=%d deletes=%d",
        manager_key,
        perf_counter() - apply_start,
        create_count,
        update_count,
        delete_count,
    )

    meta_start = perf_counter()
    meta_store.set_hash(manager_key, expected_hash)
    log_startup_timing(
        logger,
        "⏱️ [StartupTiming] seed_sync.%s.set_hash duration=%.2fs total=%.2fs",
        manager_key,
        perf_counter() - meta_start,
        perf_counter() - total_start,
    )
    return True


# ---------------------------------------------------------------------------
# Per-manager adapters
# ---------------------------------------------------------------------------


def _sync_contacts(records: list[dict], meta: SeedMetaStore) -> bool:
    if not records:
        return False
    from unity.manager_registry import ManagerRegistry

    cm = ManagerRegistry.get_contact_manager()
    filter_contacts = _manager_api(cm, "filter_contacts")
    create_contact = _manager_api(cm, "create_contact")
    update_contact = _manager_api(cm, "update_contact")

    def natural_key(r: dict) -> str:
        return f"{r.get('first_name') or ''}|{r.get('surname') or ''}".lower()

    def get_existing() -> list[dict]:
        result = filter_contacts(limit=1000)
        contacts = result.get("contacts", [])
        return [c.model_dump() if hasattr(c, "model_dump") else c for c in contacts]

    def create(rec: dict) -> Any:
        return create_contact(**{k: v for k, v in rec.items() if k != "contact_id"})

    def update(contact_id: int, rec: dict) -> Any:
        fields = {k: v for k, v in rec.items() if k != "contact_id"}
        return update_contact(contact_id=contact_id, **fields)

    return sync_seed_data(
        manager_key="contacts",
        source_records=records,
        natural_key_fn=natural_key,
        get_existing_fn=get_existing,
        create_fn=create,
        update_fn=update,
        delete_fn=None,
        id_field="contact_id",
        meta_store=meta,
    )


def _sync_guidance(records: list[Guidance], meta: SeedMetaStore) -> bool:
    if not records:
        return False
    from unity.manager_registry import ManagerRegistry

    gm = ManagerRegistry.get_guidance_manager()
    filter_guidance = _manager_api(gm, "filter")
    add_guidance = _manager_api(gm, "add_guidance")
    update_guidance = _manager_api(gm, "update_guidance")
    delete_guidance = _manager_api(gm, "delete_guidance")
    source_dicts = [r.model_dump() for r in records]
    readonly_fields = {"authoring_assistant_id"}

    def writable_fields(rec: dict) -> dict:
        return {
            k: v for k, v in rec.items() if k not in {"guidance_id", *readonly_fields}
        }

    def natural_key(r: dict) -> str:
        return str(r.get("title", ""))

    def get_existing() -> list[dict]:
        entries = filter_guidance(limit=1000)
        return [g.model_dump() if hasattr(g, "model_dump") else g for g in entries]

    def create(rec: dict) -> Any:
        return add_guidance(**writable_fields(rec))

    def update(guidance_id: int, rec: dict) -> Any:
        fields = writable_fields(rec)
        return update_guidance(guidance_id=guidance_id, **fields)

    def delete(guidance_id: int) -> Any:
        return delete_guidance(guidance_id=guidance_id)

    return sync_seed_data(
        manager_key="guidance",
        source_records=source_dicts,
        natural_key_fn=natural_key,
        get_existing_fn=get_existing,
        create_fn=create,
        update_fn=update,
        delete_fn=delete,
        id_field="guidance_id",
        meta_store=meta,
        exclude_fields=readonly_fields,
    )


def _sync_secrets(records: list[Secret], meta: SeedMetaStore) -> bool:
    if not records:
        return False

    # Manifest-declared integration secrets emit ``Secret(value="")``: the
    # name is registered for the runtime allowlist (see
    # ``_sync_integration_registry``) but the actual value is owned by the
    # user — pasted via Console or written by the OAuth callback into the
    # Secrets context directly.  Empty-value rows must NOT flow through
    # this seed sync, because:
    #   1. The update path would overwrite the user's pasted value with
    #      "" whenever the existing-value read returns a stripped value.
    #   2. The delete branch (when a name later disappears from source)
    #      would wipe user state.
    # Filter them out: this sync only touches secrets whose values are
    # provided at deploy time (e.g. file_secrets from ``load_secrets``).
    source_records = [r for r in records if (r.value or "").strip()]
    if not source_records:
        return False
    from unity.manager_registry import ManagerRegistry

    sm = ManagerRegistry.get_secret_manager()
    list_secret_keys = _manager_api(sm, "list_secret_keys")
    create_secret = _manager_api(sm, "create_secret")
    update_secret = _manager_api(sm, "update_secret")
    source_dicts = [r.model_dump() for r in source_records]

    def natural_key(r: dict) -> str:
        return str(r.get("name", ""))

    def get_existing() -> list[dict]:
        keys = list_secret_keys()
        result = []
        for name in keys:
            logs = unify.get_logs(
                context=sm._ctx,
                filter=f"name == '{name}'",
                limit=1,
                from_fields=["secret_id", "name", "description"],
            )
            if logs:
                result.append(logs[0].entries)
        return result

    def create(rec: dict) -> Any:
        return create_secret(
            name=rec["name"],
            value=rec["value"],
            description=rec.get("description"),
        )

    def update(_secret_id: int, rec: dict) -> Any:
        return update_secret(
            name=rec["name"],
            value=rec["value"],
            description=rec.get("description"),
        )

    # delete_fn intentionally None: removing an integration package from a
    # deployment must not silently delete user-pasted credentials.  The
    # user removes those via the Console UI when they want to disconnect.
    return sync_seed_data(
        manager_key="secrets",
        source_records=source_dicts,
        natural_key_fn=natural_key,
        get_existing_fn=get_existing,
        create_fn=create,
        update_fn=update,
        delete_fn=None,
        id_field="secret_id",
        meta_store=meta,
    )


def _sync_blacklist(records: list[dict], meta: SeedMetaStore) -> bool:
    if not records:
        return False
    from unity.manager_registry import ManagerRegistry

    bm = ManagerRegistry.get_blacklist_manager()
    filter_blacklist = _manager_api(bm, "filter_blacklist")
    create_blacklist_entry = _manager_api(bm, "create_blacklist_entry")
    update_blacklist_entry = _manager_api(bm, "update_blacklist_entry")
    delete_blacklist_entry = _manager_api(bm, "delete_blacklist_entry")

    def natural_key(r: dict) -> str:
        return f"{r.get('medium', '')}|{r.get('contact_detail', '')}"

    def get_existing() -> list[dict]:
        result = filter_blacklist(limit=1000)
        entries = result.get("entries", [])
        return [e.model_dump() if hasattr(e, "model_dump") else e for e in entries]

    def create(rec: dict) -> Any:
        return create_blacklist_entry(
            medium=rec["medium"],
            contact_detail=rec["contact_detail"],
            reason=rec.get("reason", ""),
        )

    def update(blacklist_id: int, rec: dict) -> Any:
        return update_blacklist_entry(
            blacklist_id=blacklist_id,
            medium=rec.get("medium"),
            contact_detail=rec.get("contact_detail"),
            reason=rec.get("reason"),
        )

    def delete(blacklist_id: int) -> Any:
        return delete_blacklist_entry(blacklist_id=blacklist_id)

    return sync_seed_data(
        manager_key="blacklist",
        source_records=records,
        natural_key_fn=natural_key,
        get_existing_fn=get_existing,
        create_fn=create,
        update_fn=update,
        delete_fn=delete,
        id_field="blacklist_id",
        meta_store=meta,
    )


def _sync_knowledge(tables: dict[str, dict], meta: SeedMetaStore) -> bool:
    """Sync knowledge seed data.

    ``tables`` is a dict like::

        {
            "Companies": {
                "description": "Known companies",
                "columns": {"company_name": "str", "industry": "str"},
                "seed_key": "company_name",
                "rows": [{"company_name": "ClientDelta", "industry": "Real Estate"}],
            },
        }
    """
    if not tables:
        return False
    from unity.manager_registry import ManagerRegistry

    km = ManagerRegistry.get_knowledge_manager()
    tables_overview = _manager_api(km, "tables_overview")
    create_table = _manager_api(km, "create_table")
    filter_rows = _manager_api(km, "filter")
    add_rows = _manager_api(km, "add_rows")
    update_rows = _manager_api(km, "update_rows")
    delete_rows = _manager_api(km, "delete_rows")

    any_changed = False
    for table_name, table_spec in tables.items():
        rows = table_spec.get("rows", [])
        if not rows:
            continue
        seed_key = table_spec.get("seed_key")
        if not seed_key:
            logger.warning(
                "%s Knowledge table %s has no seed_key, skipping",
                _ICON,
                table_name,
            )
            continue

        existing_tables = tables_overview()
        if table_name not in existing_tables:
            create_table(
                name=table_name,
                description=table_spec.get("description"),
                columns=table_spec.get("columns"),
            )

        def make_natural_key(r: dict, _sk: str = seed_key) -> str:
            return str(r.get(_sk, ""))

        def get_existing(_tn: str = table_name) -> list[dict]:
            result = filter_rows(tables=[_tn], limit=1000)
            return result.get(_tn, [])

        unique_key = "row_id"
        if table_name in existing_tables:
            tbl_info = existing_tables[table_name]
            if isinstance(tbl_info, dict) and "unique_key" in tbl_info:
                unique_key = tbl_info["unique_key"]

        def create(rec: dict, _tn: str = table_name, _uk: str = unique_key) -> Any:
            clean = {k: v for k, v in rec.items() if k != _uk}
            return add_rows(table=_tn, rows=[clean])

        def update(
            row_id: int,
            rec: dict,
            _tn: str = table_name,
            _uk: str = unique_key,
        ) -> Any:
            clean = {k: v for k, v in rec.items() if k != _uk}
            return update_rows(table=_tn, updates={row_id: clean})

        def delete(row_id: int, _tn: str = table_name, _uk: str = unique_key) -> Any:
            return delete_rows(filter=f"{_uk} == {row_id}", tables=[_tn])

        changed = sync_seed_data(
            manager_key=f"knowledge/{table_name}",
            source_records=rows,
            natural_key_fn=make_natural_key,
            get_existing_fn=get_existing,
            create_fn=create,
            update_fn=update,
            delete_fn=delete,
            id_field=unique_key,
            meta_store=meta,
        )
        if changed:
            any_changed = True

    return any_changed


# ---------------------------------------------------------------------------
# Integration registry sync (Integrations/Manifests context)
# ---------------------------------------------------------------------------


_INTEGRATION_REGISTRY_CONTEXT_LEAF = "Integrations/Manifests"


def _sync_integration_registry(rows: list[dict], meta: SeedMetaStore) -> bool:
    """Push integration registry rows into the ``Integrations/Manifests`` context.

    Each row was projected from a manifest by
    ``integrations.loader._build_registry_row``.  Row identity is the ``slug``;
    re-deploying with the same set of integrations is idempotent.  Removing an
    integration from a deployment causes the corresponding row to be deleted so
    historical telemetry of which integrations a deployment declared stays
    accurate.

    .. note::

       As of the May-2026 cleanup, the **runtime no longer reads this
       registry**.  :mod:`unity.integration_status` consults disk
       discovery directly (see ``unity.integration_status.discovery``) so
       the runtime sees the same set of packages on registered and
       non-registered assistants alike.  This sync is now telemetry-only:
       it preserves a per-deployment record of which integrations were
       declared at deploy time, useful for ops dashboards and
       deployment-reconcile, but no runtime behaviour depends on it.
       Removing this sync entirely is an option once telemetry consumers
       are confirmed gone.
    """
    if not rows:
        return False

    active = unify.get_active_context()["read"]
    ctx = f"{active}/{_INTEGRATION_REGISTRY_CONTEXT_LEAF}"
    try:
        unify.create_context(ctx)
    except Exception:
        pass

    def natural_key(r: dict) -> str:
        return str(r.get("slug", ""))

    def get_existing() -> list[dict]:
        try:
            existing_logs = unify.get_logs(context=ctx, limit=1000)
        except Exception:
            return []
        existing: list[dict] = []
        for log in existing_logs:
            entries = dict(log.entries or {})
            # Stash the log id under ``_log_id`` so update/delete callbacks can
            # find it; the generic ``sync_seed_data`` helper expects a dict with
            # the natural key + an ``id_field``.
            entries["_log_id"] = log.id
            entries.setdefault("slug", entries.get("slug", ""))
            existing.append(entries)
        return existing

    def create(rec: dict) -> Any:
        unify.log(
            context=ctx, **{k: v for k, v in rec.items() if not k.startswith("_")}
        )
        return None

    def update(_unused_id: int, rec: dict) -> Any:
        # ``sync_seed_data`` calls update with ``rec[id_field]`` as the first arg;
        # we route via ``_log_id`` instead.
        log_id = rec.get("_log_id")
        if log_id is None:
            create(rec)
            return None
        unify.update_logs(
            logs=[log_id],
            context=ctx,
            entries=[{k: v for k, v in rec.items() if not k.startswith("_")}],
            overwrite=True,
        )
        return None

    def delete(log_id: int) -> Any:
        unify.delete_logs(context=ctx, logs=log_id)
        return None

    return sync_seed_data(
        manager_key="integration_registry",
        source_records=rows,
        natural_key_fn=natural_key,
        get_existing_fn=get_existing,
        create_fn=create,
        update_fn=update,
        delete_fn=delete,
        id_field="_log_id",
        meta_store=meta,
        exclude_fields={"_log_id"},
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def sync_all_seed_data(resolved: ResolvedAssistantDeployment) -> bool:
    """Sync all seed data from a resolved assistant deployment to the DB.

    Called from ``_init_managers()`` as an explicit cross-cutting step
    after all managers are constructed but before the Actor is initialized.
    Returns True if any manager was updated.
    """
    has_data = (
        resolved.contacts
        or resolved.guidance
        or resolved.knowledge
        or resolved.secrets
        or resolved.blacklist
        or resolved.integration_registry
    )
    if not has_data:
        return False

    meta = SeedMetaStore()
    changed = False

    if resolved.contacts:
        try:
            sync_start = perf_counter()
            changed |= _sync_contacts(resolved.contacts, meta)
            log_startup_timing(
                logger,
                "⏱️ [StartupTiming] seed_sync.contacts total=%.2fs",
                perf_counter() - sync_start,
            )
        except Exception:
            logger.exception("Failed to sync seed contacts")

    if resolved.guidance:
        try:
            sync_start = perf_counter()
            changed |= _sync_guidance(resolved.guidance, meta)
            log_startup_timing(
                logger,
                "⏱️ [StartupTiming] seed_sync.guidance total=%.2fs",
                perf_counter() - sync_start,
            )
        except Exception:
            logger.exception("Failed to sync seed guidance")

    if resolved.secrets:
        try:
            sync_start = perf_counter()
            changed |= _sync_secrets(resolved.secrets, meta)
            log_startup_timing(
                logger,
                "⏱️ [StartupTiming] seed_sync.secrets total=%.2fs",
                perf_counter() - sync_start,
            )
        except Exception:
            logger.exception("Failed to sync seed secrets")

    if resolved.blacklist:
        try:
            sync_start = perf_counter()
            changed |= _sync_blacklist(resolved.blacklist, meta)
            log_startup_timing(
                logger,
                "⏱️ [StartupTiming] seed_sync.blacklist total=%.2fs",
                perf_counter() - sync_start,
            )
        except Exception:
            logger.exception("Failed to sync seed blacklist")

    if resolved.knowledge:
        try:
            sync_start = perf_counter()
            changed |= _sync_knowledge(resolved.knowledge, meta)
            log_startup_timing(
                logger,
                "⏱️ [StartupTiming] seed_sync.knowledge total=%.2fs",
                perf_counter() - sync_start,
            )
        except Exception:
            logger.exception("Failed to sync seed knowledge")

    if resolved.integration_registry:
        try:
            sync_start = perf_counter()
            changed |= _sync_integration_registry(resolved.integration_registry, meta)
            log_startup_timing(
                logger,
                "⏱️ [StartupTiming] seed_sync.integration_registry total=%.2fs",
                perf_counter() - sync_start,
            )
        except Exception:
            logger.exception("Failed to sync integration registry")

    return changed
