# Unity Communication Platform

## System Architecture

This repository is the external communication gateway in a multi-repository system:

```
         User (Console/Phone/SMS/Email)
                      │
    ┌─────────────────┴──────────────────┐
    │           Communication            │
    │    (Webhooks, Voice, SMS, Email)   │
    └────┬───────────────────────────────┘
         │
    ┌────┴────┐    ┌─────────┐    ┌─────────┐
    │  Unity  │    │  Unify  │    │Orchestra│
    │ (Brain) │───▶│  (SDK)  │───▶│  (API)  │
    │         │    │         │    │  (DB)   │
    └────┬────┘    └────┬────┘    └────┬────┘
         │              ▲              ▲
         │              │              │
         │    ┌─────────┴─┐       ┌────┴───────┐
         └───▶│  UniLLM   │       │  Console   │
              │ (LLM API) │       │(Interfaces)│
              └───────────┘       └────────────┘
```

**This repo (Communication)** receives external events (Twilio webhooks, Gmail notifications) and routes them to Unity for processing. Unity calls back to Communication when it needs to send messages, make calls, or dispatch voice agents.

Related repositories:
- [Unity](https://github.com/unifyai/unity) — AI assistant brain
- [Orchestra](https://github.com/unifyai/orchestra) — Backend API and database
- [Console](https://github.com/unifyai/console) — Web UI and observability dashboard

---

This repository provides a unified communication service with two primary components:

1. **Adapters** – A FastAPI application that handles unauthenticated webhooks from Twilio (voice, SMS, WhatsApp), Gmail, and Microsoft (Outlook, Teams). These endpoints are typically form-encoded or JSON-based callbacks from external services. The code resides in the `adapters/` directory and is deployed to Cloud Run.

2. **Communication API** – A FastAPI application exposing JSON endpoints, protected by an admin API key (`auth_admin_key`), for orchestration by Unity/Orchestra. The code lives in the `communication/` package and is also deployed to Cloud Run.

---

## Security

### Endpoint Authentication

All adapter endpoints that can trigger actions (start containers, send messages, manage infrastructure) require admin key authentication via a `Depends(require_admin_key)` FastAPI dependency. The admin key is validated with `secrets.compare_digest()` for timing-attack resistance.

**Authenticated endpoints** (require `Authorization: Bearer {ORCHESTRA_ADMIN_KEY}`):
- `/assistant/wakeup`, `/assistant/update` — called by Orchestra during hiring/config changes
- `/unify/attachment`, `/unify/message`, `/unify/meet` — called by Unity containers and Console
- `/unity/system-event`, `/unity/pre-hire` — called by Orchestra
- `/scheduled/*` (all 5 endpoints) — called by Cloud Scheduler with admin key in headers

**Signature-validated endpoints** (Twilio):
- `/twilio/call`, `/twilio/call-status`, `/twilio/sms`, `/twilio/whatsapp` — validated via `X-Twilio-Signature` using `TWILIO_AUTH_TOKEN` (gracefully skipped if token not configured)

**Externally-validated endpoints** (no app-level auth, validated by the external service):
- `/livekit/recording-complete` — LiveKit webhook signature verification
- `/email/gmail` — Google Pub/Sub push (validated by Pub/Sub delivery)
- `/email/outlook`, `/chat/teams` — Microsoft Graph `clientState` secret validation
- `/microsoft/router`, `/microsoft/auth/callback` — Microsoft OAuth flow
- `/health` — health check

### Webhook Secrets

Outlook and Teams webhook validation requires secrets that must be set as environment variables. The service logs an error and returns 500 if they are missing when a webhook arrives.

### Rate Limiting

An IP-based rate limiter protects all endpoints (120 requests per IP per 60-second window).

### PII Handling

All logging uses `logger` (not `print`). Phone numbers are redacted to last 4 digits, email addresses to domain only, and message bodies are never logged.

### SBC Proxy (Kamailio)

The SBC proxy at `sbc.unify.ai` uses TLS 1.2 with certificate verification enabled. IP allowlisting restricts inbound Teams Direct Routing calls to Microsoft's IP ranges.

### Required Environment Variables (Security)

| Variable | Purpose | Impact if missing |
|----------|---------|-------------------|
| `ORCHESTRA_ADMIN_KEY` | Admin endpoint authentication | Authenticated endpoints reject all requests |
| `TWILIO_AUTH_TOKEN` | Twilio webhook signature validation | Validation skipped (warning logged) |
| `OUTLOOK_WEBHOOK_SECRET` | Outlook notification validation | Outlook webhooks return 500 |
| `TEAMS_WEBHOOK_SECRET` | Teams notification validation | Teams webhooks return 500 |
| `OAUTH_STATE_SIGNING_KEY` | (Optional) HMAC signing for OAuth state parameter | State signature verification skipped |

### GCP Infrastructure (not tracked in code)

The following infrastructure settings are configured directly in GCP (`gcp-project-runtime`):

- **Cloud Scheduler**: All scheduler jobs (both staging and production) include `Authorization: Bearer {admin_key}` headers for all 10 adapter scheduled endpoints.
- **Firewall rules**: All remote-access rules (`default-allow-ssh`, `default-allow-rdp`, `allow-winrm`, `allow-2222`, `allow-6080`, `allow-8080`) are restricted to the IAP tunnel range (`35.235.240.0/20`). Direct SSH/RDP from the internet is blocked; use `gcloud compute ssh --tunnel-through-iap` instead.
- **VMs**: Terminated VMs are deleted promptly to release external IPs and reduce attack surface. No idle VMs with external IPs should remain in the project.

### GitHub Repository Settings (not tracked in code)

- **Branch protection** on `main`: Requires 1 approving pull request review. Force pushes and branch deletions are blocked.
- **Dependabot**: Vulnerability alerts and automated security fixes are enabled.

---

## Repository Structure

- `adapters/`
  - `main.py`, `helpers.py`, `requirements.txt`, `__init__.py`
  - Unauthenticated webhook handlers:
    - `twilio_call_webhook` – inbound voice calls (TwiML conference setup + Pub/Sub dispatch)
    - `twilio_msg_webhook`  – inbound SMS messages
    - `twilio_whatsapp_webhook` – inbound WhatsApp messages
    - additional functions for Gmail watch renewal and Pub/Sub notification processing

- `communication/`
  - FastAPI app package with routers under:
    - `phone/`  – Twilio call/SMS form callbacks (unauth) + JSON endpoints for call control, number management, agent dispatch (auth)
    - `whatsapp/` – Twilio WhatsApp form callbacks + JSON send/sender management (auth)
    - `email/`    – Google Workspace user management and Gmail send/watch (auth)
    - `social/`    – Verification code generation over WhatsApp or SMS (auth)
    - `infra/`     – Infrastructure-related endpoints (e.g. health checks, metrics) (auth)

- `main.py`       – FastAPI entrypoint, mounts routers under `/phone`, `/whatsapp`, `/gmail`, `/outlook`, `/infra`, `/social` with appropriate auth dependencies
- `requirements.txt` – Dependencies for the Communication API
- `adapters/requirements.txt` – Dependencies for the Adapters

---

## Setup

### Environment Variables

Both the Communication API and the Adapters require specific environment variables to function correctly. You can find template files in the root directory:

- **Communication API**: `env.communication.example`
- **Adapters**: `env.adapters.example`

Copy these to `.env` in the respective directories (or use them to set your environment) and fill in the required values.

### Install Dependencies

```bash
# Install all dependencies using the pyproject.toml in the root
pip install -e .
```

### Run Locally

**Communication API** (FastAPI):
```bash
# Runs on port 8080 by default
python -m communication.main
```

**Adapters** (FastAPI):
```bash
# Runs on port 8081 by default
python -m adapters.main
```

### Local Development with `local.sh`

For integration testing with the Unity repository, use the `scripts/local.sh` script which provides:

1. **Pub/Sub Emulator** — Local Google Cloud Pub/Sub emulator (no cloud credentials needed)
2. **Adapters Service** — FastAPI server for webhook handling
3. **Automatic Topic Creation** — Creates test topics/subscriptions for test assistants

**Quick Start:**
```bash
# Start with Pub/Sub emulator (recommended for local testing)
./scripts/local.sh start

# Or set environment variables automatically
eval "$(./scripts/local.sh start)"

# Check status
./scripts/local.sh status

# Stop all services
./scripts/local.sh stop
```

**Options:**
```bash
# Start without Pub/Sub emulator (use real GCP Pub/Sub)
./scripts/local.sh start --no-emulator

# Also start the Communication service (for outbound APIs)
./scripts/local.sh start --with-comms
```

**Integration with Local Orchestra:**

When running local orchestra (via Unity's `parallel_run.sh`), set `ORCHESTRA_URL` to point communication services to the local orchestra instance:

```bash
# Unity repo starts local orchestra at http://127.0.0.1:8000/v0
export ORCHESTRA_URL="http://127.0.0.1:8000/v0"

# Communication services will now use local orchestra
./scripts/local.sh start
```

**Environment Variables:**

| Variable | Default | Description |
|----------|---------|-------------|
| `ADAPTERS_PORT` | 8081 | Port for Adapters service |
| `COMMS_PORT` | 8082 | Port for Communication service |
| `PUBSUB_EMULATOR_PORT` | 8085 | Port for Pub/Sub emulator |
| `GCP_PROJECT_ID` | local-test-project | GCP project ID for Pub/Sub topics |
| `GCP_SA_KEY` | — | GCP service account credentials (see note below) |
| `TEST_ASSISTANT_ID` | default-test-assistant | Assistant ID for test topics |
| `ORCHESTRA_URL` | (staging/prod URL) | Orchestra URL (for local orchestra) |
| `ORCHESTRA_ADMIN_KEY` | — | Admin key for Orchestra auth |
| `STAGING` | true | Set topic suffix (-staging) |

> **Important: `GCP_SA_KEY` vs `GOOGLE_APPLICATION_CREDENTIALS`**
>
> This repo uses `GCP_SA_KEY` which contains the **JSON content** of the service account credentials directly (not a file path). This differs from the standard `GOOGLE_APPLICATION_CREDENTIALS` which expects a **file path**.
>
> - `GCP_SA_KEY` = JSON string content → parsed with `json.loads()` → used with `Credentials.from_service_account_info()`
> - `GOOGLE_APPLICATION_CREDENTIALS` = file path → used with `Credentials.from_service_account_file()`
>
> The JSON-content approach simplifies containerized deployments by avoiding file mounting.

**Prerequisites:**
- Python 3.11+
- Google Cloud SDK (`gcloud`) for Pub/Sub emulator
- **Java 7+** (required by Pub/Sub emulator)
  - macOS: `brew install openjdk`
  - Ubuntu: `sudo apt install default-jdk`
- Install emulator components:
  ```bash
  gcloud components install pubsub-emulator beta
  ```

> **Note:** If Java is not installed, `local.sh` will display an error with installation instructions. You can still run `./scripts/local.sh start --no-emulator` to use real GCP Pub/Sub instead.

---

## Adapters (Webhooks)

The Adapters service is a FastAPI application that handles unauthenticated callbacks from external services. It primarily:

- Validates incoming webhooks (e.g., Twilio signatures).
- Resolves the target assistant and user context via Orchestra.
- Publishes events to Pub/Sub for Unity to consume.
- Returns appropriate responses (e.g., TwiML for voice calls).

**Key Endpoints**:

- `POST /twilio/call` – Inbound Twilio voice calls.
- `POST /twilio/sms` – Inbound Twilio SMS messages.
- `POST /twilio/whatsapp` – Inbound Twilio WhatsApp messages.
- `POST /email/gmail` – Google Pub/Sub push notifications for Gmail.
- `POST /email/outlook` – Microsoft Graph notifications for Outlook.
- `POST /chat/teams` – Microsoft Graph notifications for Teams.
- `POST /scheduled/*` – Cron jobs for maintenance (token refresh, watch renewals, etc.).

---

## Communication API (FastAPI)

All JSON endpoints require a valid admin API key via the `auth_admin_key` dependency.  Form-encoded endpoints under `/phone` and `/whatsapp` are unauthenticated and intended for direct Twilio callbacks.

### Phone (`/phone`)

**Unauthenticated (Twilio) Form Endpoints**:

- `POST /phone/recording`    – Twilio recording callback
- `POST /phone/call-status`  – Twilio call status callback (unmute logic)

**Authenticated JSON Endpoints**:

- `POST /phone/dispatch-livekit-agent`
- `POST /phone/send-call`
- `POST /phone/send-text`
- `GET  /phone/available-countries`
- `POST /phone/create`
- `DELETE /phone/delete`
- `POST /phone/hang-up`
- `POST /phone/end-conference`

### WhatsApp (`/whatsapp`)

- `POST /whatsapp/status`        – Twilio status callback (unauth)
- **Authenticated JSON**:
  - `POST /whatsapp/send-text`
  - `POST /whatsapp/send-greeting`
  - `POST /whatsapp/create`
  - `DELETE /whatsapp/delete`
  - `POST /whatsapp/assign`
  - `GET /whatsapp/conflict`

### Gmail (`/gmail`)

**Authenticated JSON Endpoints**:

- `POST /gmail/create`
- `DELETE /gmail/delete`
- `POST /gmail/send`
- `POST /gmail/watch`

### Outlook (`/outlook`)

**Authenticated JSON Endpoints**:

- `POST /outlook/send`
- `POST /outlook/watch`
- `POST /outlook/watch/renew`
- `DELETE /outlook/watch`

### Social Verification (`/social`)

**Authenticated JSON Endpoints**:

- `GET  /social/available-platforms`
- `POST /social/verify`

---

## Running Tests in CI

**Tests are opt-in to reduce GitHub Actions costs.** Tests only run when explicitly requested:

- **Commit message**: Include `[run-tests]` in your commit message
- **PR title**: Include `[run-tests]` in your pull request title
- **Manual trigger**: Use the "Run workflow" button in GitHub Actions

Examples:
```bash
# Run tests on this commit
git commit -m "Fix webhook handler [run-tests]"

# No tests (default)
git commit -m "Update README"
```

Note: The `black` formatting check always runs on every push.

---

## Deployment

Both services are containerized and deployed to **Google Cloud Run**.

- **Communication API**: Build using `Dockerfile-comms` and deploy to the `unity-comms-app` service.
- **Adapters**: Build using `Dockerfile-adapters` and deploy to the `unity-adapters` service.

Deployment is automated via **Google Cloud Build**. See the `cloudbuild/` directory for the build configurations:
- `unity-comms-app.yaml`
- `adapters.yaml`
