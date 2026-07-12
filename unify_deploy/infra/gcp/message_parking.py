"""GCS message-parking helpers for pause/resume.

When an operator runs ``pipeline_control pause``, in-flight Pub/Sub
messages are too valuable to nack (they'd just be redelivered to a
still-running worker pod) and too costly to leave in the subscription
(the HPA stays pinned at max replicas while ``num_undelivered_messages``
is non-zero).

Instead we *park* each message: upload its raw payload to
``dispatches/<dispatch_id>/parked/`` in GCS, then ``ack()`` the Pub/Sub
receipt.  The subscription drains, the HPA scales the ingest deployment
down on its own, and the parked blob remains until ``resume`` re-publishes
it onto the ingest topic.

FIFO semantics
--------------
Pub/Sub message ordering is deliberately *not* enabled on the parse/
ingest subscriptions (see ``deploy/scripts/dev/setup_pipeline_infra.sh``
for the creation flags and ``deploy/guides/PIPELINE_OPERATIONS.md`` for the
rationale).  To preserve today's soft-FIFO behaviour through a pause→
resume cycle, parked blobs are named with a zero-padded nanosecond
prefix derived from the original Pub/Sub ``publish_time``:

    dispatches/<dispatch_id>/parked/<published_at_ns>-<message_id>.json

GCS ``list_blobs`` returns keys sorted lexicographically, so sorting
this list is equivalent to sorting by original publish time.  The
``resume`` flow re-publishes the payloads serially in that order, which
is the same guarantee a non-paused dispatch has today.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Iterable

from .artifact_store import GcsArtifactStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# 20 digits is enough for any nanosecond epoch that could realistically be
# produced in this decade (2**63-1 nanoseconds ~= year 2262), and it makes
# lexicographic sort == chronological sort for any practical input.
_PUBLISHED_AT_DIGITS = 20


def _safe_fragment(value: str) -> str:
    """Sanitise *value* for use in a GCS key fragment (no path separators)."""
    text = str(value or "").strip() or "msg"
    return "".join(
        char if char.isalnum() or char in ("-", "_") else "_" for char in text
    )


def _parse_published_at_ns(published_at: str | None) -> int:
    """Convert a Pub/Sub ISO-8601 ``publish_time`` to a nanosecond epoch.

    Returns ``0`` for unparseable input so those messages sort first on
    replay.  This should never happen with real Pub/Sub messages —
    ``google.cloud.pubsub_v1`` always populates ``publish_time`` — but we
    handle it defensively so a clock-skewed test harness cannot break
    the parking flow.
    """
    if not published_at:
        return 0
    # Pub/Sub uses RFC 3339 ("2026-04-24T04:31:10.123456Z"). Python's
    # ``datetime.fromisoformat`` accepts that directly once we normalise
    # the trailing ``Z`` (supported natively from 3.11, but kept explicit
    # for forward-compat).
    text = published_at.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(text)
    except ValueError:
        logger.debug("Could not parse published_at=%r, defaulting to 0", published_at)
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


# ---------------------------------------------------------------------------
# Park / resume primitives
# ---------------------------------------------------------------------------


def parked_blob_key(
    dispatch_id: str,
    *,
    published_at: str | None,
    message_id: str,
) -> str:
    """Return the GCS key a parked message should be written to.

    The key layout is::

        dispatches/<dispatch_id>/parked/<published_at_ns>-<message_id>.json

    where ``published_at_ns`` is a zero-padded 20-digit epoch in
    nanoseconds.  This lets ``list_blobs`` emit keys in original publish
    order under standard lexicographic sort.
    """
    ns = _parse_published_at_ns(published_at)
    prefix = f"{ns:0{_PUBLISHED_AT_DIGITS}d}"
    return (
        f"dispatches/{_safe_fragment(dispatch_id)}/parked/"
        f"{prefix}-{_safe_fragment(message_id)}.json"
    )


def park_message(
    artifact_store: GcsArtifactStore,
    *,
    dispatch_id: str,
    published_at: str | None,
    message_id: str,
    payload: dict[str, Any],
    topic: str = "ingest",
    parked_by: str = "",
) -> str:
    """Upload a message payload into the parking lot and return the key.

    The stored document wraps the original payload with enough metadata
    for ``resume`` to reconstruct a publish-equivalent message and for
    an operator to audit where each parked record came from.
    """
    key = parked_blob_key(
        dispatch_id,
        published_at=published_at,
        message_id=message_id,
    )
    document = {
        "topic": topic,
        "payload": payload,
        "parked_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(
            timespec="microseconds",
        ),
        "published_at": published_at,
        "message_id": message_id,
        "dispatch_id": dispatch_id,
        "parked_by": parked_by or "unknown",
    }
    artifact_store.put_json(key, document)
    logger.info(
        "Parked message %s for dispatch=%s topic=%s -> %s",
        message_id,
        dispatch_id,
        topic,
        key,
    )
    return key


def list_parked_keys(
    artifact_store: GcsArtifactStore,
    dispatch_id: str,
) -> list[str]:
    """List parked blob keys for *dispatch_id*, sorted by publish order.

    Returned keys are relative to the artifact store's prefix (i.e.
    suitable for ``artifact_store.get_json(key)``), not full
    ``gs://bucket/prefix/...`` URIs.
    """
    logical_prefix = f"dispatches/{_safe_fragment(dispatch_id)}/parked/"
    full_prefix = artifact_store._full_key(logical_prefix)
    bucket = artifact_store.bucket
    logical_root = artifact_store._full_key("")
    strip_len = len(logical_root)
    keys: list[str] = []
    for blob in bucket.list_blobs(prefix=full_prefix):
        if not blob.name.endswith(".json"):
            continue
        key = blob.name[strip_len:] if strip_len else blob.name
        keys.append(key)
    keys.sort()
    return keys


def read_parked(
    artifact_store: GcsArtifactStore,
    key: str,
) -> dict[str, Any]:
    """Fetch a parked-message document from GCS."""
    data = artifact_store.get_json(key)
    if not isinstance(data, dict):
        raise ValueError(f"Parked blob {key!r} is not a JSON object")
    return data


def delete_parked(artifact_store: GcsArtifactStore, key: str) -> None:
    """Delete a parked blob by key (idempotent)."""
    artifact_store.delete(key)


def parked_payloads(
    artifact_store: GcsArtifactStore,
    dispatch_id: str,
) -> Iterable[tuple[str, dict[str, Any]]]:
    """Yield ``(key, document)`` for each parked blob in publish order.

    Errors reading individual blobs are logged and skipped so a single
    corrupt blob cannot block the entire resume operation.
    """
    for key in list_parked_keys(artifact_store, dispatch_id):
        try:
            yield key, read_parked(artifact_store, key)
        except Exception:
            logger.exception("Could not read parked blob %s; skipping", key)
