# `brain_jobs` integration package

Internal Droid integration that hosts the scheduled-job template
generated from the brain repo's [`brain.scheduled`](https://github.com/unifyai/brain/tree/main/brain/scheduled)
registry.  This package exists so the existing `ScenarioActivation`
flow can materialise brain's recurring + trigger-based jobs at deploy
time, without any laptop step.

## What it contains

- [`manifest.yaml`](manifest.yaml) — minimal package manifest. No
  external API surface, no secrets, no capabilities — schedules +
  activation are the only thing this package contributes.
- [`scenarios/brain_jobs_v0.yaml`](scenarios/brain_jobs_v0.yaml) —
  generated template that names every brain-registered scheduled job
  with its cadence, execution mode, and FunctionManager entrypoint
  function name. The `client`, `deployment`, and per-task
  `assistant_id` fields stay empty intentionally; the deploying
  client's `ScenarioActivation` substitutes them at materialisation
  time.

## Regenerating the template

This file is generated.  Do not edit it by hand.

```bash
cd path/to/brain && source .venv/bin/activate
brain scheduled export-scenario   # defaults to brain_operator deployment
```

That command writes
`droid-deploy/droid_deploy/assistant_deployments/integrations/packages/brain_jobs/scenarios/brain_jobs_v0.yaml`
relative to the sibling droid-deploy checkout (configurable via
`--out`).  Commit + push the resulting YAML; the next droid-deploy
build picks it up.

## Activation

The activating deployment declares a `ScenarioActivation`:

```python
ScenarioActivation(
    scenario_template="brain_jobs/brain_jobs_v0",
    assistant_id="<numeric assistant id>",
    scenario_id_override="<client>_brain_jobs_v0",
    tasks_enabled=False,    # ship disabled, operator flips
)
```

See [`unify_company/deployments/brain_operator/deployment.py`](../../../clients/unify_company/deployments/brain_operator/deployment.py)
for the canonical example.

## See also

- Operating pattern: [`brain/docs/operations/scheduled-jobs.md`](https://github.com/unifyai/brain/blob/main/docs/operations/scheduled-jobs.md)
- Brain operator deployment: [`unify_company/deployments/brain_operator/README.md`](../../../clients/unify_company/deployments/brain_operator/README.md)
