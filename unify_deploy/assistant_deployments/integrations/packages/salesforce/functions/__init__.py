"""Salesforce callable functions.

Underscore-prefixed modules are library code and skipped by
FunctionManager's discovery sweep:

* ``_client`` — auth (OAuth refresh-token flow with in-process
  access-token caching), retry, ``salesforce_request`` +
  ``salesforce_get`` + ``salesforce_query`` (SOQL with cursor
  following).
* ``_config`` — runtime config dict resolved from env (sync objects,
  cadence overrides, API version, page size).
* ``_sync_helpers`` — sync state loader and freshness helpers.
* ``_sync_<surface>`` — per-object snapshot writers (accounts,
  contacts, leads, opportunities, cases), dispatched by the
  orchestrator in ``sync`` via importlib.

Discoverable (registered) modules:

* ``live`` — single ``salesforce_request`` tool covering the live REST
  surface.  Writes are not gated in code; the actor surfaces what's
  about to change to the user before issuing a destructive verb.
* ``soql`` — typed ``run_salesforce_soql`` / ``describe_salesforce_object``
  helpers (kept because cursor-following + WHERE-clause shape make a
  typed wrapper meaningfully cheaper than per-call construction).
* ``identity`` — ``get_salesforce_me`` connectivity probe.
* ``local`` — ``query_local_salesforce_*`` analytical queries.
* ``sync`` — orchestrator + state inspection.
"""
