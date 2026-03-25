"""
Behavioral contract tests for the tunnel relay service.

Tests the full tunnel lifecycle (register → status → list → delete) against
the real deployed comms app. Authenticated via user API key (not admin key),
which exercises the authenticate_user_api_key dependency and Orchestra
user lookup.

Exercises:
- SETTINGS.tunnel_gcs_bucket (GCS state storage)
- SETTINGS.tunnel_subdomain (public URL generation)
- authenticate_user_api_key (Orchestra /user/basic-info)
- tunnel_helpers.py (register_tunnel, get_tunnel_status, list_user_tunnels,
  unregister_tunnel)
"""

import pytest
import requests

from .conftest import COMMS_APP_URL, UNIFY_KEY

pytestmark = [pytest.mark.staging]


@pytest.fixture(scope="module")
def user_headers():
    """Authorization headers using the user's Unify API key."""
    if not UNIFY_KEY:
        pytest.skip("UNIFY_KEY required for tunnel tests")
    return {"Authorization": f"Bearer {UNIFY_KEY}"}


class TestTunnelLifecycle:
    """Contract: The tunnel CRUD endpoints form a consistent lifecycle.

    register → returns tunnel_id, public_url, client_config
    status   → returns current tunnel state for the owner
    list     → includes the registered tunnel
    delete   → removes the tunnel, subsequent status returns 404
    """

    def test_register_status_list_delete(self, user_headers):
        tunnel_id = None
        try:
            # Register
            reg_resp = requests.post(
                f"{COMMS_APP_URL}/infra/tunnel/register",
                json={"local_port": 8080, "name": "contract-test-tunnel"},
                headers=user_headers,
                timeout=30,
            )
            assert (
                reg_resp.status_code == 200
            ), f"tunnel/register failed: {reg_resp.status_code} {reg_resp.text}"
            reg_body = reg_resp.json()
            tunnel_id = reg_body["tunnel_id"]
            assert tunnel_id
            assert "hostname" in reg_body or "public_url" in reg_body
            assert "client_config" in reg_body

            # Status
            status_resp = requests.get(
                f"{COMMS_APP_URL}/infra/tunnel/{tunnel_id}",
                headers=user_headers,
                timeout=15,
            )
            assert (
                status_resp.status_code == 200
            ), f"tunnel status failed: {status_resp.status_code} {status_resp.text}"
            status_body = status_resp.json()
            assert status_body["tunnel_id"] == tunnel_id

            # List
            list_resp = requests.get(
                f"{COMMS_APP_URL}/infra/tunnels",
                headers=user_headers,
                timeout=15,
            )
            assert list_resp.status_code == 200
            list_body = list_resp.json()
            tunnel_ids = [t["tunnel_id"] for t in list_body.get("tunnels", [])]
            assert (
                tunnel_id in tunnel_ids
            ), f"Registered tunnel {tunnel_id} not found in list: {tunnel_ids}"

            # Delete
            del_resp = requests.delete(
                f"{COMMS_APP_URL}/infra/tunnel/{tunnel_id}",
                headers=user_headers,
                timeout=15,
            )
            assert del_resp.status_code == 200
            del_body = del_resp.json()
            assert del_body["deleted"] is True
            tunnel_id = None  # Already cleaned up

            # Verify gone
            gone_resp = requests.get(
                f"{COMMS_APP_URL}/infra/tunnel/{del_body['tunnel_id']}",
                headers=user_headers,
                timeout=15,
            )
            assert gone_resp.status_code == 404

        finally:
            if tunnel_id:
                requests.delete(
                    f"{COMMS_APP_URL}/infra/tunnel/{tunnel_id}",
                    headers=user_headers,
                    timeout=10,
                )


class TestTunnelAuth:
    """Contract: Tunnel endpoints reject unauthenticated requests."""

    def test_register_rejects_no_auth(self):
        resp = requests.post(
            f"{COMMS_APP_URL}/infra/tunnel/register",
            json={"local_port": 8080},
            timeout=15,
        )
        assert resp.status_code in (
            401,
            403,
            422,
        ), f"Expected auth error, got {resp.status_code}"

    def test_list_rejects_bad_key(self):
        resp = requests.get(
            f"{COMMS_APP_URL}/infra/tunnels",
            headers={"Authorization": "Bearer invalid-key-12345"},
            timeout=15,
        )
        assert resp.status_code in (
            401,
            403,
        ), f"Expected auth error for bad key, got {resp.status_code}"
