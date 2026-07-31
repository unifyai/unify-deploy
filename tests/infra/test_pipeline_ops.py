"""Pipeline operations shared by the operator CLI and the hosted control plane.

The tests here cover the two things this module exists to guarantee, both of
which the CLI's own command bodies only approximated:

* **Recovery is serialised on a lease.** Two publishes for one job put two live
  attempts against one attempt-lease, and the loser's writes freeze the durable
  checkpoint -- the run then under-ingests while reporting success. The CLI
  guarded this with a freshness window and a ``--force`` escape; a lease removes
  the race instead of narrowing it.
* **A dispatch's folded state never claims false completion.** The assistant
  acts on that one word, and "succeeded" while a worker is still writing is the
  answer that makes it act wrongly.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from unify.common.pipeline.artifact_store import LocalArtifactStore
from unify_deploy.infra.pipeline_ops import (
    RecoveryBusy,
    _fold_state,
    dispatch_status,
    hold_recovery,
    prepare_submit,
    retry_dispatch,
)


@pytest.fixture()
def store(tmp_path) -> LocalArtifactStore:
    return LocalArtifactStore(root_dir=tmp_path / "artifacts")


class _JobStore:
    """Minimal job store: the ops only read, mutate and list."""

    def __init__(self, jobs: dict[str, Any], dispatch_jobs: list[str]):
        self.jobs = jobs
        self.dispatch_jobs = dispatch_jobs
        self.upserts: list[str] = []

    def read_dispatch(self, dispatch_id: str):
        if not self.dispatch_jobs:
            raise FileNotFoundError(dispatch_id)
        return SimpleNamespace(dispatch_id=dispatch_id, job_ids=self.dispatch_jobs)

    def read_job(self, job_id: str):
        return self.jobs[job_id]

    def upsert_job(self, job) -> str:
        self.jobs[job.job_id] = job
        self.upserts.append(job.job_id)
        return job.job_id


def _job(job_id: str, status: str, **metadata):
    return SimpleNamespace(
        job_id=job_id,
        status=status,
        error=None,
        finished_at=None,
        metadata=dict(metadata),
    )


def _infra(store: LocalArtifactStore, job_store: _JobStore, published: list):
    async def publish(*, topic: str, payload: dict) -> str:
        published.append({"topic": topic, "payload": payload})
        return f"msg-{len(published)}"

    return SimpleNamespace(
        artifact_store=store,
        job_store=job_store,
        work_queue=SimpleNamespace(publish=publish),
        settings=SimpleNamespace(
            artifact_store=SimpleNamespace(bucket="bucket"),
            pubsub=SimpleNamespace(project_id="proj"),
            env_suffix=lambda: "",
        ),
    )


class TestRecoveryLease:
    def test_a_second_attempt_is_told_who_holds_it(self, store):
        """The whole point: two recoveries cannot both publish for one job."""
        first = hold_recovery(store, "job-1")
        with pytest.raises(RecoveryBusy) as excinfo:
            hold_recovery(store, "job-1")
        assert first.owner_id in str(excinfo.value)
        assert "nothing was published" in str(excinfo.value)

    def test_releasing_lets_the_next_attempt_through(self, store):
        hold_recovery(store, "job-1").release()
        # No raise: the successor need not wait out a TTL after a clean release.
        hold_recovery(store, "job-1").release()

    def test_leases_are_per_job(self, store):
        hold_recovery(store, "job-1")
        # A different job is unrelated work; blocking it would serialise the
        # whole dispatch behind one slow recovery.
        hold_recovery(store, "job-2")


class TestRetry:
    def test_a_live_job_is_never_published_beside(self, store):
        """A queued or running job already has an attempt that owns it.

        Publishing alongside is the duplicate delivery the lease exists to stop,
        so the scope filter refuses before the lease is even taken.
        """
        job_store = _JobStore({"j1": _job("j1", "running")}, ["j1"])
        published: list = []
        result = asyncio.run(
            retry_dispatch(
                infra=_infra(store, job_store, published),
                dispatch_id="d1",
                scope="dlq",
            ),
        )
        assert result["requeued"] == 0
        assert published == []
        assert result["skipped"][0]["reason"] == "still running"

    def test_a_succeeded_job_is_not_redone_under_dlq_scope(self, store):
        job_store = _JobStore({"j1": _job("j1", "success")}, ["j1"])
        published: list = []
        result = asyncio.run(
            retry_dispatch(
                infra=_infra(store, job_store, published),
                dispatch_id="d1",
                scope="dlq",
            ),
        )
        assert published == []
        assert result["skipped"][0]["reason"] == "already succeeded"

    def test_a_parked_job_republishes_its_own_failed_message(self, store):
        """The DLQ payload is the exact message that failed.

        Re-publishing it resumes precisely the work that stopped, where
        re-parsing from scratch would redo the expensive half for nothing.
        """
        store.put_json(
            "jobs/j1/dlq/rec1.json",
            {
                "job_id": "j1",
                "retry_topic": "ingest",
                "payload": {"kind": "ingest_requested", "job_id": "j1"},
                "dlq_message_id": "m1",
                "delivery_attempt": 5,
                "retry_classification": "retryable",
                "recorded_at": "2026-07-31T00:00:00+00:00",
            },
        )
        job_store = _JobStore({"j1": _job("j1", "error")}, ["j1"])
        published: list = []
        result = asyncio.run(
            retry_dispatch(
                infra=_infra(store, job_store, published),
                dispatch_id="d1",
                scope="dlq",
            ),
        )
        assert result["requeued"] == 1
        assert published[0]["topic"] == "ingest"
        assert job_store.jobs["j1"].status == "queued"
        assert job_store.jobs["j1"].metadata["retry_scope"] == "dlq"

    def test_the_lease_is_released_so_a_later_retry_is_not_blocked(self, store):
        store.put_json(
            "jobs/j1/dlq/rec1.json",
            {
                "job_id": "j1",
                "retry_topic": "ingest",
                "payload": {"job_id": "j1"},
                "dlq_message_id": "m1",
                "delivery_attempt": 1,
                "retry_classification": "retryable",
                "recorded_at": "2026-07-31T00:00:00+00:00",
            },
        )
        job_store = _JobStore({"j1": _job("j1", "error")}, ["j1"])
        published: list = []
        infra = _infra(store, job_store, published)
        asyncio.run(retry_dispatch(infra=infra, dispatch_id="d1", scope="dlq"))
        # Held leases would wedge every subsequent recovery for two minutes.
        hold_recovery(store, "j1")

    def test_scope_all_discards_checkpoints_before_republishing(self, store):
        """Re-attempting everything means the committed marks no longer apply.

        Leaving them would make the re-run skip exactly the rows it was asked to
        rewrite -- which is why this is never the default scope.
        """
        from unify.common.pipeline.types import IngestCheckpoint

        store.write_checkpoint(
            "j1",
            "table-1",
            IngestCheckpoint(
                job_id="j1",
                artifact_id="table-1",
                chunks_committed=3,
                rows_committed=300,
                last_updated="2026-07-31T00:00:00+00:00",
            ),
        )
        store.put_json(
            "jobs/j1/outbox/parse.json",
            {"payload": {"kind": "ingest_requested", "job_id": "j1"}},
        )
        job_store = _JobStore({"j1": _job("j1", "error")}, ["j1"])
        published: list = []
        asyncio.run(
            retry_dispatch(
                infra=_infra(store, job_store, published),
                dispatch_id="d1",
                scope="all",
            ),
        )
        assert store.read_checkpoint("j1", "table-1") is None
        assert published[0]["topic"] == "ingest"

    def test_a_stale_job_resumes_from_the_parse_outbox(self, store):
        """A worker that died after parsing should not re-parse.

        The outbox holds the ingest message the parse stage already committed
        to, so recovery continues at the stage that stopped.
        """
        store.put_json(
            "jobs/j1/outbox/parse.json",
            {"payload": {"kind": "ingest_requested", "job_id": "j1"}},
        )
        job_store = _JobStore({"j1": _job("j1", "error")}, ["j1"])
        published: list = []
        result = asyncio.run(
            retry_dispatch(
                infra=_infra(store, job_store, published),
                dispatch_id="d1",
                scope="stale",
            ),
        )
        assert result["requeued"] == 1
        assert published[0]["topic"] == "ingest"


class TestFoldedState:
    @pytest.mark.parametrize(
        "statuses,expected",
        [
            ([], "queued"),
            (["queued", "queued"], "queued"),
            (["success", "running"], "running"),
            (["success", "success"], "succeeded"),
            (["success", "error"], "failed"),
            (["success", "cancelled"], "cancelled"),
            (["success", "paused"], "paused"),
        ],
    )
    def test_states_fold_conservatively(self, statuses, expected):
        assert _fold_state(statuses) == expected

    def test_one_unfinished_job_keeps_the_whole_run_unfinished(self):
        """The failure this prevents is the expensive one.

        A run reported succeeded while a worker is still writing sends the
        caller off to build a view over half-landed data.
        """
        assert _fold_state(["success"] * 9 + ["running"]) != "succeeded"

    def test_an_unreadable_job_does_not_read_as_finished(self):
        assert _fold_state(["success", "unknown"]) != "succeeded"


class TestStatus:
    def test_rows_come_from_checkpoints_not_job_metadata(self, store):
        """The checkpoint is what a resume trusts.

        Reporting anything else would let the run row and the resumable
        progress disagree about where the work got to.
        """
        from unify.common.pipeline.types import IngestCheckpoint

        store.write_checkpoint(
            "j1",
            "t1",
            IngestCheckpoint(
                job_id="j1",
                artifact_id="t1",
                chunks_committed=2,
                rows_committed=250,
                last_updated="2026-07-31T00:00:00+00:00",
            ),
        )
        job_store = _JobStore(
            {
                "j1": _job(
                    "j1",
                    "success",
                    target_context="Data/Deals",
                    total_rows_inserted=999999,
                ),
            },
            ["j1"],
        )
        status = dispatch_status(infra=_infra(store, job_store, []), dispatch_id="d1")
        assert status["rows_written"] == 250
        assert status["state"] == "succeeded"
        assert status["contexts"] == ["Data/Deals"]

    def test_an_unknown_dispatch_refuses_with_a_404(self, store):
        from unify_deploy.infra.pipeline_ops import PipelineOpError

        job_store = _JobStore({}, [])
        with pytest.raises(PipelineOpError) as excinfo:
            dispatch_status(infra=_infra(store, job_store, []), dispatch_id="nope")
        assert excinfo.value.status_code == 404


class TestPrepare:
    def test_the_dispatch_id_is_the_callers_run_key(self, store):
        prepared = prepare_submit(
            artifact_store=store,
            run_key="run1",
            request_key="jobs/run1/request.json",
            paths=["/tmp/a.pdf", "/tmp/b.csv"],
        )
        # One identity across the run row, artifacts, leases and checkpoints is
        # what makes a dispatched run resumable by anything reading the layout.
        assert prepared.dispatch_id == "run1"
        assert len(prepared.sources) == 2

    def test_a_store_that_cannot_sign_reports_plain_keys(self, store):
        """The local store has nothing to sign, so the caller uploads through
        the control plane instead -- and learns that from the empty URL rather
        than from a branch on deployment shape."""
        prepared = prepare_submit(
            artifact_store=store,
            run_key="run1",
            request_key="jobs/run1/request.json",
            paths=["/tmp/a.pdf"],
        )
        assert prepared.request_upload.upload_url == ""
        assert prepared.sources[0].object_uri.startswith("jobs/run1/sources/")

    def test_source_keys_are_ordered_and_sanitised(self, store):
        prepared = prepare_submit(
            artifact_store=store,
            run_key="run1",
            request_key="jobs/run1/request.json",
            paths=["/tmp/../evil name.pdf", "/tmp/b.csv"],
        )
        keys = [target.object_uri for target in prepared.sources]
        assert keys[0].startswith("jobs/run1/sources/0000-")
        assert keys[1].startswith("jobs/run1/sources/0001-")
        assert ".." not in keys[0]
        assert " " not in keys[0]
