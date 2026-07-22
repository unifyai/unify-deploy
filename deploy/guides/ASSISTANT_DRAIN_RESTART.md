# Assistant drain / restart control plane

Graceful and force restart for hosted assistants, including never-idle
workers that would otherwise keep overlapping offline Jobs forever.

## Why

Offline Jobs already re-fetch `latest.txt` on boot. Long-lived live sessions
and overlapping schedules do not: while admission stays open, task B can
start before A finishes, so the busy-set never empties and a new client
bundle never loads.

`DrainIntent` closes admission until in-flight work finishes (or a
deadline), then stops the live session and clears the intent so deferred
work runs on the new revision.

## API (`/infra`, admin key unless noted)

| Method | Path | Behavior |
|--------|------|----------|
| `POST` | `/assistants/{id}/restart` | Arm `graceful` or `force` drain |
| `GET` | `/assistants/{id}/restart` | Status + busy summary (admin **or** owning assistant key) |
| `DELETE` | `/assistants/{id}/restart` | Cancel if not yet `stopping` |
| `POST` | `/assistants/drain-bundle` | Fan-out by `routing_manifest.yaml` `bundle_key` |

Body for restart:

```json
{
  "mode": "graceful",
  "reason": "deploy",
  "target_revision": "abc123",
  "deadline_seconds": 900
}
```

Defaults: graceful deadline **900s**, then force-stop whatever is still busy.

Durable store: ConfigMap `unity-assistant-drain-intents` in the default
namespace (per-assistant JSON under `data`).

## Admission while `block_admission`

| Gate | Behavior |
|------|----------|
| `/infra/job/start` | **503** — no new live wake |
| `/infra/task-execution/offline-dispatch` | **503** — Cloud Tasks retries after clear |
| In-pod `act` (`drain_gate`) | Refuse new acts; finish current |
| Inactivity loop | If draining and `ACTIVE_WORK` empty → shut down |

## Ops scripts

```bash
# One assistant (prod Cloud Run default)
ORCHESTRA_ADMIN_KEY=… \
  deploy/scripts/dev/restart_assistant.sh 1406 graceful   # or force

# Staging
ORCHESTRA_ADMIN_KEY=… \
  UNITY_COMMS_URL=https://service.a.run.app \
  deploy/scripts/dev/restart_assistant.sh 7367 graceful

# Every assistant mapped to a client bundle
ORCHESTRA_ADMIN_KEY=… \
  deploy/scripts/dev/drain_bundle_assistants.sh unify_company production <sha>
```

`unify_company` → assistant **1406** (production) / **7367** (staging).

Canonical hosts:

- prod: `https://service.a.run.app`
- staging: `https://service.a.run.app`

## Deploy overlay

Brain `scripts/publish_client_bundle.sh` (after writing `latest.txt`) calls
`POST /infra/assistants/drain-bundle` when `ORCHESTRA_ADMIN_KEY` is set.
GitHub Actions needs repo secret `ORCHESTRA_ADMIN_KEY`; without it publish
still succeeds and drain is skipped. Do **not** pin `UNITY_COMMS_URL` to
prod on the brain repo — the script selects staging vs prod from
`ENVIRONMENT`.

Backup (not in v1): a long-lived runtime that self-compares its loaded
bundle SHA to `latest.txt` and self-arms drain when behind. Until that
exists, rely on publish fan-out + manual `restart_assistant.sh`.

## Code

- Comms: `communication/infra/drain.py`
- Runtime gate: `unify/runtime/drain_gate.py` (in `unify` repo)
- Bundle map: `unify_deploy/assistant_deployments/routing_manifest.yaml`
