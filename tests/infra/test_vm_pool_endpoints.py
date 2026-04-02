import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from communication.infra import views
from communication.infra.models import PoolAssignRequest


class _ImmediateLoop:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def run_in_executor(self, _executor, func):
        future = self._loop.create_future()
        try:
            future.set_result(func())
        except Exception as exc:  # pragma: no cover - defensive test helper
            future.set_exception(exc)
        return future


def _pool_assign_result() -> dict:
    return {
        "vm_name": "unity-pool-ubuntu-3-preview",
        "assistant_id": "1207",
        "ip_address": "10.0.0.1",
        "hostname": "unity-pool-ubuntu-3-preview.vm.unify.ai",
        "desktop_url": "https://unity-pool-ubuntu-3-preview.vm.unify.ai",
        "status": "RUNNING",
        "ssh_username": "unityuser",
        "ssh_port": 22,
    }


@pytest.mark.asyncio
async def test_assign_pool_endpoint_waits_for_authenticated_agent(monkeypatch):
    request = PoolAssignRequest(
        assistant_id="1207",
        unify_apikey="ready-key",
        vm_type="ubuntu",
    )
    current_loop = asyncio.get_running_loop()
    ready_checks = []
    release_pool_vm = MagicMock()
    replenish_pool = MagicMock()

    monkeypatch.setattr(
        views.asyncio,
        "get_running_loop",
        lambda: _ImmediateLoop(current_loop),
    )
    monkeypatch.setattr(
        views, "assign_pool_vm", lambda **_kwargs: _pool_assign_result()
    )
    monkeypatch.setattr(views, "replenish_pool", replenish_pool)
    monkeypatch.setattr(views, "release_pool_vm", release_pool_vm)

    async def _wait_for_agent(hostname: str, api_key: str) -> bool:
        ready_checks.append((hostname, api_key))
        return True

    monkeypatch.setattr(views, "_wait_for_assigned_vm_agent", _wait_for_agent)

    response = await views.assign_pool_endpoint(request)

    assert response.hostname == "unity-pool-ubuntu-3-preview.vm.unify.ai"
    assert ready_checks == [
        ("unity-pool-ubuntu-3-preview.vm.unify.ai", "ready-key"),
    ]
    release_pool_vm.assert_not_called()
    replenish_pool.assert_called_once()


@pytest.mark.asyncio
async def test_assign_pool_endpoint_releases_unready_vm(monkeypatch):
    request = PoolAssignRequest(
        assistant_id="1207",
        unify_apikey="ready-key",
        vm_type="ubuntu",
    )
    current_loop = asyncio.get_running_loop()
    release_pool_vm = MagicMock()
    replenish_pool = MagicMock()

    monkeypatch.setattr(
        views.asyncio,
        "get_running_loop",
        lambda: _ImmediateLoop(current_loop),
    )
    monkeypatch.setattr(
        views, "assign_pool_vm", lambda **_kwargs: _pool_assign_result()
    )
    monkeypatch.setattr(views, "replenish_pool", replenish_pool)
    monkeypatch.setattr(views, "release_pool_vm", release_pool_vm)

    async def _wait_for_agent(_hostname: str, _api_key: str) -> bool:
        return False

    monkeypatch.setattr(views, "_wait_for_assigned_vm_agent", _wait_for_agent)

    with pytest.raises(HTTPException) as exc_info:
        await views.assign_pool_endpoint(request)

    assert exc_info.value.status_code == 503
    release_pool_vm.assert_called_once_with("1207")
    replenish_pool.assert_called_once()
