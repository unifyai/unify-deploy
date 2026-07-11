"""Salto KS callable functions.

Underscore-prefixed modules are library code and skipped by
FunctionManager's discovery sweep:

* ``_client`` — auth (OAuth client_credentials with in-process
  access-token caching), retry, ``salto_request`` + verb-specific
  HTTP helpers.
* ``_config`` — runtime config dict resolved from env (endpoint
  overrides, default site, sync cadence, etc.).
* ``_capabilities`` — token capability probe.

Discoverable (registered) modules:

* ``live`` — single ``salto_request`` tool covering the live REST API.
  Writes are not gated in code; the actor surfaces what's about to
  change to the user before issuing a destructive verb because Salto
  operations affect physical access on real buildings.
* ``account`` — typed installation-pinning helpers used as the
  connectivity smoke test.
"""
