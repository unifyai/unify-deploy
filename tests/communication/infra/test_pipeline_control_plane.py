"""The ``/infra/pipeline/*`` control plane an assistant pod dispatches through.

The tests that matter here are about authority, not plumbing. This plane exists
so a pod never holds the credentials to publish to the parse topic or write the
artifact bucket, and the guarantees that buys are only real if: the caller is
authenticated before anything happens, the identity work runs under comes from
the verified session rather than the request body, and a recovery publish is
serialised so two attempts cannot both fire at one job.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from communication.dependencies import CallerContext
from communication.infra.self_router import assistant_self_router
import communication.infra.pipeline_control_plane as plane

OWNER = SimpleNamespace(user_id="user-7", assistant_id=42, org_id=None, team_ids=[])


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(assistant_self_router, prefix="/infra")
    return TestClient(app)


def _as_owner() -> AsyncMock:
    """The pod authenticating as itself, with a verified session identity.

    Patched at the dependency rather than at ``_authorised_identity`` so the
    plane's own identity extraction is what runs -- that mapping from session to
    binding ids is precisely the thing these tests need to hold.
    """
    return AsyncMock(return_value=CallerContext(is_admin=False, identity=OWNER))


def _refused() -> AsyncMock:
    """A caller whose key does not match the assistant it names."""
    from fastapi import HTTPException

    return AsyncMock(side_effect=HTTPException(status_code=403, detail="not yours"))


def _fake_infra(*, signs: bool = True) -> MagicMock:
    store = MagicMock()
    if signs:
        store.signed_upload_url.side_effect = lambda key, ttl_seconds=900: (
            f"https://signed.test/{key}",
            f"gs://bucket/{key}",
        )
    else:
        # A store that cannot sign: the self-host shape, where the plane and the
        # workers share a volume instead of an object store.
        del store.signed_upload_url
    infra = MagicMock()
    infra.artifact_store = store
    infra.settings = SimpleNamespace(
        artifact_store=SimpleNamespace(bucket="bucket"),
        pubsub=SimpleNamespace(project_id="proj"),
        env_suffix=lambda: "",
    )
    return infra


class TestAuthority:
    def test_an_unauthorised_caller_reaches_no_operation(self, client: TestClient):
        """Authorisation runs before the plane touches any backend.

        If it did not, a refused caller could still make this service publish or
        mutate on its behalf -- the exact escalation the indirection prevents.
        """
        with (
            patch.object(plane, "authorize_admin_or_assistant", _refused()),
            patch.object(plane, "_infra") as infra,
        ):
            response = client.post(
                "/infra/pipeline/submit",
                json={
                    "assistant_id": 42,
                    "run_key": "run1",
                    "request_key": "jobs/run1/request.json",
                    "paths": ["/tmp/a.pdf"],
                },
            )
        assert response.status_code == 403
        infra.assert_not_called()

    def test_publish_binds_the_session_identity_not_the_body(
        self,
        client: TestClient,
    ):
        """Identity comes from the verified session, always.

        A body-supplied user or assistant id would let one pod dispatch work
        that lands in another tenant's contexts, which is the whole reason the
        wire contract has no identity fields.
        """
        publish = MagicMock(return_value=["run1-0000"])
        with (
            patch.object(plane, "authorize_admin_or_assistant", _as_owner()),
            patch.object(plane, "_infra", return_value=_fake_infra()),
            patch(
                "unify_deploy.infra.pipeline_ops.publish_submit",
                publish,
            ),
        ):
            response = client.post(
                "/infra/pipeline/submit/publish",
                json={
                    "assistant_id": 42,
                    "run_key": "run1",
                    "request_key": "jobs/run1/request.json",
                    "logical_paths": ["/tmp/a.pdf"],
                    "source_uris": ["gs://bucket/jobs/run1/sources/0000-a.pdf"],
                    "ingestion_mode": "dm",
                    "target_context": "Data/Deals",
                    # An attempt to name someone else's scope directly.
                    "user_id": "someone-else",
                    "assistant_id_override": 999,
                },
            )
        assert response.status_code == 200, response.text
        assert publish.call_args.kwargs["user_id"] == "user-7"
        assert publish.call_args.kwargs["assistant_id"] == "42"


class TestSubmitting:
    def test_prepare_returns_one_target_per_file_plus_the_request(
        self,
        client: TestClient,
    ):
        with (
            patch.object(plane, "authorize_admin_or_assistant", _as_owner()),
            patch.object(plane, "_infra", return_value=_fake_infra()),
        ):
            response = client.post(
                "/infra/pipeline/submit",
                json={
                    "assistant_id": 42,
                    "run_key": "run1",
                    "request_key": "jobs/run1/request.json",
                    "paths": ["/tmp/a.pdf", "/tmp/b.csv"],
                },
            )
        body = response.json()
        assert response.status_code == 200, response.text
        # The dispatch id is the caller's run key, not a fresh one: one identity
        # across run row, artifacts, leases and checkpoints is what makes a
        # dispatched run resumable.
        assert body["dispatch_id"] == "run1"
        assert body["request_upload"]["upload_url"].startswith("https://signed.test/")
        assert len(body["sources"]) == 2
        assert [source["logical_path"] for source in body["sources"]] == [
            "/tmp/a.pdf",
            "/tmp/b.csv",
        ]

    def test_a_mismatched_publish_is_refused(self, client: TestClient):
        """Paths and staged URIs must correspond one to one.

        A mismatch means the caller staged something other than what it was
        given, so publishing would dispatch work whose bytes may not be where
        the fleet will look for them.
        """
        with (
            patch.object(plane, "authorize_admin_or_assistant", _as_owner()),
            patch.object(plane, "_infra", return_value=_fake_infra()),
        ):
            response = client.post(
                "/infra/pipeline/submit/publish",
                json={
                    "assistant_id": 42,
                    "run_key": "run1",
                    "request_key": "jobs/run1/request.json",
                    "logical_paths": ["/tmp/a.pdf", "/tmp/b.csv"],
                    "source_uris": ["gs://bucket/only-one"],
                },
            )
        assert response.status_code == 400
        assert "one to one" in response.json()["detail"]

    def test_a_signing_store_refuses_brokered_uploads(self, client: TestClient):
        """Where a URL can be signed, the bytes must not come through here.

        Accepting them anyway would put every ingestion under this service's
        request-size ceiling and bill its bandwidth for data it has no reason
        to see.
        """
        with (
            patch.object(plane, "authorize_admin_or_assistant", _as_owner()),
            patch.object(plane, "_infra", return_value=_fake_infra(signs=True)),
        ):
            response = client.put(
                "/infra/pipeline/upload/run1/a.pdf?assistant_id=42",
                content=b"bytes",
            )
        assert response.status_code == 400
        assert "signed upload URLs" in response.json()["detail"]

    def test_a_non_signing_store_accepts_bytes_under_the_verified_run(
        self,
        client: TestClient,
    ):
        """Self-host uploads land under the run in the path, not the caller's key.

        Composing the key here rather than accepting one is what stops an upload
        being aimed at another run's namespace.
        """
        infra = _fake_infra(signs=False)
        with (
            patch.object(plane, "authorize_admin_or_assistant", _as_owner()),
            patch.object(plane, "_infra", return_value=infra),
        ):
            response = client.put(
                "/infra/pipeline/upload/run1/..%5C..%5Cescape.pdf?assistant_id=42",
                content=b"bytes",
            )
        assert response.status_code == 200, response.text
        key = infra.artifact_store.put_bytes.call_args.args[0]
        assert key.startswith("jobs/run1/sources/")
        assert ".." not in key


class TestRecovering:
    def test_retry_has_no_force_escape(self, client: TestClient):
        """A force flag would reintroduce the duplicate-message race.

        Two recovery publishes for one job put two live attempts against one
        attempt-lease, and the loser's writes freeze the durable checkpoint --
        the run then under-ingests while reporting nothing. The lease is the
        control; an override is not offered.
        """
        retry = AsyncMock(return_value={"requeued": 0})
        with (
            patch.object(plane, "authorize_admin_or_assistant", _as_owner()),
            patch.object(plane, "_infra", return_value=_fake_infra()),
            patch("unify_deploy.infra.pipeline_ops.retry_dispatch", retry),
        ):
            response = client.post(
                "/infra/pipeline/retry",
                json={
                    "assistant_id": 42,
                    "dispatch_id": "run1",
                    "scope": "dlq",
                    "force": True,
                },
            )
        assert response.status_code == 200, response.text
        assert "force" not in retry.call_args.kwargs

    def test_a_busy_job_reports_the_holder_rather_than_racing(
        self,
        client: TestClient,
    ):
        from unify_deploy.infra.pipeline_ops import RecoveryBusy

        with (
            patch.object(plane, "authorize_admin_or_assistant", _as_owner()),
            patch.object(plane, "_infra", return_value=_fake_infra()),
            patch(
                "unify_deploy.infra.pipeline_ops.retry_dispatch",
                AsyncMock(side_effect=RecoveryBusy("run1-0000", "recovery-abc")),
            ),
        ):
            response = client.post(
                "/infra/pipeline/retry",
                json={"assistant_id": 42, "dispatch_id": "run1", "scope": "dlq"},
            )
        assert response.status_code == 409
        assert "recovery-abc" in response.json()["detail"]

    def test_an_unknown_dispatch_is_a_404_not_a_500(self, client: TestClient):
        from unify_deploy.infra.pipeline_ops import PipelineOpError

        with (
            patch.object(plane, "authorize_admin_or_assistant", _as_owner()),
            patch.object(plane, "_infra", return_value=_fake_infra()),
            patch(
                "unify_deploy.infra.pipeline_ops.dispatch_status",
                MagicMock(
                    side_effect=PipelineOpError("no such dispatch", status_code=404),
                ),
            ),
        ):
            response = client.get("/infra/pipeline/status/nope?assistant_id=42")
        assert response.status_code == 404


class TestHealth:
    def test_health_reports_unusable_when_no_backends_are_configured(
        self,
        client: TestClient,
    ):
        """Configured is not reachable.

        A plane that answers but cannot publish must not read as healthy: the
        caller would dispatch work that then goes nowhere and sits queued
        forever.
        """
        infra = _fake_infra()
        infra.settings = SimpleNamespace(
            artifact_store=SimpleNamespace(bucket=""),
            pubsub=SimpleNamespace(project_id=""),
        )
        with patch.object(plane, "_infra", return_value=infra):
            response = client.get("/infra/pipeline/health")
        assert response.status_code == 200
        assert response.json()["ok"] is False

    def test_health_is_ok_with_backends(self, client: TestClient):
        with patch.object(plane, "_infra", return_value=_fake_infra()):
            response = client.get("/infra/pipeline/health")
        assert response.json()["ok"] is True
