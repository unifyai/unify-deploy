"""HubSpot callable functions.

Underscore-prefixed modules are library code and skipped by
FunctionManager's discovery sweep:

* ``_client`` — auth (Private App bearer token), retry, ``hubspot_request``
  + verb-specific HTTP helpers (``hubspot_get``, ``hubspot_post``,
  ``hubspot_patch``, ``hubspot_delete``, ``hubspot_search``).
* ``_config`` — runtime config dict resolved from env (sync hubs,
  cadence overrides, page size).
* ``_capabilities`` — token capability probe.
* ``_normalize`` — DataManager row-shape normalisers used by every
  sync writer.
* ``_engagement_helpers`` — shared body/property fields for engagement
  syncs.
* ``_sync_helpers`` — sync registries (CRM, engagement, marketing,
  sales, service) + freshness helpers + DataManager state loader.
* ``_sync_<surface>`` — per-object snapshot writers (~43 modules),
  dispatched by the orchestrator in ``sync`` via importlib.

Discoverable (registered) modules:

* ``live`` — single ``hubspot_request`` tool covering the live REST API.
* ``search`` — typed CRM search (``search_hubspot``).
* ``associations`` — typed cross-object association helpers.
* ``properties`` — typed property-schema helpers.
* ``local_query`` — ``query_local_hubspot_*`` analytical queries
  against the synced DataManager copy.
* ``sync`` — orchestrator + state inspection.
"""
