"""
Behavioral contract tests for Cloud Scheduler adapter endpoints.

Each test verifies that a scheduler endpoint:
1. Accepts an admin-key-authenticated request
2. Executes its production logic (test=true mode where available)
3. Returns the expected response shape

These hit the real deployed preview services with real credentials.

Endpoints covered:
- POST /scheduled/email-watches
- POST /scheduled/microsoft-tokens
- POST /scheduled/teams-watches
- POST /scheduled/cert-renewal
- POST /scheduled/pending-startups
- POST /scheduled/jobs/create
- POST /scheduled/jobs/cleanup
- POST /scheduled/jobs/expire-stale
"""

import pytest
import requests

from .conftest import ADAPTERS_URL, ADMIN_KEY

pytestmark = [pytest.mark.staging]

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
    """Contract: POST /scheduled/cert-renewal checks the wildcard TLS cert
    expiry and renews if needed. In normal conditions returns a no-op result.

    Known issue: The adapters container does not include the communication/
    package, so `from communication.infra.cert_renewal import renew_if_needed`
    fails with ModuleNotFoundError. This needs a cloudbuild fix to include
    cert_renewal in the adapters image, or move the endpoint to the comms app.
    """

    @pytest.mark.xfail(
        reason="Adapters image missing communication/ package (ModuleNotFoundError)",
        strict=False,
    )
    def test_cert_renewal_check(self):
        resp = requests.post(
            f"{ADAPTERS_URL}/scheduled/cert-renewal",
            headers=_ADMIN_HEADERS,
            timeout=60,
        )
        assert (
            resp.status_code == 200
        ), f"cert-renewal failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert isinstance(body, dict)


class TestPendingStartupsScheduler:
    """Contract: POST /scheduled/pending-startups forwards to the comms app's
    /infra/pending/process and returns success or a 502 on failure."""

    def test_pending_startups_returns_json(self):
        resp = requests.post(
            f"{ADAPTERS_URL}/scheduled/pending-startups",
            headers=_ADMIN_HEADERS,
            timeout=30,
        )
        assert resp.status_code in (
            200,
            502,
        ), f"pending-startups unexpected status: {resp.status_code} {resp.text}"
        body = resp.json()
        assert isinstance(body, dict)


class TestJobsCreateScheduler:
    """Contract: POST /scheduled/jobs/create replenishes the idle container
    pool and returns creation details."""

    def test_jobs_create_returns_pool_info(self):
        resp = requests.post(
            f"{ADAPTERS_URL}/scheduled/jobs/create",
            headers=_ADMIN_HEADERS,
            timeout=30,
        )
        assert (
            resp.status_code == 200
        ), f"jobs/create failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert "created" in body or "mode" in body, f"Unexpected response shape: {body}"


class TestJobsCleanupScheduler:
    """Contract: POST /scheduled/jobs/cleanup trims excess idle containers
    and returns cleanup details."""

    def test_jobs_cleanup_returns_details(self):
        resp = requests.post(
            f"{ADAPTERS_URL}/scheduled/jobs/cleanup",
            headers=_ADMIN_HEADERS,
            timeout=30,
        )
        assert (
            resp.status_code == 200
        ), f"jobs/cleanup failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert isinstance(body, dict)


class TestStaleJobsExpireScheduler:
    """Contract: POST /scheduled/jobs/expire-stale suspends K8s jobs that
    have been running beyond the max age threshold."""

    def test_stale_jobs_expire_returns_summary(self):
        resp = requests.post(
            f"{ADAPTERS_URL}/scheduled/jobs/expire-stale",
            headers=_ADMIN_HEADERS,
            timeout=30,
        )
        assert (
            resp.status_code == 200
        ), f"expire-stale failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert (
            "total_running" in body or "expired" in body
        ), f"Unexpected response shape: {body}"
