"""Unit tests for :mod:`unity_deploy.infra.gcp.artifact_store`.

Focused on the retry wrapper semantics. In particular, HTTP 404 responses
must be treated as definitive ``not found`` answers and bubble up without
consulting the generic retry policy — otherwise callers like
``PubSubWorkQueue.is_cancelled`` burn ~30s of backoff on every message
simply to discover that the cancellation sentinel does not exist.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from unity.common.pipeline.retry_policy import ResilientRequestPolicy
from unity_deploy.infra.gcp.artifact_store import (
    GcsArtifactStore,
    _is_not_found_error,
)


def _make_store(*, max_retries: int = 3) -> GcsArtifactStore:
    """Build a GcsArtifactStore wired to mocks with a liberal retry policy.

    The retry policy is intentionally permissive (``retry_mode='all_errors'``,
    non-zero ``max_retries``) so we can assert that the 404 short-circuit
    fires *in spite of* the policy being willing to retry.
    """
    settings = MagicMock()
    settings.bucket = "test-bucket"
    settings.prefix = ""
    policy = ResilientRequestPolicy(
        max_retries=max_retries,
        retry_delay_seconds=0.0,
        backoff_multiplier=1.0,
        jitter_ratio=0.0,
        retry_mode="all_errors",
    )
    return GcsArtifactStore(
        client=MagicMock(),
        settings=settings,
        retry_policy=policy,
    )


class TestIsNotFoundError:
    def test_detects_google_api_core_not_found(self):
        from google.api_core.exceptions import NotFound

        assert _is_not_found_error(NotFound("boom"))

    def test_detects_invalid_response_with_404_status(self):
        from google.resumable_media.common import InvalidResponse

        response = MagicMock()
        response.status_code = 404
        exc = InvalidResponse(response, "Request failed with status code", 404)
        assert _is_not_found_error(exc)

    def test_rejects_invalid_response_with_non_404_status(self):
        from google.resumable_media.common import InvalidResponse

        response = MagicMock()
        response.status_code = 500
        exc = InvalidResponse(response, "Request failed with status code", 500)
        assert not _is_not_found_error(exc)

    def test_string_fallback_matches_404_prefix(self):
        exc = Exception("404 GET https://example.com/o/foo: No such object")
        assert _is_not_found_error(exc)

    def test_string_fallback_matches_wrapped_tuple_form(self):
        exc = Exception(
            "wrapped: ('Request failed with status code', 404, 'Expected one of', 200)",
        )
        assert _is_not_found_error(exc)

    def test_plain_value_error_is_not_a_404(self):
        assert not _is_not_found_error(ValueError("nope"))


class TestWithRetry404ShortCircuit:
    def test_404_raises_immediately_with_zero_retries(self):
        from google.api_core.exceptions import NotFound

        store = _make_store(max_retries=5)
        call_count = {"n": 0}

        def fn():
            call_count["n"] += 1
            raise NotFound("missing")

        with pytest.raises(NotFound):
            store._with_retry(fn, operation="get_json(foo)")

        assert call_count["n"] == 1, (
            "404 must bubble on the first attempt, but fn was called "
            f"{call_count['n']} times"
        )

    def test_non_404_error_still_retries_under_permissive_policy(self):
        store = _make_store(max_retries=2)
        call_count = {"n": 0}

        def fn():
            call_count["n"] += 1
            raise RuntimeError("transient")

        with pytest.raises(RuntimeError):
            store._with_retry(fn, operation="get_json(foo)")

        assert call_count["n"] == 3, (
            "non-404 errors should exhaust retries (1 initial + 2 retries), "
            f"but fn was called {call_count['n']} times"
        )

    def test_successful_call_returns_value_without_retry(self):
        store = _make_store()

        def fn():
            return "ok"

        assert store._with_retry(fn, operation="get_json(foo)") == "ok"


class TestJobScopedArtifactKeys:
    """The collapsed layout writes every per-job artifact under a single
    ``jobs/<job_id>/...`` root, so a single ``gsutil ls`` shows an entire
    run in one place and no env-scoped prefix is applied (the environment
    lives in the *bucket* name instead).
    """

    def test_materialize_table_input_requires_job_id(self):
        from unity.common.pipeline.types import InlineRowsHandle

        store = _make_store()
        handle = InlineRowsHandle(rows=[], columns=[])

        with pytest.raises(ValueError, match="requires a non-empty\\s+job_id"):
            store.materialize_table_input(
                handle,
                logical_path="demo.csv",
                table_id="table_1",
                artifact_format="jsonl",
                job_id="",
            )


class TestCheckpointSafety:
    def test_read_checkpoint_only_treats_404_as_missing(self):
        store = _make_store()
        store.get_json = MagicMock(side_effect=ValueError("corrupt json"))  # type: ignore[method-assign]

        with pytest.raises(ValueError, match="corrupt json"):
            store.read_checkpoint("job-1", "table-1")

    def test_write_checkpoint_rejects_non_monotonic_rows(self):
        from unity.common.pipeline.types import IngestCheckpoint

        store = _make_store()
        blob = MagicMock()
        blob.generation = 7
        blob.download_as_text.return_value = (
            '{"job_id":"job-1","artifact_id":"table-1",'
            '"chunks_committed":2,"rows_committed":100,"last_updated":"now"}'
        )
        store.bucket.blob.return_value = blob

        with pytest.raises(ValueError, match="non-monotonic checkpoint"):
            store.write_checkpoint(
                "job-1",
                "table-1",
                IngestCheckpoint(
                    job_id="job-1",
                    artifact_id="table-1",
                    chunks_committed=3,
                    rows_committed=90,
                ),
            )

    def test_concurrent_attempt_handoff_can_advance_checkpoint(self):
        """A fresh-generation attempt taking over must still advance rows.

        Regression for the fact_TelematicsTrips desync: the monotonic guard
        only blocks regressions, never legitimate forward progress, so the
        winning attempt (new attempt_id + higher lease_generation) can carry
        a stalled 166k checkpoint to the declared 206,719 instead of being
        silently fenced.
        """
        import json

        from unity.common.pipeline.types import IngestCheckpoint

        store = _make_store()
        blob = MagicMock()
        blob.generation = 11
        blob.download_as_text.return_value = json.dumps(
            {
                "job_id": "job-1",
                "artifact_id": "table-1",
                "chunks_committed": 166,
                "rows_committed": 166000,
                "attempt_id": "attempt-old",
                "lease_generation": 3,
                "last_updated": "now",
            },
        )
        store.bucket.blob.return_value = blob

        store.write_checkpoint(
            "job-1",
            "table-1",
            IngestCheckpoint(
                job_id="job-1",
                artifact_id="table-1",
                chunks_committed=207,
                rows_committed=206719,
            ),
            attempt_id="attempt-new",
            lease_generation=4,
        )

        # The advance was written with optimistic-concurrency fencing on the
        # generation we read, and carries the new attempt's identity.
        assert blob.upload_from_string.call_count == 1
        _, kwargs = blob.upload_from_string.call_args
        assert kwargs["if_generation_match"] == 11
        written = json.loads(blob.upload_from_string.call_args.args[0])
        assert written["rows_committed"] == 206719
        assert written["attempt_id"] == "attempt-new"
        assert written["lease_generation"] == 4
