"""Tests for the Employment Hero package's OAuth refresh + cache resolver
in functions/_client.py.

The resolver has two paths:

1. OAuth: ``CLIENT_ID``/``CLIENT_SECRET``/``REFRESH_TOKEN`` are set -> POST
   ``oauth2/token`` with ``grant_type=refresh_token``, cache the result
   for ``expires_in - 300`` seconds.
2. Not connected: structured envelope listing the missing secret names.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions import (
    _client,
)


@pytest.fixture(autouse=True)
def _isolate_token_cache():
    """Each test runs against an empty token cache."""
    _client._TOKEN_CACHE.clear()
    yield
    _client._TOKEN_CACHE.clear()


@pytest.fixture
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """Strip every EH env var so each test starts from a known baseline."""
    for key in (
        "EMPLOYMENTHERO_OAUTH_CLIENT_ID",
        "EMPLOYMENTHERO_OAUTH_CLIENT_SECRET",
        "EMPLOYMENTHERO_REFRESH_TOKEN",
        "EMPLOYMENTHERO_BASE_URL",
        "EMPLOYMENTHERO_OAUTH_TOKEN_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# Path 2: not connected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_connected_when_no_secrets(_clean_env):
    token, err = await _client._resolve_access_token()
    assert token is None
    assert err is not None
    assert "not connected" in err["error"].lower()
    assert set(err["missing_secrets"]) == {
        "EMPLOYMENTHERO_OAUTH_CLIENT_ID",
        "EMPLOYMENTHERO_OAUTH_CLIENT_SECRET",
        "EMPLOYMENTHERO_REFRESH_TOKEN",
    }


@pytest.mark.asyncio
async def test_not_connected_lists_only_missing_secrets(_clean_env):
    _clean_env.setenv("EMPLOYMENTHERO_OAUTH_CLIENT_ID", "cid")
    token, err = await _client._resolve_access_token()
    assert token is None
    assert err is not None
    assert set(err["missing_secrets"]) == {
        "EMPLOYMENTHERO_OAUTH_CLIENT_SECRET",
        "EMPLOYMENTHERO_REFRESH_TOKEN",
    }


# ---------------------------------------------------------------------------
# Path 1: OAuth refresh
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_body or {}
        self.text = text
        self.content = (text or str(json_body)).encode() if (text or json_body) else b""

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Stub for httpx.AsyncClient that captures POST args and returns a queued response."""

    def __init__(self, *, queued: list[_FakeResponse], **_kwargs):
        self._queued = queued
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, *, data=None, headers=None, json=None):
        self.calls.append({"method": "POST", "url": url, "data": data, "json": json})
        return self._queued.pop(0)


def _patch_httpx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    queued: list[_FakeResponse],
) -> _FakeAsyncClient:
    fake = _FakeAsyncClient(queued=queued)

    def _factory(*args, **kwargs):
        return fake

    monkeypatch.setattr(
        _client.httpx if hasattr(_client, "httpx") else "httpx.AsyncClient",
        _factory,
        raising=False,
    )
    # _client.py imports httpx inside function bodies, so patch at the module
    # level the function actually references.
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    return fake


@pytest.mark.asyncio
async def test_oauth_refresh_happy_path_caches_token(_clean_env, monkeypatch):
    _clean_env.setenv("EMPLOYMENTHERO_OAUTH_CLIENT_ID", "cid")
    _clean_env.setenv("EMPLOYMENTHERO_OAUTH_CLIENT_SECRET", "csec")
    _clean_env.setenv("EMPLOYMENTHERO_REFRESH_TOKEN", "rt")

    fake = _patch_httpx(
        monkeypatch,
        queued=[
            _FakeResponse(
                200,
                json_body={
                    "access_token": "new-access",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            ),
        ],
    )

    token, err = await _client._resolve_access_token()
    assert token == "new-access"
    assert err is None

    # Cache populated.
    assert ("cid", "rt") in _client._TOKEN_CACHE
    cached_token, expires_at = _client._TOKEN_CACHE[("cid", "rt")]
    assert cached_token == "new-access"
    # Cache TTL = expires_in (3600) - leeway (300) = 3300s in the future.
    assert expires_at > time.time() + 3000
    assert expires_at <= time.time() + 3600

    # POST was made with the right form payload.
    call = fake.calls[0]
    assert call["url"].endswith("/oauth2/token")
    assert call["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "rt",
        "client_id": "cid",
        "client_secret": "csec",
    }


@pytest.mark.asyncio
async def test_oauth_refresh_cache_hit_avoids_http(_clean_env, monkeypatch):
    _clean_env.setenv("EMPLOYMENTHERO_OAUTH_CLIENT_ID", "cid")
    _clean_env.setenv("EMPLOYMENTHERO_OAUTH_CLIENT_SECRET", "csec")
    _clean_env.setenv("EMPLOYMENTHERO_REFRESH_TOKEN", "rt")

    # Pre-populate the cache with a far-future expiry.
    _client._TOKEN_CACHE[("cid", "rt")] = ("cached-tok", time.time() + 1000)

    # If the resolver tries to make an HTTP call, this empty queue
    # would IndexError on .pop() — so the assertion is implicit.
    fake = _patch_httpx(monkeypatch, queued=[])

    token, err = await _client._resolve_access_token()
    assert token == "cached-tok"
    assert err is None
    assert fake.calls == []


@pytest.mark.asyncio
async def test_oauth_refresh_expired_cache_re_resolves(_clean_env, monkeypatch):
    _clean_env.setenv("EMPLOYMENTHERO_OAUTH_CLIENT_ID", "cid")
    _clean_env.setenv("EMPLOYMENTHERO_OAUTH_CLIENT_SECRET", "csec")
    _clean_env.setenv("EMPLOYMENTHERO_REFRESH_TOKEN", "rt")

    # Cache with a past expiry — must be ignored, fresh refresh issued.
    _client._TOKEN_CACHE[("cid", "rt")] = ("stale-tok", time.time() - 10)

    _patch_httpx(
        monkeypatch,
        queued=[
            _FakeResponse(
                200,
                json_body={"access_token": "fresh-tok", "expires_in": 3600},
            ),
        ],
    )

    token, err = await _client._resolve_access_token()
    assert token == "fresh-tok"
    assert err is None


@pytest.mark.asyncio
async def test_oauth_refresh_401_returns_reconnect_envelope(_clean_env, monkeypatch):
    _clean_env.setenv("EMPLOYMENTHERO_OAUTH_CLIENT_ID", "cid")
    _clean_env.setenv("EMPLOYMENTHERO_OAUTH_CLIENT_SECRET", "csec")
    _clean_env.setenv("EMPLOYMENTHERO_REFRESH_TOKEN", "stale-rt")

    _patch_httpx(
        monkeypatch,
        queued=[_FakeResponse(401, text="invalid_grant")],
    )

    token, err = await _client._resolve_access_token()
    assert token is None
    assert err is not None
    assert err["status_code"] == 401
    assert "reconnect" in err["error"].lower()
    # Cache should NOT have been populated.
    assert _client._TOKEN_CACHE == {}


@pytest.mark.asyncio
async def test_oauth_refresh_400_returns_reconnect_envelope(_clean_env, monkeypatch):
    """EH returns 400 ``invalid_grant`` for revoked refresh tokens; same UX."""
    _clean_env.setenv("EMPLOYMENTHERO_OAUTH_CLIENT_ID", "cid")
    _clean_env.setenv("EMPLOYMENTHERO_OAUTH_CLIENT_SECRET", "csec")
    _clean_env.setenv("EMPLOYMENTHERO_REFRESH_TOKEN", "revoked-rt")

    _patch_httpx(
        monkeypatch,
        queued=[_FakeResponse(400, text="invalid_grant")],
    )

    token, err = await _client._resolve_access_token()
    assert token is None
    assert err is not None
    assert err["status_code"] == 400
    assert "reconnect" in err["error"].lower()


@pytest.mark.asyncio
async def test_oauth_refresh_5xx_passes_through_error(_clean_env, monkeypatch):
    _clean_env.setenv("EMPLOYMENTHERO_OAUTH_CLIENT_ID", "cid")
    _clean_env.setenv("EMPLOYMENTHERO_OAUTH_CLIENT_SECRET", "csec")
    _clean_env.setenv("EMPLOYMENTHERO_REFRESH_TOKEN", "rt")

    _patch_httpx(
        monkeypatch,
        queued=[_FakeResponse(503, text="service unavailable")],
    )

    token, err = await _client._resolve_access_token()
    assert token is None
    assert err is not None
    assert err["status_code"] == 503
    assert "503" in err["error"]


@pytest.mark.asyncio
async def test_oauth_refresh_token_rotation_re_keys_cache(_clean_env, monkeypatch):
    """If EH returns a new refresh_token, the in-process cache moves to
    the new key so subsequent calls in this worker still benefit."""
    _clean_env.setenv("EMPLOYMENTHERO_OAUTH_CLIENT_ID", "cid")
    _clean_env.setenv("EMPLOYMENTHERO_OAUTH_CLIENT_SECRET", "csec")
    _clean_env.setenv("EMPLOYMENTHERO_REFRESH_TOKEN", "rt-old")

    _patch_httpx(
        monkeypatch,
        queued=[
            _FakeResponse(
                200,
                json_body={
                    "access_token": "fresh-tok",
                    "refresh_token": "rt-new",  # rotation
                    "expires_in": 3600,
                },
            ),
        ],
    )

    token, err = await _client._resolve_access_token()
    assert token == "fresh-tok"
    assert err is None

    # New key holds the cached token; old key is dropped.
    assert ("cid", "rt-new") in _client._TOKEN_CACHE
    assert ("cid", "rt-old") not in _client._TOKEN_CACHE


@pytest.mark.asyncio
async def test_oauth_response_missing_access_token_returns_error(
    _clean_env,
    monkeypatch,
):
    _clean_env.setenv("EMPLOYMENTHERO_OAUTH_CLIENT_ID", "cid")
    _clean_env.setenv("EMPLOYMENTHERO_OAUTH_CLIENT_SECRET", "csec")
    _clean_env.setenv("EMPLOYMENTHERO_REFRESH_TOKEN", "rt")

    _patch_httpx(
        monkeypatch,
        queued=[
            _FakeResponse(200, json_body={"expires_in": 3600}),
        ],  # missing access_token
    )

    token, err = await _client._resolve_access_token()
    assert token is None
    assert err is not None
    assert "missing access_token" in err["error"]


# ---------------------------------------------------------------------------
# Cache invalidation helper
# ---------------------------------------------------------------------------


def test_invalidate_cached_token_drops_matching_entries():
    _client._TOKEN_CACHE[("c1", "r1")] = ("tok-1", time.time() + 1000)
    _client._TOKEN_CACHE[("c2", "r2")] = ("tok-2", time.time() + 1000)
    _client._TOKEN_CACHE[("c3", "r3")] = (
        "tok-1",
        time.time() + 1000,
    )  # same token, different keys

    _client._invalidate_cached_token("tok-1")

    # Both entries holding "tok-1" are dropped.
    assert ("c1", "r1") not in _client._TOKEN_CACHE
    assert ("c3", "r3") not in _client._TOKEN_CACHE
    # Unrelated entry survives.
    assert ("c2", "r2") in _client._TOKEN_CACHE


def test_invalidate_cached_token_noop_when_not_found():
    _client._TOKEN_CACHE[("c1", "r1")] = ("tok-1", time.time() + 1000)
    _client._invalidate_cached_token("tok-other")
    assert ("c1", "r1") in _client._TOKEN_CACHE
