# Unity Communication Platform

This repository provides a unified communication service with two primary components:

1. **Adapters** – HTTP Cloud Functions (Flask + Functions Framework) that handle unauthenticated, form-encoded webhooks from Twilio (voice, SMS) and Gmail.  They reside in the `adapters/` directory and are deployed as Google Cloud Functions.

2. **Communication API** – A FastAPI application exposing JSON endpoints, protected by an admin API key (`auth_admin_key`), for orchestration by Unity/Orchestra. The code lives in the root and the `communication/` package and is deployed (e.g.) to Cloud Run.

---

## Repository Structure

- `adapters/`
  - `main.py`, `helpers.py`, `requirements.txt`, `__init__.py`
  - Unauthenticated webhook handlers:
    - `twilio_call_webhook` – inbound voice calls (TwiML conference setup + Pub/Sub dispatch)
    - `twilio_msg_webhook`  – inbound SMS messages
    - additional functions for Gmail watch renewal and Pub/Sub notification processing

- `communication/`
  - FastAPI app package with routers under:
    - `phone/`  – Twilio call/SMS form callbacks (unauth) + JSON endpoints for call control, number management, agent dispatch (auth)
    - `email/`    – Google Workspace user management and Gmail send/watch (auth)
    - `social/`    – Verification code generation over SMS (auth)
    - `infra/`     – Infrastructure-related endpoints (e.g. health checks, metrics) (auth)

- `main.py`       – FastAPI entrypoint, mounts routers under `/phone`, `/gmail`, `/outlook`, `/infra`, `/social` with appropriate auth dependencies
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
# Repeat for other entry points (twilio_msg_webhook, etc.)
```

---

## Adapters (Webhooks)

These are unauthenticated HTTP functions that receive form-encoded callbacks and:

- Publish events to Pub/Sub for background processing
- Return TwiML (for voice) or simple acknowledgments

**Key entry points**:

- `twilio_call_webhook`  – `/` (default HTTP trigger) for Twilio voice call events
- `twilio_msg_webhook`   – `/` for Twilio SMS events
- `renew_watch`           – CloudEvent/HTTP for Gmail watch renewal
- `process_notification`  – Pub/Sub handler for Gmail history

---

## Communication API (FastAPI)

All JSON endpoints require a valid admin API key via the `auth_admin_key` dependency.  Form-encoded endpoints under `/phone` are unauthenticated and intended for direct Twilio callbacks.

### Phone (`/phone`)

**Unauthenticated (Twilio) Form Endpoints**:

- `POST /phone/recording`    – Twilio recording callback
- `POST /phone/call-status`  – Twilio call status callback (unmute logic)

**Authenticated JSON Endpoints**:

- `POST /phone/dispatch-agent`
- `POST /phone/send-call`
- `POST /phone/send-text`
- `POST /phone/meet-call`
- `GET  /phone/available-countries`
- `POST /phone/create`
- `DELETE /phone/delete`
- `POST /phone/hang-up`
- `POST /phone/end-conference`

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

## Deployment

- **Adapters**: Deploy individual entry points in `adapters/` as Cloud Functions with the `functions-framework` HTTP trigger.
- **Communication API**: Build and deploy via Cloud Run (or any container platform), ensuring the `PORT` environment variable is respected.

See `.github/workflows` and `cloudbuild/` for CI/CD examples.

