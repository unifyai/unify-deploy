"""
Unit tests for VM readiness logic in vm_helpers.

These tests verify:
- _probe_vm_https returns True on successful HTTPS response, False on failure
- _probe_vm_https treats 4xx as ready (Caddy is up) and 5xx as not ready
- _probe_vm_https handles connection errors and timeouts gracefully
- get_vm_status reports vm_ready=False before the timer expires
- get_vm_status reports vm_ready=False after timer if probe fails
- get_vm_status reports vm_ready=True after timer if probe succeeds
- Timer calculation uses correct offsets for Ubuntu vs Windows
- Timer uses max(creation_ready, start_ready) when both timestamps exist
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import requests

# ---------------------------------------------------------------------------
# _probe_vm_https
# ---------------------------------------------------------------------------


class TestProbeVmHttps:

    @patch("communication.infra.vm_helpers.requests.head")
    def test_returns_true_on_200(self, mock_head):
        from communication.infra.vm_helpers import _probe_vm_https

        mock_head.return_value = MagicMock(status_code=200)
        assert _probe_vm_https("example.vm.unify.ai") is True

    @patch("communication.infra.vm_helpers.requests.head")
    def test_returns_true_on_404(self, mock_head):
        """Caddy returns 404 for unknown paths — still means it's serving."""
        from communication.infra.vm_helpers import _probe_vm_https

        mock_head.return_value = MagicMock(status_code=404)
        assert _probe_vm_https("example.vm.unify.ai") is True

    @patch("communication.infra.vm_helpers.requests.head")
    def test_returns_false_on_500(self, mock_head):
        from communication.infra.vm_helpers import _probe_vm_https

        mock_head.return_value = MagicMock(status_code=500)
        assert _probe_vm_https("example.vm.unify.ai") is False

    @patch("communication.infra.vm_helpers.requests.head")
    def test_returns_false_on_503(self, mock_head):
        from communication.infra.vm_helpers import _probe_vm_https

        mock_head.return_value = MagicMock(status_code=503)
        assert _probe_vm_https("example.vm.unify.ai") is False

    @patch("communication.infra.vm_helpers.requests.head")
    def test_returns_false_on_connection_error(self, mock_head):
        """TCP connection refused — Caddy not listening yet."""
        from communication.infra.vm_helpers import _probe_vm_https

        mock_head.side_effect = requests.ConnectionError("Connection refused")
        assert _probe_vm_https("example.vm.unify.ai") is False

    @patch("communication.infra.vm_helpers.requests.head")
    def test_returns_false_on_timeout(self, mock_head):
        from communication.infra.vm_helpers import _probe_vm_https

        mock_head.side_effect = requests.Timeout("timed out")
        assert _probe_vm_https("example.vm.unify.ai") is False

    @patch("communication.infra.vm_helpers.requests.head")
    def test_returns_false_on_ssl_error(self, mock_head):
        from communication.infra.vm_helpers import _probe_vm_https

        mock_head.side_effect = requests.exceptions.SSLError("SSL handshake failed")
        assert _probe_vm_https("example.vm.unify.ai") is False

    @patch("communication.infra.vm_helpers.requests.head")
    def test_uses_verify_false(self, mock_head):
        """Probe uses verify=False so it passes as soon as Caddy is listening,
        even during the ACME window with a temporary self-signed cert."""
        from communication.infra.vm_helpers import _probe_vm_https

        mock_head.return_value = MagicMock(status_code=200)
        _probe_vm_https("example.vm.unify.ai", timeout=3.0)
        mock_head.assert_called_once_with(
            "https://example.vm.unify.ai/",
            timeout=3.0,
            verify=False,
        )

    @patch("communication.infra.vm_helpers.requests.head")
    def test_does_not_leak_global_warning_suppression(self, mock_head):
        """InsecureRequestWarning suppression must be scoped to the probe call,
        not leaked to the rest of the process."""
        import warnings
        from communication.infra.vm_helpers import _probe_vm_https

        mock_head.return_value = MagicMock(status_code=200)
        _probe_vm_https("example.vm.unify.ai")

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            warnings.warn(
                "test",
                requests.packages.urllib3.exceptions.InsecureRequestWarning,
            )
            assert len(w) == 1


# ---------------------------------------------------------------------------
# get_vm_status readiness logic
# ---------------------------------------------------------------------------


def _make_instance(
    creation_ts: datetime,
    last_start_ts: datetime | None = None,
    external_ip: str = "1.2.3.4",
    status: str = "RUNNING",
):
    """Build a mock GCE Instance object."""
    inst = MagicMock()
    inst.status = status
    inst.creation_timestamp = creation_ts.isoformat()
    inst.last_start_timestamp = last_start_ts.isoformat() if last_start_ts else ""
    inst.machine_type = "zones/us-central1-a/machineTypes/e2-standard-2"

    # Network interface with external IP
    ac = MagicMock()
    ac.nat_i_p = external_ip
    ni = MagicMock()
    ni.access_configs = [ac]
    inst.network_interfaces = [ni]

    return inst


class TestGetVmStatusReadiness:

    @patch("communication.infra.vm_helpers._probe_vm_https")
    @patch("communication.infra.vm_helpers.compute_v1.InstancesClient")
    def test_not_ready_before_timer_expires(self, mock_client_cls, mock_probe):
        from communication.infra.vm_helpers import get_vm_status

        now = datetime.now(timezone.utc)
        # Created 1 minute ago — Ubuntu needs 4 minutes
        inst = _make_instance(creation_ts=now - timedelta(minutes=1))
        mock_client_cls.return_value.get.return_value = inst

        result = get_vm_status("533", vm_type="ubuntu")

        assert result["vm_ready"] is False
        # Probe should not be called when timer hasn't expired
        mock_probe.assert_not_called()

    @patch("communication.infra.vm_helpers._probe_vm_https", return_value=False)
    @patch("communication.infra.vm_helpers.compute_v1.InstancesClient")
    def test_not_ready_after_timer_if_probe_fails(self, mock_client_cls, mock_probe):
        from communication.infra.vm_helpers import get_vm_status

        now = datetime.now(timezone.utc)
        # Created 10 minutes ago — timer definitely expired
        inst = _make_instance(creation_ts=now - timedelta(minutes=10))
        mock_client_cls.return_value.get.return_value = inst

        result = get_vm_status("533", vm_type="ubuntu")

        assert result["vm_ready"] is False
        mock_probe.assert_called_once()

    @patch("communication.infra.vm_helpers._probe_vm_https", return_value=True)
    @patch("communication.infra.vm_helpers.compute_v1.InstancesClient")
    def test_ready_after_timer_if_probe_succeeds(self, mock_client_cls, mock_probe):
        from communication.infra.vm_helpers import get_vm_status

        now = datetime.now(timezone.utc)
        inst = _make_instance(creation_ts=now - timedelta(minutes=10))
        mock_client_cls.return_value.get.return_value = inst

        result = get_vm_status("533", vm_type="ubuntu")

        assert result["vm_ready"] is True
        mock_probe.assert_called_once()

    @patch("communication.infra.vm_helpers._probe_vm_https", return_value=True)
    @patch("communication.infra.vm_helpers.compute_v1.InstancesClient")
    def test_desktop_url_populated_when_ready(self, mock_client_cls, mock_probe):
        from communication.infra.vm_helpers import get_vm_status

        now = datetime.now(timezone.utc)
        inst = _make_instance(
            creation_ts=now - timedelta(minutes=10),
            external_ip="104.198.191.93",
        )
        mock_client_cls.return_value.get.return_value = inst

        result = get_vm_status("533", vm_type="ubuntu")

        assert result["desktop_url"] is not None
        assert "533" in result["desktop_url"]

    @patch("communication.infra.vm_helpers._probe_vm_https")
    @patch("communication.infra.vm_helpers.compute_v1.InstancesClient")
    def test_not_ready_when_no_external_ip(self, mock_client_cls, mock_probe):
        from communication.infra.vm_helpers import get_vm_status

        now = datetime.now(timezone.utc)
        inst = _make_instance(
            creation_ts=now - timedelta(minutes=10),
            external_ip=None,
        )
        # Override network interfaces for no external IP
        ni = MagicMock()
        ni.access_configs = []
        inst.network_interfaces = [ni]
        mock_client_cls.return_value.get.return_value = inst

        result = get_vm_status("533", vm_type="ubuntu")

        assert result["vm_ready"] is False
        mock_probe.assert_not_called()


# ---------------------------------------------------------------------------
# Timer calculation
# ---------------------------------------------------------------------------


class TestTimerCalculation:

    @patch("communication.infra.vm_helpers._probe_vm_https", return_value=True)
    @patch("communication.infra.vm_helpers.compute_v1.InstancesClient")
    def test_ubuntu_creation_wait_is_4_minutes(self, mock_client_cls, mock_probe):
        from communication.infra.vm_helpers import get_vm_status

        now = datetime.now(timezone.utc)
        # Created 3 minutes ago — should NOT be ready (need 4 min)
        inst = _make_instance(creation_ts=now - timedelta(minutes=3))
        mock_client_cls.return_value.get.return_value = inst

        result = get_vm_status("533", vm_type="ubuntu")
        assert result["vm_ready"] is False

    @patch("communication.infra.vm_helpers._probe_vm_https", return_value=True)
    @patch("communication.infra.vm_helpers.compute_v1.InstancesClient")
    def test_windows_creation_wait_is_8_minutes(self, mock_client_cls, mock_probe):
        from communication.infra.vm_helpers import get_vm_status

        now = datetime.now(timezone.utc)
        # Created 5 minutes ago — should NOT be ready for Windows (need 8 min)
        inst = _make_instance(creation_ts=now - timedelta(minutes=5))
        mock_client_cls.return_value.get.return_value = inst

        result = get_vm_status("533", vm_type="windows")
        assert result["vm_ready"] is False

    @patch("communication.infra.vm_helpers._probe_vm_https", return_value=True)
    @patch("communication.infra.vm_helpers.compute_v1.InstancesClient")
    def test_ubuntu_start_wait_is_60_seconds(self, mock_client_cls, mock_probe):
        from communication.infra.vm_helpers import get_vm_status

        now = datetime.now(timezone.utc)
        # Created long ago, last started 30 seconds ago — not ready (need 60s)
        inst = _make_instance(
            creation_ts=now - timedelta(hours=1),
            last_start_ts=now - timedelta(seconds=30),
        )
        mock_client_cls.return_value.get.return_value = inst

        result = get_vm_status("533", vm_type="ubuntu")
        assert result["vm_ready"] is False

    @patch("communication.infra.vm_helpers._probe_vm_https", return_value=True)
    @patch("communication.infra.vm_helpers.compute_v1.InstancesClient")
    def test_windows_start_wait_is_90_seconds(self, mock_client_cls, mock_probe):
        from communication.infra.vm_helpers import get_vm_status

        now = datetime.now(timezone.utc)
        # Created long ago, last started 70 seconds ago — not ready for Windows (need 90s)
        inst = _make_instance(
            creation_ts=now - timedelta(hours=1),
            last_start_ts=now - timedelta(seconds=70),
        )
        mock_client_cls.return_value.get.return_value = inst

        result = get_vm_status("533", vm_type="windows")
        assert result["vm_ready"] is False

    @patch("communication.infra.vm_helpers._probe_vm_https", return_value=True)
    @patch("communication.infra.vm_helpers.compute_v1.InstancesClient")
    def test_uses_max_of_creation_and_start_ready(self, mock_client_cls, mock_probe):
        """If the VM was just started but creation_ready is still in the future
        (impossible in practice, but tests the max() logic), creation_ready wins."""
        from communication.infra.vm_helpers import get_vm_status

        now = datetime.now(timezone.utc)
        # Created 2 minutes ago (creation_ready = now + 2min for Ubuntu)
        # Last started 5 minutes ago (start_ready = now - 4min)
        # max(creation_ready, start_ready) = creation_ready → not ready
        inst = _make_instance(
            creation_ts=now - timedelta(minutes=2),
            last_start_ts=now - timedelta(minutes=5),
        )
        mock_client_cls.return_value.get.return_value = inst

        result = get_vm_status("533", vm_type="ubuntu")
        assert result["vm_ready"] is False

    @patch("communication.infra.vm_helpers._probe_vm_https", return_value=True)
    @patch("communication.infra.vm_helpers.compute_v1.InstancesClient")
    def test_ready_when_both_timers_expired_and_probe_passes(
        self,
        mock_client_cls,
        mock_probe,
    ):
        from communication.infra.vm_helpers import get_vm_status

        now = datetime.now(timezone.utc)
        inst = _make_instance(
            creation_ts=now - timedelta(hours=1),
            last_start_ts=now - timedelta(minutes=10),
        )
        mock_client_cls.return_value.get.return_value = inst

        result = get_vm_status("533", vm_type="ubuntu")
        assert result["vm_ready"] is True

    @patch("communication.infra.vm_helpers.compute_v1.InstancesClient")
    def test_returns_none_for_missing_vm(self, mock_client_cls):
        from communication.infra.vm_helpers import get_vm_status
        from google.api_core.exceptions import NotFound

        mock_client_cls.return_value.get.side_effect = NotFound("not found")

        result = get_vm_status("999", vm_type="ubuntu")
        assert result is None


# ---------------------------------------------------------------------------
# /infra/vm/ready endpoint — HTTPS probe gate
# ---------------------------------------------------------------------------


import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def vm_ready_client():
    """Test client wired to the tunnel_router (hosts /vm/ready)."""
    from fastapi import FastAPI
    from communication.infra.views import tunnel_router

    app = FastAPI()
    app.include_router(tunnel_router, prefix="/infra")
    return TestClient(app)


_VM_READY_URL = "/infra/vm/ready"
_AUTH_HEADER = {"Authorization": "Bearer test-key"}
_VALID_BODY = {"assistant_id": "564", "vm_type": "windows"}


class TestVmReadyEndpointProbe:
    """The /vm/ready endpoint must probe HTTPS before publishing the event."""

    @patch.dict(
        "os.environ",
        {"GCP_SA_KEY": '{"type":"service_account","project_id":"test"}'},
    )
    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch("communication.infra.views._probe_vm_https", return_value=True)
    @patch("communication.infra.views.authenticate_user_api_key")
    @patch("communication.infra.views.extract_api_key", return_value="test-key")
    def test_publishes_when_probe_succeeds(
        self,
        _mock_extract,
        _mock_auth,
        mock_probe,
        mock_publisher_cls,
        _mock_creds,
    ):
        from communication.infra.vm_helpers import get_dns_hostname

        mock_future = MagicMock()
        mock_future.result.return_value = "msg-123"
        mock_publisher_cls.return_value.publish.return_value = mock_future
        mock_publisher_cls.return_value.topic_path.return_value = "projects/p/topics/t"

        from fastapi import FastAPI
        from communication.infra.views import tunnel_router

        app = FastAPI()
        app.include_router(tunnel_router, prefix="/infra")
        client = TestClient(app)

        resp = client.post(_VM_READY_URL, json=_VALID_BODY, headers=_AUTH_HEADER)

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["assistant_id"] == "564"
        mock_probe.assert_called_once_with(get_dns_hostname("564"))
        mock_publisher_cls.return_value.publish.assert_called_once()

    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch("communication.infra.views._probe_vm_https", return_value=False)
    @patch("communication.infra.views.authenticate_user_api_key")
    @patch("communication.infra.views.extract_api_key", return_value="test-key")
    def test_returns_503_when_probe_fails(
        self,
        _mock_extract,
        _mock_auth,
        mock_probe,
        mock_publisher_cls,
    ):
        from fastapi import FastAPI
        from communication.infra.views import tunnel_router

        app = FastAPI()
        app.include_router(tunnel_router, prefix="/infra")
        client = TestClient(app)

        resp = client.post(_VM_READY_URL, json=_VALID_BODY, headers=_AUTH_HEADER)

        assert resp.status_code == 503
        assert "not reachable" in resp.json()["detail"].lower()
        mock_probe.assert_called_once()
        mock_publisher_cls.return_value.publish.assert_not_called()

    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch("communication.infra.views._probe_vm_https", return_value=False)
    @patch("communication.infra.views.authenticate_user_api_key")
    @patch("communication.infra.views.extract_api_key", return_value="test-key")
    def test_probe_failure_does_not_publish_event(
        self,
        _mock_extract,
        _mock_auth,
        mock_probe,
        mock_publisher_cls,
    ):
        """Specifically verify no Pub/Sub message escapes on probe failure."""
        from fastapi import FastAPI
        from communication.infra.views import tunnel_router

        app = FastAPI()
        app.include_router(tunnel_router, prefix="/infra")
        client = TestClient(app)

        client.post(_VM_READY_URL, json=_VALID_BODY, headers=_AUTH_HEADER)

        mock_publisher_cls.return_value.publish.assert_not_called()
        mock_publisher_cls.return_value.topic_path.assert_not_called()
