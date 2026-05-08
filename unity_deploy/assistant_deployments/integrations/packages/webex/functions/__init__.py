"""Webex callable functions.

Underscore-prefixed modules are library code and skipped by
FunctionManager's discovery sweep:

* ``_client`` — auth resolution, refresh-token caching, ``webex_request``
  + ``webex_get`` HTTP helpers.
* ``_config`` — runtime config dict resolved from env.
* ``_capabilities`` — token capability probe used by ``probe_webex_tier``.
* ``_local_helpers`` — DataManager-query helpers used by ``local``.
* ``_sync_helpers`` — DataManager-write helpers used by sync.
* ``_sync_meetings`` / ``_sync_people`` / ``_sync_recordings`` /
  ``_sync_rooms`` / ``_sync_transcripts`` — per-object snapshot
  writers, dispatched by the orchestrator in ``sync``.

Discoverable (registered) modules:

* ``live`` — single ``webex_request`` tool covering the live REST API.
* ``local`` — ``query_local_webex_*`` DataManager-side analytical
  queries.
* ``sync`` — orchestrator + state inspection + tier probe.
"""
