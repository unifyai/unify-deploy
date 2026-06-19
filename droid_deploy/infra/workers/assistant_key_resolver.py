"""Per-message Unify api_key resolution for shared ingest workers.

Shared worker pods process messages on behalf of many users and
assistants, so the Unify ``UNIFY_KEY`` cannot live on the pod. Instead,
each ingest message carries an :class:`IngestBinding`
(``user_id`` + required ``assistant_id``), and the worker looks up the
caller's api_key via Orchestra's admin endpoints before touching the
Unify SDK. Resolution is deterministic:

- **Assistant-scoped path** -- calls
  ``GET {SETTINGS.ORCHESTRA_URL}/admin/assistant?agent_id={assistant_id}``
  and returns the ``api_key`` field from the matching
  ``AssistantRead`` (personal or organizational, whichever the
  assistant is bound to).

Both endpoints authenticate with a bearer token read from
``SETTINGS.ORCHESTRA_ADMIN_KEY``. Both values are sourced from the
same pydantic-settings singleton the rest of the codebase already
uses (see ``droid.settings.SETTINGS`` and e.g.
``droid_deploy.runtime.assistant_jobs_backend`` for the pattern).
The worker pods surface these via ``ORCHESTRA_URL`` and
``ORCHESTRA_ADMIN_KEY`` env vars populated from the ``droid-secrets``
k8s Secret; pydantic-settings picks them up at import time.

Caching
-------

Lookups are memoised per ``(user_id, assistant_id)`` with a short TTL
so a burst of messages for the same assistant reuses one resolved
key. The cache is intentionally small and process-local -- a fresh
pod starts with a cold cache and the 5-minute TTL bounds staleness
after key rotation. Cache hits do not extend entry lifetime; entries
expire on the original insertion time so a rotated key cannot linger
indefinitely just because traffic keeps it "hot".

Errors
------

All failure modes (transport errors, non-2xx status, empty payloads,
missing ``api_key`` field) raise
:class:`AssistantKeyLookupError`. The ingest worker catches this and
nacks the message for redelivery so the lookup can be retried when
Orchestra is healthy again -- an ack-and-drop on a transient lookup
failure would silently lose the message.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any, Optional

import httpx

from droid.common.pipeline.types import IngestBinding
from droid.settings import SETTINGS

logger = logging.getLogger(__name__)

_CACHE_MAX_ENTRIES = 512
_CACHE_TTL_SECONDS = 300.0
_HTTP_TIMEOUT_SECONDS = 10.0


class AssistantKeyLookupError(RuntimeError):
    """Raised when the ingest worker cannot resolve a Unify api_key.

    Carries the binding identity so the caller can log which message
    failed without having to re-plumb the binding through the
    exception chain.
    """

    def __init__(
        self,
        message: str,
        *,
        user_id: str,
        assistant_id: Optional[str],
    ) -> None:
        super().__init__(message)
        self.user_id = user_id
        self.assistant_id = assistant_id


class _TtlLruCache:
    """Minimal LRU cache with per-entry TTL.

    Keeps the implementation dependency-free (no ``cachetools``) so the
    resolver has no external surface beyond ``httpx``.  Eviction is
    O(1) amortised: OrderedDict preserves insertion order and
    ``move_to_end`` marks recency on read.
    """

    def __init__(self, *, max_entries: int, ttl_seconds: float) -> None:
        self._max = max_entries
        self._ttl = ttl_seconds
        self._data: "OrderedDict[tuple[str, Optional[str]], tuple[str, float]]" = (
            OrderedDict()
        )

    def get(self, key: tuple[str, Optional[str]]) -> Optional[str]:
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at <= time.monotonic():
            # Expired on read: drop so subsequent callers re-fetch.
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: tuple[str, Optional[str]], value: str) -> None:
        expires_at = time.monotonic() + self._ttl
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = (value, expires_at)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


_cache = _TtlLruCache(
    max_entries=_CACHE_MAX_ENTRIES,
    ttl_seconds=_CACHE_TTL_SECONDS,
)


def clear_cache() -> None:
    """Drop every cached api_key (test helper / key-rotation escape hatch)."""
    _cache.clear()


async def resolve_api_key(
    binding: IngestBinding,
    *,
    http_client: Optional[httpx.AsyncClient] = None,
) -> str:
    """Return the Unify ``api_key`` to use for this binding.

    Pod-level config (Orchestra base URL and admin bearer token) is
    sourced from :data:`droid.settings.SETTINGS`, not from function
    arguments.  This keeps the resolver's call sites trivial
    (``await resolve_api_key(binding)``) and follows the project-wide
    convention of reading env-derived config through pydantic-settings
    rather than bare ``os.environ`` reads.

    Parameters
    ----------
    binding
        The ingest binding from the in-flight message. ``user_id`` and
        ``assistant_id`` are both required.
    http_client
        Optional pre-built :class:`httpx.AsyncClient`. Tests pass a
        client with an in-memory transport; production code leaves it
        unset so a short-lived client is created per lookup.

    Raises
    ------
    AssistantKeyLookupError
        Any non-2xx response, malformed payload, absent api_key, or
        missing ``ORCHESTRA_URL`` / ``ORCHESTRA_ADMIN_KEY`` on the pod.
    """
    user_id = binding.user_id
    assistant_id = binding.assistant_id
    if not user_id:
        raise AssistantKeyLookupError(
            "binding.user_id is required for api_key resolution",
            user_id=user_id,
            assistant_id=assistant_id,
        )

    orchestra_url = SETTINGS.ORCHESTRA_URL
    admin_key = SETTINGS.ORCHESTRA_ADMIN_KEY.get_secret_value()
    if not orchestra_url:
        raise AssistantKeyLookupError(
            "SETTINGS.ORCHESTRA_URL is empty; cannot resolve api_key. "
            "Set ORCHESTRA_URL on the worker pod.",
            user_id=user_id,
            assistant_id=assistant_id,
        )
    if not admin_key:
        raise AssistantKeyLookupError(
            "SETTINGS.ORCHESTRA_ADMIN_KEY is empty; cannot resolve "
            "api_key. Mount ORCHESTRA_ADMIN_KEY from droid-secrets.",
            user_id=user_id,
            assistant_id=assistant_id,
        )

    cache_key = (user_id, assistant_id)
    cached = _cache.get(cache_key)
    if cached is not None:
        logger.debug(
            "api_key cache hit user_id=%s assistant_id=%s",
            user_id,
            assistant_id,
        )
        return cached

    base = _normalize_orchestra_url(orchestra_url)
    headers = {"Authorization": f"Bearer {admin_key}"}

    close_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS)
    try:
        api_key = await _resolve_via_assistant(
            client=client,
            base_url=base,
            headers=headers,
            user_id=user_id,
            assistant_id=assistant_id,
        )
    finally:
        if close_client:
            await client.aclose()

    _cache.set(cache_key, api_key)
    logger.info(
        "Resolved Unify api_key user_id=%s assistant_id=%s (cached %ds)",
        user_id,
        assistant_id,
        int(_CACHE_TTL_SECONDS),
    )
    return api_key


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _normalize_orchestra_url(url: str) -> str:
    """Strip trailing slash and ensure a ``/v0`` prefix is present."""
    trimmed = url.rstrip("/")
    if trimmed.endswith("/v0"):
        return trimmed
    return f"{trimmed}/v0"


async def _resolve_via_assistant(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict,
    user_id: str,
    assistant_id: str,
) -> str:
    """FM path: look up the assistant's api_key by agent_id."""
    url = f"{base_url}/admin/assistant"
    try:
        resp = await client.get(
            url,
            params={"agent_id": assistant_id},
            headers=headers,
        )
    except httpx.HTTPError as exc:
        raise AssistantKeyLookupError(
            f"Orchestra request failed for assistant_id={assistant_id}: {exc}",
            user_id=user_id,
            assistant_id=assistant_id,
        ) from exc

    if resp.status_code != 200:
        raise AssistantKeyLookupError(
            f"Orchestra GET /admin/assistant returned {resp.status_code} "
            f"for assistant_id={assistant_id}: {resp.text[:200]}",
            user_id=user_id,
            assistant_id=assistant_id,
        )

    payload = _parse_json(resp, user_id=user_id, assistant_id=assistant_id)
    # Admin endpoint wraps results in InfoResponse(info=[...]).  A filter
    # by agent_id is expected to match exactly one assistant.
    assistants = payload.get("info") if isinstance(payload, dict) else None
    if not isinstance(assistants, list) or not assistants:
        raise AssistantKeyLookupError(
            f"No assistant found for agent_id={assistant_id}",
            user_id=user_id,
            assistant_id=assistant_id,
        )
    first = assistants[0]
    if not isinstance(first, dict):
        raise AssistantKeyLookupError(
            f"Unexpected assistant payload shape for agent_id={assistant_id}: "
            f"{type(first).__name__}",
            user_id=user_id,
            assistant_id=assistant_id,
        )
    api_key = first.get("api_key")
    if not isinstance(api_key, str) or not api_key:
        raise AssistantKeyLookupError(
            f"Assistant agent_id={assistant_id} has no api_key in admin response",
            user_id=user_id,
            assistant_id=assistant_id,
        )
    return api_key


def _parse_json(
    resp: httpx.Response,
    *,
    user_id: str,
    assistant_id: Optional[str],
) -> Any:
    try:
        return resp.json()
    except ValueError as exc:
        raise AssistantKeyLookupError(
            f"Non-JSON response from Orchestra: {exc}",
            user_id=user_id,
            assistant_id=assistant_id,
        ) from exc
