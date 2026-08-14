"""In-memory stand-in for the Orchestra logs API.

The deployment sync paths -- the integration registry reconcile, custom
guidance sync, secret creation -- are unify-deploy logic sitting on top of
unisdk's logs API. Exercising them used to mean pointing unisdk at a deployed
Orchestra, which made the assertions depend on a credential and on whatever
rows that backend happened to hold.

This module supplies the storage semantics those paths rely on -- contexts,
rows with server-assigned ids and auto-counted columns, filtered reads,
federated reads merged across contexts -- backed by a dict, so managers and
reconcile adapters run against it unmodified.

Two properties keep it honest rather than merely quiet:

* Nothing escapes. ``install`` replaces unisdk's HTTP session, so a call to an
  endpoint the store does not implement cannot reach a real backend. unisdk
  copies ``BASE_URL`` into every submodule at import, which is why the session
  is the choke point and a pinned ``ORCHESTRA_URL`` would not be one.
* Nothing is silently empty. A blocked request and a filter expression the
  store cannot evaluate are both recorded, and ``assert_fully_served`` fails
  the test on either. Manager code routinely wraps reads in ``except
  Exception``, so without that record a missing endpoint would surface as "no
  rows matched" and the assertions would pass against nothing.
"""

from __future__ import annotations

import itertools
import sys
from typing import Any, Dict, List, Optional, Sequence

import unisdk
from unisdk.utils import http as unisdk_http

FAKE_PROJECT = "UnifyDeployHermeticTests"
"""Project every hermetic test writes under, pinned so no run inherits one."""


class _MissingIsNone(dict):
    """Row scope where an absent column reads as ``None``, as the backend does."""

    def __missing__(self, key: str) -> None:
        return None


class FakeOrchestra:
    """The logs API's storage behaviour, held in memory."""

    def __init__(self, project: str = FAKE_PROJECT) -> None:
        self.project = project
        self.contexts: Dict[str, Dict[str, Any]] = {}
        self.rows: Dict[int, unisdk.Log] = {}
        self.unserved: List[str] = []
        self._ids = itertools.count(1)
        self._counters: Dict[tuple[str, str], itertools.count] = {}

    # ------------------------------------------------------------------ #
    # Assertions                                                         #
    # ------------------------------------------------------------------ #
    def assert_fully_served(self) -> None:
        """Fail if anything was blocked or skipped rather than answered."""
        assert not self.unserved, (
            "the in-memory Orchestra could not serve "
            f"{len(self.unserved)} call(s), so the assertions above ran "
            "against incomplete data:\n  " + "\n  ".join(self.unserved)
        )

    # ------------------------------------------------------------------ #
    # Contexts                                                           #
    # ------------------------------------------------------------------ #
    def create_context(
        self,
        name: str,
        *_args: Any,
        auto_counting: Optional[Dict[str, Optional[str]]] = None,
        **_kwargs: Any,
    ) -> None:
        config = self.contexts.setdefault(_norm(name), {"auto_counting": {}})
        config["auto_counting"].update(auto_counting or {})

    def get_contexts(self, *_args: Any, **_kwargs: Any) -> List[str]:
        return sorted(self.contexts)

    def delete_context(self, name: str, *_args: Any, **_kwargs: Any) -> None:
        context = _norm(name)
        self.contexts.pop(context, None)
        for row_id in [i for i, row in self.rows.items() if row.context == context]:
            del self.rows[row_id]

    def get_fields(
        self,
        *,
        context: Optional[str] = None,
        **_kwargs: Any,
    ) -> Dict[str, str]:
        fields: Dict[str, str] = {}
        for row in self._in_context(_resolve(context, "read")):
            for key, value in row.entries.items():
                fields.setdefault(key, type(value).__name__)
        return fields

    def create_fields(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    # ------------------------------------------------------------------ #
    # Writes                                                             #
    # ------------------------------------------------------------------ #
    def log(
        self,
        *,
        context: Optional[str] = None,
        **entries: Any,
    ) -> unisdk.Log:
        for reserved in ("project", "new", "overwrite", "mutable", "api_key"):
            entries.pop(reserved, None)
        return self._insert(_resolve(context, "write"), entries)

    def create_logs(
        self,
        *,
        context: Optional[str] = None,
        entries: Any = None,
        **_kwargs: Any,
    ) -> List[int]:
        target = _resolve(context, "write")
        if isinstance(entries, dict):
            entries = [entries]
        return [self._insert(target, entry).id for entry in entries or []]

    def update_logs(
        self,
        *,
        context: Optional[str] = None,
        logs: Any = None,
        entries: Any = None,
        overwrite: bool = False,
        **_kwargs: Any,
    ) -> List[int]:
        ids = _as_list(logs)
        updates = [entries] * len(ids) if isinstance(entries, dict) else entries or []
        for row_id, update in zip(ids, updates):
            row = self.rows.get(int(row_id))
            if row is None:
                continue
            if overwrite:
                row.entries.clear()
            row.entries.update(update)
        return ids

    def delete_logs(
        self,
        *,
        logs: Any = None,
        **_kwargs: Any,
    ) -> List[int]:
        ids = _as_list(logs)
        for row_id in ids:
            self.rows.pop(int(row_id), None)
        return ids

    def _insert(self, context: str, entries: Dict[str, Any]) -> unisdk.Log:
        row_id = next(self._ids)
        stored = dict(entries)
        auto_counting = self.contexts.get(context, {}).get("auto_counting", {})
        for column in auto_counting:
            if stored.get(column) is None:
                stored[column] = next(
                    self._counters.setdefault(
                        (context, column),
                        itertools.count(1),
                    ),
                )
        self.rows[row_id] = unisdk.Log(id=row_id, context=context, **stored)
        return self.rows[row_id]

    # ------------------------------------------------------------------ #
    # Reads                                                              #
    # ------------------------------------------------------------------ #
    def get_logs(
        self,
        *,
        context: Optional[str] = None,
        filter: Optional[str] = None,
        limit: Optional[int] = 1000,
        offset: int = 0,
        from_fields: Optional[Sequence[str]] = None,
        exclude_fields: Optional[Sequence[str]] = None,
        return_ids_only: bool = False,
        **kwargs: Any,
    ) -> List[Any]:
        self._note_unmodelled("get_logs", kwargs)
        rows = [
            row
            for row in self._in_context(_resolve(context, "read"))
            if self._matches(filter, row.entries)
        ]
        rows = rows[offset : None if limit is None else offset + limit]
        if return_ids_only:
            return [row.id for row in rows]
        return [
            unisdk.Log(
                id=row.id,
                context=row.context,
                **_project_fields(row.entries, from_fields, exclude_fields),
            )
            for row in rows
        ]

    def get_logs_federated(
        self,
        *,
        contexts: Sequence[Dict[str, Any]],
        filter: Optional[str] = None,
        sorting: Optional[Sequence[Dict[str, Any]]] = None,
        offset: int = 0,
        limit: int = 100,
        unique_id_field: Optional[str] = None,
        annotate: bool = True,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        merged: List[Dict[str, Any]] = []
        counts: Dict[str, int] = {}
        for spec in contexts:
            context = _norm(spec["context"])
            source = spec.get("source") or context
            for row in self._in_context(context):
                if not self._matches(filter, row.entries):
                    continue
                if not self._matches(spec.get("filter"), row.entries):
                    continue
                entries = _project_fields(
                    row.entries,
                    spec.get("from_fields"),
                    spec.get("exclude_fields"),
                )
                if annotate:
                    entries["_federated_source"] = source
                    entries["_federated_context"] = context
                merged.append(entries)
                counts[source] = counts.get(source, 0) + 1
        total = len(merged)
        for spec in reversed(list(sorting or [])):
            field = spec["field"]
            merged.sort(
                key=lambda entries: (
                    entries.get(field) is None,
                    entries.get(field),
                ),
                reverse=spec.get("direction") == "descending",
            )
        if unique_id_field:
            seen: set[Any] = set()
            deduped = []
            for entries in merged:
                key = entries.get(unique_id_field)
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(entries)
            merged = deduped
        return {
            "logs": merged[offset : offset + limit],
            "count": total,
            "counts": counts,
        }

    def _in_context(self, context: str) -> List[unisdk.Log]:
        rows = [row for row in self.rows.values() if row.context == context]
        return sorted(rows, key=lambda row: row.id)

    def _matches(self, filter: Optional[str], entries: Dict[str, Any]) -> bool:
        if not filter:
            return True
        try:
            return bool(eval(filter, {"__builtins__": {}}, _MissingIsNone(entries)))
        except Exception as exc:
            self.unserved.append(f"filter {filter!r} could not be evaluated: {exc}")
            return False

    def _note_unmodelled(self, call: str, kwargs: Dict[str, Any]) -> None:
        """Record row-selecting arguments the store does not honour."""
        for name in ("from_ids", "exclude_ids", "group_by", "sorting"):
            if kwargs.get(name):
                self.unserved.append(f"{call} was passed {name}={kwargs[name]!r}")

    # ------------------------------------------------------------------ #
    # Everything else the sync paths reach for                           #
    # ------------------------------------------------------------------ #
    def acquire_sync_lease(self, *_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        return {"acquired": True}

    def release_sync_lease(self, *_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        return {"released": True}

    def create_project(self, project: str, *_args: Any, **_kwargs: Any) -> None:
        self.project = project

    def activate(self, project: str, *_args: Any, **_kwargs: Any) -> None:
        self.project = project
        unisdk.PROJECT = project


class _BlockedSession:
    """Stands in for unisdk's requests session so no call leaves the process."""

    def __init__(self, store: FakeOrchestra) -> None:
        self._store = store

    def request(self, method: str, url: str, **_kwargs: Any):
        self._store.unserved.append(f"{method} {url} was blocked")
        raise RuntimeError(
            f"{method} {url}: hermetic tests have no Orchestra to call. "
            "Model the endpoint on FakeOrchestra if this path needs it.",
        )


def install(monkeypatch) -> FakeOrchestra:
    """Serve unisdk's logs API from memory for the duration of one test."""
    store = FakeOrchestra()
    served = (
        "acquire_sync_lease",
        "activate",
        "create_context",
        "create_fields",
        "create_logs",
        "create_project",
        "delete_context",
        "delete_logs",
        "get_contexts",
        "get_fields",
        "get_logs",
        "get_logs_federated",
        "log",
        "release_sync_lease",
        "update_logs",
    )
    for name in served:
        replacement = getattr(store, name)
        original = getattr(unisdk, name)
        monkeypatch.setattr(unisdk, name, replacement)
        # A module that did ``from unisdk import log`` before this ran holds
        # the original directly and would sail past the attribute swap above,
        # so every existing alias is rebound too. Modules imported after this
        # point pick up the replacement through unisdk itself.
        for module in list(sys.modules.values()):
            if module is not None and getattr(module, name, None) is original:
                monkeypatch.setattr(module, name, replacement)
    monkeypatch.setattr(unisdk, "PROJECT", store.project)
    monkeypatch.setattr(unisdk_http, "_SESSION", _BlockedSession(store))
    return store


def _norm(context: Any) -> str:
    return str(context or "").strip("/")


def _resolve(context: Optional[str], mode: str) -> str:
    return _norm(context or unisdk.get_active_context()[mode])


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _project_fields(
    entries: Dict[str, Any],
    from_fields: Optional[Sequence[str]],
    exclude_fields: Optional[Sequence[str]],
) -> Dict[str, Any]:
    """Apply a read's field allow/deny lists, as the backend does."""
    selected = dict(entries)
    if from_fields:
        selected = {k: v for k, v in selected.items() if k in set(from_fields)}
    if exclude_fields:
        selected = {k: v for k, v in selected.items() if k not in set(exclude_fields)}
    return selected
