import json
import os
import sys
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import pytest

os.environ["GCP_SA_KEY"] = '{"type": "service_account", "project_id": "test"}'
os.environ["ORCHESTRA_ADMIN_KEY"] = "test-admin-key"
os.environ["GCP_PROJECT_ID"] = "test-project"
os.environ["ORCHESTRA_URL"] = "http://localhost:8000"

_mock_livekit = MagicMock()
_mock_livekit.api = MagicMock()
_mock_livekit.protocol = MagicMock()
_mock_livekit.protocol.sip = MagicMock()
sys.modules["livekit"] = _mock_livekit
sys.modules["livekit.api"] = _mock_livekit.api
sys.modules["livekit.protocol"] = _mock_livekit.protocol
sys.modules["livekit.protocol.sip"] = _mock_livekit.protocol.sip


@pytest.fixture(scope="module")
def app_module():
    for mod in list(sys.modules.keys()):
        if mod.startswith("adapters"):
            del sys.modules[mod]
    from adapters import main

    return main


@pytest.fixture
def client(app_module):
    app_module.SETTINGS.orchestra_admin_key = "test-admin-key"
    test_client = TestClient(app_module.app)
    test_client.headers["Authorization"] = "Bearer test-admin-key"
    return test_client


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


def test_infra_maintenance_calls_terminal_session_prune(client, app_module):
    def _post(url: str, *_args, **_kwargs):
        if url.endswith("/infra/sessions/prune-terminal"):
            return _FakeResponse(
                {
                    "deleted_count": 2,
                    "deleted_assistant_ids": ["100", "101"],
                },
            )
        return _FakeResponse({"ok": True})

    with (
        patch.object(app_module, "SUPPORTED_POOL_VM_TYPES", ("ubuntu",)),
        patch.object(app_module, "replenish_idle_pool", return_value={"ok": True}),
        patch.object(app_module, "cleanup_idle_pool", return_value={"deleted": 0}),
        patch.object(app_module, "expire_all_stale_jobs", return_value={"expired": 0}),
        patch.object(app_module.requests, "post", side_effect=_post) as mock_post,
    ):
        response = client.post("/scheduled/infra/maintenance")

    assert response.status_code == 200
    body = response.json()
    assert body["assistant_session_prune"] == {
        "deleted_count": 2,
        "deleted_assistant_ids": ["100", "101"],
    }
    assert any(
        call.args[0].endswith("/infra/sessions/prune-terminal")
        for call in mock_post.call_args_list
    )


def test_scheduled_jobs_create_passes_extra_demand(client, app_module):
    with patch.object(
        app_module,
        "replenish_idle_pool",
        return_value={"status": "ok"},
    ) as mock_replenish:
        response = client.post("/scheduled/jobs/create", params={"extra_demand": 2})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mock_replenish.assert_called_once_with(refresh=False, extra_demand=2)
