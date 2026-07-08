"""Brain operator FunctionManager wrappers.

Every module here exposes :func:`@custom_function`-decorated
coroutines that thinly wrap brain entrypoints.  ``FunctionManager``
discovers them at assistant boot via
``unify.function_manager.custom_functions.collect_custom_functions``;
the resulting ``Functions/Compositional.function_id`` values are what
``brain.scheduled.install`` stamps onto the ``Tasks.entrypoint`` field
so the deterministic offline-dispatcher can call them.

Modules:

* ``crm`` — CRM Gmail / Fireflies sync ticks and the digest / hygiene
  / pipeline-pack daily-or-weekly jobs.
* ``outbound`` — evergreen-tick dispatcher (one wrapper per tick
  name; per-campaign cadence lives in the scenario YAML).
* ``influencers`` — YouTube browser-extraction one-shot trigger.
* ``intel`` — HackerNews-via-browser daily digest to WhatsApp
  (canonical example of a browser-driven scheduled job).
* ``social`` — Instagram/TikTok auto-publish pipeline (exported to
  ``brain_jobs_v0.yaml`` but ships ``enabled: false`` until armed).
* ``gtm`` — stargazer poll/enrich, SmartLead reconcile, outbound replenish
  (``Data/GTM`` single source of truth on brain operator).

Underscore-prefixed modules (none today) would be library-only and
skipped by FunctionManager's discovery sweep.
"""
