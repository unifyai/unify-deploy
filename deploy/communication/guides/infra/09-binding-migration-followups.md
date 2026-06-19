# Binding Migration Follow-ups

Deferred after the binding rollout is deployed and the live integration suite is rerun.

## Remaining cleanup

- Delete the dead pre-migration controller path in `communication/assistant_session_controller/controller.py`, including the shadow `_update_status_for_session()` implementation and stale helpers that still read top-level `status.jobRef` / `status.vmRef`.
- Decide the rollout handling for pre-migration `AssistantSession` objects that still carry flat `spec.desktopRequired` / `spec.desktopMode`. The new reconciler only reads nested `spec.desktop`.
- Tighten Droid desktop-ready handling in `droid/droid/conversation_manager/domains/event_handlers.py` so missing `binding_id` fails closed when the current session already has a binding, and add regression tests for stale/missing-binding events.
- Update `droid/deploy/scripts/stress_test/cleanup_stale_jobs.py` to stop runtimes through `AssistantSession` desired state or use binding-scoped release data instead of assistant-wide release requests.
- Remove unused legacy cleanup helpers from `orchestra/orchestra/web/api/utils/assistant_infra.py` once callers are gone: `release_pool_vm()`, `stop_jobs()`, and the job-based runtime status path via `get_running_jobs()`.
- Unify async/sync teardown semantics in `orchestra/orchestra/web/api/utils/assistant_infra.py`, especially around missing Comms configuration, retry behavior, and follow-on cleanup gating.
- Update docs and examples that still describe assistant-wide VM release or old session field shapes, especially under `guides/infra/`.
- Consider exposing `binding_id` on `/infra/vm/pool/status` if future tooling or diagnostics need a first-class binding-aware pool view.

## Deployment note

- Before or during rollout, ensure existing live `AssistantSession` CRs are not left on the old flat desktop-field shape. Drain or recreate them rather than adding compatibility reads back into production code.
