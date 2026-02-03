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

1. **Adapters** – HTTP Cloud Functions (Flask + Functions Framework) that handle unauthenticated, form-encoded webhooks from Twilio (voice, SMS, WhatsApp) and Gmail.  They reside in the `adapters/` directory and are deployed as Google Cloud Functions.

2. **Communication API** – A FastAPI application exposing JSON endpoints, protected by an admin API key (`auth_admin_key`), for orchestration by Unity/Orchestra. The code lives in the root and the `communication/` package and is deployed (e.g.) to Cloud Run.

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

### Install Dependencies

```bash
pip install -r requirements.txt           # Communication API
pip install -r adapters/requirements.txt  # Adapters
```

### Run Locally

**Communication API** (FastAPI):
```bash
uvicorn main:app --reload --port 8080
```

**Adapters** (Cloud Functions emulator):
```bash
cd adapters
functions-framework --target=twilio_call_webhook --port=8081
# Repeat for other entry points (twilio_msg_webhook, twilio_whatsapp_webhook, etc.)
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
| `PROJECT_ID` | local-test-project | GCP project ID for Pub/Sub topics |
| `TEST_ASSISTANT_ID` | default-test-assistant | Assistant ID for test topics |
| `ORCHESTRA_URL` | (staging/prod URL) | Orchestra URL (for local orchestra) |
| `ORCHESTRA_ADMIN_KEY` | — | Admin key for Orchestra auth |
| `STAGING` | true | Set topic suffix (-staging) |

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

These are unauthenticated HTTP functions that receive form-encoded callbacks and:

- Publish events to Pub/Sub for background processing
- Return TwiML (for voice) or simple acknowledgments

**Key entry points**:

- `twilio_call_webhook`  – `/` (default HTTP trigger) for Twilio voice call events
- `twilio_msg_webhook`   – `/` for Twilio SMS events
- `twilio_whatsapp_webhook`   – `/` for Twilio WhatsApp events
- `renew_watch`           – CloudEvent/HTTP for Gmail watch renewal
- `process_notification`  – Pub/Sub handler for Gmail history

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

- **Adapters**: Deploy individual entry points in `adapters/` as Cloud Functions with the `functions-framework` HTTP trigger.
- **Communication API**: Build and deploy via Cloud Run (or any container platform), ensuring the `PORT` environment variable is respected.

See `.github/workflows` and `cloudbuild/` for CI/CD examples.
