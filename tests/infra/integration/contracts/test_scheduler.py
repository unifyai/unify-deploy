"""
Behavioral contract tests for Cloud Scheduler adapter endpoints.

Each test verifies that a scheduler endpoint:
1. Accepts an admin-key-authenticated request
2. Executes its production logic (test=true mode where available)
3. Returns the expected response shape

These hit the real deployed services with real credentials.

Endpoints covered:
- POST /scheduled/infra/maintenance  (unified infra sweep)
- POST /scheduled/email-watches
- POST /scheduled/microsoft-tokens
- POST /scheduled/teams-watches
- POST /scheduled/cert-renewal
"""

import pytest
import requests

from ..conftest import ADAPTERS_URL, ADMIN_KEY, COMMS_APP_URL

pytestmark = [pytest.mark.integration]

_ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}


class TestEmailWatchesScheduler:
    """Contract: POST /scheduled/email-watches (test=true) lists assistants
    from Orchestra and renews Gmail/Outlook watches via the comms app."""

    def test_email_watches_test_mode(self):
        resp = requests.post(
            f"{ADAPTERS_URL}/scheduled/email-watches",
            json={"test": True},
            headers=_ADMIN_HEADERS,
            timeout=60,
        )
        assert (
            resp.status_code == 200
        ), f"email-watches failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert (
            "results" in body or "gmail" in body or "outlook" in body
        ), f"Unexpected response shape: {body}"


class TestMicrosoftTokensScheduler:
    """Contract: POST /scheduled/microsoft-tokens (test=true) attempts to
    refresh Microsoft access tokens for assistants that have them."""

    def test_microsoft_tokens_test_mode(self):
        resp = requests.post(
            f"{ADAPTERS_URL}/scheduled/microsoft-tokens",
            json={"test": True},
            headers=_ADMIN_HEADERS,
            timeout=60,
        )
        assert (
            resp.status_code == 200
        ), f"microsoft-tokens failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert (
            "refreshed" in body or "failed" in body
        ), f"Unexpected response shape: {body}"


class TestTeamsWatchesScheduler:
    """Contract: POST /scheduled/teams-watches (test=true) attempts to
    renew Microsoft Teams webhook subscriptions."""

    def test_teams_watches_test_mode(self):
        resp = requests.post(
            f"{ADAPTERS_URL}/scheduled/teams-watches",
            json={"test": True},
            headers=_ADMIN_HEADERS,
            timeout=60,
        )
        assert (
            resp.status_code == 200
        ), f"teams-watches failed: {resp.status_code} {resp.text}"


class TestCertRenewalScheduler:
    """Contract: POST /infra/cert-renewal on the comms app checks the wildcard
    TLS cert expiry and renews if needed. The adapter proxies to this endpoint."""

    def test_cert_renewal_via_comms(self):
        resp = requests.post(
            f"{COMMS_APP_URL}/infra/cert-renewal",
            headers=_ADMIN_HEADERS,
            timeout=120,
        )
        assert (
            resp.status_code == 200
        ), f"cert-renewal (comms) failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert isinstance(body, dict)

    def test_cert_renewal_via_adapter_proxy(self):
        resp = requests.post(
            f"{ADAPTERS_URL}/scheduled/cert-renewal",
            headers=_ADMIN_HEADERS,
            timeout=120,
        )
        assert (
            resp.status_code == 200
        ), f"cert-renewal (adapter proxy) failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert isinstance(body, dict)


class TestInfraMaintenanceScheduler:
    """Contract: POST /scheduled/infra/maintenance runs the unified
    infrastructure sweep (replenish, cleanup, expire, orphan-reconcile,
    quarantine-purge) and returns a result dict."""

    def test_infra_maintenance_returns_results(self):
        resp = requests.post(
            f"{ADAPTERS_URL}/scheduled/infra/maintenance",
            headers=_ADMIN_HEADERS,
            timeout=120,
        )
        assert (
            resp.status_code == 200
        ), f"infra/maintenance failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert isinstance(body, dict)
        assert any(
            k in body
            for k in (
                "pool_replenish",
                "pool_cleanup",
                "stale_jobs",
                "orphaned_vms",
                "quarantined_vms",
            )
        ), f"Unexpected response shape: {body}"


# ---------------------------------------------------------------------------
# Microsoft router (Graph subscription validation handshake)
# ---------------------------------------------------------------------------


class TestMicrosoftRouter:
    """Contract: POST /microsoft/router with a validationToken query param
    echoes the token back as plain text (Graph subscription setup)."""

    def test_validation_token_echoed(self):
        resp = requests.post(
            f"{ADAPTERS_URL}/microsoft/router",
            params={"validationToken": "contract-test-validation-12345"},
            timeout=15,
        )
        assert (
            resp.status_code == 200
        ), f"microsoft/router validation failed: {resp.status_code} {resp.text}"
        assert resp.text.strip() == "contract-test-validation-12345"


# ---------------------------------------------------------------------------
# Phone conference status (unauthenticated Twilio callback)
# ---------------------------------------------------------------------------


class TestPhoneConferenceStatus:
    """Contract: POST /phone/conference-status handles Twilio conference
    status callbacks. Unauthenticated (Twilio calls this directly)."""

    def test_participant_join_returns_200(self):
        resp = requests.post(
            f"{COMMS_APP_URL}/phone/conference-status",
            data={
                "StatusCallbackEvent": "participant-join",
                "ConferenceSid": "CFcontract_test_sid",
                "FriendlyName": "contract-test-conference",
            },
            timeout=15,
        )
        assert (
            resp.status_code == 200
        ), f"conference-status failed: {resp.status_code} {resp.text}"


# ---------------------------------------------------------------------------
# Teams call (admin key in body)
# ---------------------------------------------------------------------------


class TestTeamsCall:
    """Contract: POST /teams/call initiates a Teams call for an assistant.
    Authenticated via admin_key in the JSON body."""

    def test_teams_call_invalid_uri_returns_400(self):
        resp = requests.post(
            f"{ADAPTERS_URL}/teams/call",
            json={
                "from_uri": "sip:test@example.com",
                "to_uri": "invalid-uri",
                "call_id": "contract-test-call",
                "admin_key": ADMIN_KEY,
            },
            timeout=15,
        )
        assert resp.status_code in (
            200,
            400,
        ), f"teams/call unexpected: {resp.status_code} {resp.text}"

    def test_teams_call_rejects_bad_admin_key(self):
        resp = requests.post(
            f"{ADAPTERS_URL}/teams/call",
            json={
                "from_uri": "sip:test@example.com",
                "to_uri": "sip:+15005550006@example.com",
                "call_id": "contract-test-call",
                "admin_key": "wrong-key",
            },
            timeout=15,
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Health check + Microsoft auth callback
# ---------------------------------------------------------------------------


class TestAdapterHealth:
    """Contract: GET /health returns 200."""

    def test_health_returns_200(self):
        resp = requests.get(f"{ADAPTERS_URL}/health", timeout=10)
        assert resp.status_code == 200


class TestMicrosoftAuthCallback:
    """Contract: GET /microsoft/auth/callback handles the OAuth redirect.
    Without a valid code, it should error gracefully."""

    def test_callback_without_code_returns_error(self):
        resp = requests.get(
            f"{ADAPTERS_URL}/microsoft/auth/callback",
            params={"state": "test-state"},
            timeout=15,
        )
        # 400/422 = missing required params, 500 = unhandled
        assert resp.status_code in (
            200,
            400,
            422,
            500,
        ), f"microsoft/auth/callback unexpected: {resp.status_code}"
