# Self-hosted OpenReplay (session replay)

Short-term path: **dedicated GCE VM** (OpenReplay's supported GCP install) with
**recordings on GCS**. Console and the marketing site send events via
`@openreplay/tracker` to `https://openreplay[-staging].unify.ai/ingest`.

This is intentionally **not** on GKE Autopilot — OpenReplay's helm charts need
RWX/hostPath-style volumes that fight Autopilot. A single `n2-standard-2` VM
is the fastest reliable path.

## Staging status (2026-07-26)

Already provisioned:

| Piece | Value |
|---|---|
| VM | `unity-openreplay-staging` (`n2-standard-2`, `us-central1-a`) |
| UI / ingest | https://openreplay.example.com |
| DNS | A → `203.0.113.18` (zone `unifyai`) |
| GCS | `unity-openreplay-{recordings,assets,sourcemaps}-staging` (30-day lifecycle) |
| HMAC SA | `openreplay-gcs@gcp-project-runtime` |
| HMAC secrets | `OPENREPLAY_GCS_HMAC_ACCESS_ID_STAGING`, `OPENREPLAY_GCS_HMAC_SECRET_STAGING` |
| TLS | Let's Encrypt via cert-manager (`openreplay-ssl` Ready) |
| OpenReplay | v1.27.x, `use_tls: true`, s3 → GCS |
| Projects | `my first project` (Console), `landing-page` (marketing site) |

**Console staging:** Cloud Build trigger `unify-console-redesign` has
`_OPENREPLAY_PROJECT_KEY` set; tracker lives in
`console/src/components/Integrations/OpenReplayTracker.tsx` (no cookie banner).

**Landing staging:** Cloud Build trigger `landing-page-staging` should have
`_OPENREPLAY_PROJECT_KEY` for the `landing-page` OpenReplay project; tracker is
consent-gated in `landing-page/src/components/integrations/OpenReplayTracker.tsx`.

```bash
gcloud compute ssh unity-openreplay-staging \
  --project=gcp-project-runtime --zone=us-central1-a \
  --tunnel-through-iap
```

## 1. GCS buckets + HMAC (idempotent)

```bash
./deploy/scripts/dev/setup_openreplay_infra.sh staging
# later:
./deploy/scripts/dev/setup_openreplay_infra.sh production
```

## 2. VM + DNS + install

```bash
./deploy/scripts/dev/provision_openreplay_vm.sh staging
# Follow printed DNS + IAP SSH + `openreplay -i` instructions.
```

Domains:

| Env | Hostname | VM |
|---|---|---|
| staging | `openreplay.example.com` | `unity-openreplay-staging` |
| production | `openreplay.unify.ai` | `unity-openreplay` |

After install, patch GCS into `vars.yaml` using `vars.yaml.example`, enable
`use_tls: true`, install cert-manager (`certmanager.sh` / ClusterIssuer), then
`openreplay -R`.

## 3. Project key → Console

Bake into Console builds (see `console` Dockerfile / `cloudbuild.yaml`):

| Var | Example |
|---|---|
| `NEXT_PUBLIC_OPENREPLAY_PROJECT_KEY` | from OpenReplay UI |
| `NEXT_PUBLIC_OPENREPLAY_INGEST_POINT` | `https://openreplay.example.com/ingest` |

Cloud Build substitutions (staging defaults in `cloudbuild.yaml`):

- `_OPENREPLAY_PROJECT_KEY` (empty → tracker no-ops)
- `_OPENREPLAY_INGEST_POINT`

## 4. Privacy (Unify choice)

Console tracker records **inputs unmasked** (`defaultInputMode: 0` and
obscure\* flags false). The product UI already masks secrets with `.` in the
DOM; replays should show what the user actually saw. Network request
bodies are not captured; `Authorization` / cookie headers are ignored.

## 5. Watch sessions

Open https://openreplay.example.com, sign in, filter by Orchestra user id
(Console calls `tracker.setUserID`) or email metadata.
