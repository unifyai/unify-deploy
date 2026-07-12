"""Employment Hero callable functions.

Underscore-prefixed modules are library code and skipped by
FunctionManager's discovery sweep:

* ``_client`` — auth (OAuth refresh-token flow with in-process
  access-token caching), retry, ``eh_request`` + verb-specific HTTP
  helpers (``eh_get``, ``eh_post``, ``eh_patch``, ``eh_delete``,
  ``eh_paginate``).  Also exposes ``org_path`` for path construction
  and ``_org_id_or_error`` for org-pinning lookup.
* ``_config`` — runtime config dict resolved from env (sync hubs,
  cadence overrides, page size).
* ``_capabilities`` — token capability probe.
* ``_local_helpers`` — DataManager-query helpers used by ``local``.
* ``_pay_helpers`` — ``band_rate`` for coarse pay banding in the
  ``sync_employmenthero_pay`` snapshot path.
* ``_sync_helpers`` — sync registries + freshness helpers +
  DataManager state loader.
* ``_sync_<surface>`` — per-object snapshot writers (~17 modules),
  dispatched by the orchestrator in ``sync`` via importlib.

Discoverable (registered) modules:

* ``live`` — single ``employmenthero_request`` tool covering the
  live REST API.  Writes are not gated in code; the actor applies
  the rules in ``employmenthero_high_stakes_writes`` and
  ``employmenthero_sensitive_data`` before issuing destructive verbs
  or surfacing PII.
* ``account`` — typed org-pinning helpers.  Most paths take an
  ``{org_id}`` segment and ``get_employmenthero_active_organisation``
  resolves the pinned id (or first accessible org) so the actor
  doesn't have to.
* ``local`` — ``query_local_employmenthero_*`` analytical queries
  against the synced DataManager copy.
* ``sync`` — orchestrator + state inspection + tier probe.
"""
