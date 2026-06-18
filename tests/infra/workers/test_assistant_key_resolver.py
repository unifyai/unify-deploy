"""Unit tests for :mod:`unity_deploy.infra.workers.assistant_key_resolver`.

Covers:

- Assistant-scoped path (current FM + DM bindings) hits
  ``GET /v0/admin/assistant`` with the right Bearer auth and query
  string, and returns the ``api_key`` field from the first element of
  ``info``.
- ``orchestra_url`` normalisation: both with and without the ``/v0``
  suffix resolve to the same request URL.
- In-process LRU+TTL cache: a second lookup for the same
  ``(user_id, assistant_id)`` reuses the first response; a lookup
  after the TTL expires hits the backend again.
- Error propagation: non-2xx responses, transport failures, empty
  payloads, missing api_key fields, and empty SETTINGS all raise
  :class:`AssistantKeyLookupError` with the binding identity attached.

These are pure symbolic tests -- no real Orchestra is involved. The
httpx transport is stubbed via :class:`httpx.MockTransport`, and
``SETTINGS.ORCHESTRA_URL`` / ``SETTINGS.ORCHESTRA_ADMIN_KEY`` are
patched per-test.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from droid.common.pipeline.types import DmBinding, FmBinding, IngestBinding
from droid.settings import SETTINGS
from unity_deploy.infra.workers.assistant_key_resolver import (
    AssistantKeyLookupError,
    clear_cache,
    resolve_api_key,
)

ORCHESTRA_URL = "https://orchestra.test"
ADMIN_KEY = "admin-token"


@pytest.fixture(autouse=True)
def _settings_populated(monkeypatch):
    """Populate SETTINGS.ORCHESTRA_URL / ADMIN_KEY on the global singleton.

    The resolver reads these directly from :data:`SETTINGS`, so we
    patch the singleton's attributes for the duration of each test
    rather than threading params through the call sites. Test isolation
    is preserved by ``monkeypatch``'s automatic restore.
    """
    monkeypatch.setattr(SETTINGS, "ORCHESTRA_URL", ORCHESTRA_URL)
    monkeypatch.setattr(SETTINGS, "ORCHESTRA_ADMIN_KEY", SecretStr(ADMIN_KEY))


@pytest.fixture(autouse=True)
def _clear_resolver_cache():
    """Every test starts from a cold cache so ordering cannot leak state."""
    clear_cache()
    yield
    clear_cache()


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# FM path (assistant_id set -> GET /admin/assistant)
# ---------------------------------------------------------------------------


class TestFmPath:
    @pytest.mark.asyncio
    async def test_returns_api_key_from_assistant_response(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(
                200,
                json={
                    "info": [
                        {
                            "agent_id": "42",
                            "api_key": "unify-live-ABC",
                            "email": "a@b",
                        },
                    ],
                },
            )

        binding = FmBinding(
            user_id="alice",
            assistant_id="42",
            logical_path="x.csv",
        )
        async with _mock_client(handler) as client:
            key = await resolve_api_key(binding, http_client=client)

        assert key == "unify-live-ABC"
        assert captured["auth"] == f"Bearer {ADMIN_KEY}"
        assert "admin/assistant" in captured["url"]
        assert "agent_id=42" in captured["url"]

    @pytest.mark.asyncio
    async def test_normalises_orchestra_url_without_v0_suffix(
        self,
        monkeypatch,
    ) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"info": [{"api_key": "k"}]})

        monkeypatch.setattr(SETTINGS, "ORCHESTRA_URL", "https://orchestra.test")
        binding = FmBinding(user_id="u", assistant_id="1", logical_path="x")
        async with _mock_client(handler) as client:
            await resolve_api_key(binding, http_client=client)

        assert "/v0/admin/assistant" in captured["url"]

    @pytest.mark.asyncio
    async def test_normalises_orchestra_url_with_v0_suffix(
        self,
        monkeypatch,
    ) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"info": [{"api_key": "k"}]})

        monkeypatch.setattr(
            SETTINGS,
            "ORCHESTRA_URL",
            "https://orchestra.test/v0",
        )
        binding = FmBinding(user_id="u", assistant_id="1", logical_path="x")
        async with _mock_client(handler) as client:
            await resolve_api_key(binding, http_client=client)

        # No double "/v0/v0/" prefix.
        assert captured["url"].count("/v0/") == 1


# ---------------------------------------------------------------------------
# DM bindings are assistant-scoped too.
# ---------------------------------------------------------------------------


class TestDmBindingAssistantScoped:
    @pytest.mark.asyncio
    async def test_uses_assistant_endpoint_when_assistant_id_present(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"info": [{"api_key": "assistant-key"}]})

        binding = DmBinding(
            user_id="alice",
            assistant_id="42",
            target_context="Orders",
        )
        async with _mock_client(handler) as client:
            key = await resolve_api_key(binding, http_client=client)

        assert key == "assistant-key"
        assert "admin/assistant" in captured["url"]
        assert "agent_id=42" in captured["url"]


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


class TestCache:
    @pytest.mark.asyncio
    async def test_second_lookup_hits_cache(self) -> None:
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(200, json={"info": [{"api_key": "cached-key"}]})

        binding = FmBinding(user_id="alice", assistant_id="42", logical_path="x")
        async with _mock_client(handler) as client:
            k1 = await resolve_api_key(binding, http_client=client)
            k2 = await resolve_api_key(binding, http_client=client)

        assert k1 == k2 == "cached-key"
        assert call_count["n"] == 1

    @pytest.mark.asyncio
    async def test_distinct_bindings_do_not_share_cache_entries(self) -> None:
        call_log: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            call_log.append(url)
            return httpx.Response(
                200,
                json={"info": [{"api_key": f"key-{url[-2:]}"}]},
            )

        fm = FmBinding(user_id="alice", assistant_id="42", logical_path="x")
        dm = DmBinding(user_id="alice", assistant_id="77", target_context="ctx")
        async with _mock_client(handler) as client:
            fm_key = await resolve_api_key(fm, http_client=client)
            dm_key = await resolve_api_key(dm, http_client=client)

        assert fm_key.startswith("key-")
        assert dm_key.startswith("key-")
        assert len(call_log) == 2

    @pytest.mark.asyncio
    async def test_cache_entry_expires_after_ttl(self, monkeypatch) -> None:
        """TTL expiry reissues the backend request.

        We monkey-patch ``time.monotonic`` inside the resolver module
        to simulate the 5-minute TTL elapsing without sleeping.
        """
        from unity_deploy.infra.workers import assistant_key_resolver

        fake_now = {"t": 1000.0}
        monkeypatch.setattr(
            assistant_key_resolver.time,
            "monotonic",
            lambda: fake_now["t"],
        )

        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(
                200,
                json={"info": [{"api_key": f"k-{call_count['n']}"}]},
            )

        binding = FmBinding(user_id="a", assistant_id="1", logical_path="x")
        async with _mock_client(handler) as client:
            k1 = await resolve_api_key(binding, http_client=client)
            # Jump forward past the 5-minute TTL.
            fake_now["t"] += 10_000.0
            k2 = await resolve_api_key(binding, http_client=client)

        assert k1 == "k-1"
        assert k2 == "k-2"
        assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


class TestErrors:
    @pytest.mark.asyncio
    async def test_non_2xx_raises_lookup_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        binding = FmBinding(user_id="a", assistant_id="1", logical_path="x")
        async with _mock_client(handler) as client:
            with pytest.raises(AssistantKeyLookupError) as exc_info:
                await resolve_api_key(binding, http_client=client)

        err = exc_info.value
        assert err.user_id == "a"
        assert err.assistant_id == "1"
        assert "500" in str(err)

    @pytest.mark.asyncio
    async def test_transport_error_raises_lookup_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host", request=request)

        binding = FmBinding(user_id="a", assistant_id="1", logical_path="x")
        async with _mock_client(handler) as client:
            with pytest.raises(AssistantKeyLookupError) as exc_info:
                await resolve_api_key(binding, http_client=client)

        err = exc_info.value
        assert err.user_id == "a"
        assert err.assistant_id == "1"

    @pytest.mark.asyncio
    async def test_empty_assistant_info_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"info": []})

        binding = FmBinding(user_id="a", assistant_id="1", logical_path="x")
        async with _mock_client(handler) as client:
            with pytest.raises(AssistantKeyLookupError, match="No assistant found"):
                await resolve_api_key(binding, http_client=client)

    @pytest.mark.asyncio
    async def test_missing_api_key_field_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"info": [{"agent_id": "1"}]})

        binding = FmBinding(user_id="a", assistant_id="1", logical_path="x")
        async with _mock_client(handler) as client:
            with pytest.raises(AssistantKeyLookupError, match="no api_key"):
                await resolve_api_key(binding, http_client=client)

    @pytest.mark.asyncio
    async def test_parent_binding_rejects_missing_assistant_id(self) -> None:
        with pytest.raises(ValidationError):
            IngestBinding(user_id="nobody")  # type: ignore[call-arg]

    @pytest.mark.asyncio
    async def test_non_json_body_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"<html>not json</html>",
                headers={"content-type": "text/html"},
            )

        binding = FmBinding(user_id="a", assistant_id="1", logical_path="x")
        async with _mock_client(handler) as client:
            with pytest.raises(AssistantKeyLookupError, match="Non-JSON"):
                await resolve_api_key(binding, http_client=client)

    @pytest.mark.asyncio
    async def test_missing_orchestra_url_raises(self, monkeypatch) -> None:
        """Empty ``SETTINGS.ORCHESTRA_URL`` is a misconfiguration, not a crash.

        Workers surface it as :class:`AssistantKeyLookupError` so the
        message is nacked for redelivery once the operator fixes the
        pod env, rather than being silently dropped.
        """
        monkeypatch.setattr(SETTINGS, "ORCHESTRA_URL", "")
        binding = FmBinding(user_id="a", assistant_id="1", logical_path="x")
        with pytest.raises(AssistantKeyLookupError, match="ORCHESTRA_URL"):
            await resolve_api_key(binding)

    @pytest.mark.asyncio
    async def test_missing_admin_key_raises(self, monkeypatch) -> None:
        monkeypatch.setattr(SETTINGS, "ORCHESTRA_ADMIN_KEY", SecretStr(""))
        binding = FmBinding(user_id="a", assistant_id="1", logical_path="x")
        with pytest.raises(AssistantKeyLookupError, match="ORCHESTRA_ADMIN_KEY"):
            await resolve_api_key(binding)
