"""``/infra/pipeline/*`` — the control plane an assistant dispatches through.

An assistant pod cannot be handed the credentials to publish to the parse topic
and write the artifact bucket. Those are fleet-wide authorities: a pod holding
them could enqueue work for any tenant, so a single compromised or
prompt-injected pod would have the whole fleet's blast radius. Instead a pod
posts its run here, this service authenticates it against its own
``AssistantSession``, and derives every routing decision itself.

That is why the wire contract is small and why the identity fields are absent
from it. A caller says *what* to ingest -- a staged request key and the files it
names -- and never *who* to ingest as: ``user_id`` and ``assistant_id`` come
from the verified session, so a pod cannot dispatch, observe or recover another
tenant's work even by asking precisely.

Submitting is two calls on purpose. ``POST submit`` mints short-lived write
targets and returns them; the pod stages its bytes straight to the store; then
``POST submit/publish`` emits one parse message per file. Brokering the targets
rather than the bytes keeps this service off the data path -- a multi-gigabyte
folder ingestion never touches an HTTP body here, so neither the request-size
ceiling nor this service's bandwidth bounds what the assistant can store.

Routes attach to ``assistant_self_router``, which is mounted at ``/infra``
*without* the blanket admin-key dependency, and each authorises with
:func:`authorize_admin_or_assistant` -- so the platform control plane keeps
working while a pod can only ever act on itself.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from communication.dependencies import authorize_admin_or_assistant
from communication.infra.self_router import assistant_self_router

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wire contract
# ---------------------------------------------------------------------------


class PipelineSubmitRequest(BaseModel):
    """Ask for write targets for one run's staged request and its sources.

    ``run_key`` is the identity the caller already recorded the run under, and
    it becomes the dispatch id -- one identity across the run row, the
    artifacts, the leases and the checkpoints is what makes a dispatched run
    resumable by anything that can read the layout.

    No identity fields: they come from the authenticated session.
    """

    assistant_id: int
    run_key: str = Field(min_length=1, max_length=64)
    request_key: str = Field(min_length=1, max_length=256)
    paths: list[str] = Field(min_length=1, max_length=4096)


class PipelinePublishRequest(BaseModel):
    """Publish a prepared run, now that its bytes are staged.

    ``source_uris`` are the object URIs the prepare step handed back, in the
    same order as its ``paths``. Echoing them rather than re-deriving keeps the
    two halves honest: a mismatch means the caller staged something other than
    what it was given, and the publish refuses rather than dispatching work
    whose bytes may not be where the fleet will look.
    """

    assistant_id: int
    run_key: str = Field(min_length=1, max_length=64)
    request_key: str = Field(min_length=1, max_length=256)
    logical_paths: list[str] = Field(min_length=1, max_length=4096)
    source_uris: list[str] = Field(min_length=1, max_length=4096)
    # ``fm`` keeps documents whole under Files/…; ``dm`` merges tabular content
    # into one queryable context. Derived by the manager from the target kind,
    # never a free choice at this layer.
    ingestion_mode: Literal["fm", "dm"] = "dm"
    target_context: str = ""
    destination: Optional[str] = None
    # Where the run's events should be journalled, so workers write to the same
    # contexts an in-process run does. Opaque here; the workers interpret it.
    observability: Optional[dict[str, str]] = None


class PipelineRecoveryRequest(BaseModel):
    """Steer a dispatch that is already running on the fleet.

    Deliberately no ``force``: the guarantee this plane offers is that two
    recovery attempts cannot publish for one job at once, and a force flag is
    exactly the escape that reintroduces the duplicate-message race. A caller
    that believes a prior message is gone is guessing; the lease knows.
    """

    assistant_id: int
    dispatch_id: str = Field(min_length=1, max_length=64)
    scope: Literal["dlq", "stale", "all"] = "dlq"


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


async def _authorised_identity(request: Request, assistant_id: int) -> dict[str, str]:
    """Authorise the caller and return the identity work will be bound to.

    An admin caller (the platform control plane) is trusted for the assistant id
    it names; a pod is verified against its own session and can name no other.
    Either way the ids used downstream come from here, never from the body.
    """
    caller = await authorize_admin_or_assistant(request, assistant_id=assistant_id)
    identity = getattr(caller, "identity", None)
    if identity is not None:
        return {
            "user_id": str(identity.user_id),
            "assistant_id": str(identity.assistant_id),
        }

    # Admin path: no session to read, so resolve the named assistant's owner
    # from Orchestra rather than trusting a body field for provenance.
    from communication.infra.task_execution import _get_assistant_data

    data = _get_assistant_data(str(assistant_id))
    return {
        "user_id": str(data.get("user_id") or ""),
        "assistant_id": str(assistant_id),
    }


def _infra() -> Any:
    from unify_deploy.infra.workers.worker_utils import build_worker_infra

    return build_worker_infra()


def _handled(exc: Exception) -> HTTPException:
    """Translate an operation's refusal into its HTTP shape."""
    from unify_deploy.infra.pipeline_ops import PipelineOpError

    from pydantic import ValidationError

    if isinstance(exc, PipelineOpError):
        return HTTPException(status_code=exc.status_code, detail=str(exc))
    if isinstance(exc, ValidationError):
        # A malformed request is not a server fault, and the difference decides
        # what the caller does next: 500 reads as transient, so a caller that
        # trusts the status code retries a rejection that will refuse
        # identically every time. Observed with an invalid execution_target,
        # where every brokered publish came back as "refused (500)".
        logger.warning("Pipeline control plane rejected a request: %s", exc)
        return HTTPException(status_code=422, detail=str(exc))
    logger.exception("Pipeline control plane operation failed")
    return HTTPException(status_code=500, detail=str(exc) or "operation failed")


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------


@assistant_self_router.post("/pipeline/submit")
async def pipeline_submit(request: Request, body: PipelineSubmitRequest):
    """Mint write targets for a run's staged request and each of its files."""
    await _authorised_identity(request, body.assistant_id)

    from unify_deploy.infra.pipeline_ops import prepare_submit

    try:
        prepared = prepare_submit(
            artifact_store=_infra().artifact_store,
            run_key=body.run_key,
            request_key=body.request_key,
            paths=list(body.paths),
        )
    except Exception as exc:
        raise _handled(exc) from exc

    return {
        "dispatch_id": prepared.dispatch_id,
        "request_upload": {
            "upload_url": prepared.request_upload.upload_url,
            "object_uri": prepared.request_upload.object_uri,
        },
        "sources": [
            {
                "logical_path": target.logical_path,
                "upload_url": target.upload_url,
                "object_uri": target.object_uri,
            }
            for target in prepared.sources
        ],
    }


@assistant_self_router.post("/pipeline/submit/publish")
async def pipeline_publish(request: Request, body: PipelinePublishRequest):
    """Emit one parse message per staged file and record the dispatch."""
    identity = await _authorised_identity(request, body.assistant_id)

    if len(body.logical_paths) != len(body.source_uris):
        raise HTTPException(
            status_code=400,
            detail=(
                "logical_paths and source_uris must correspond one to one; "
                f"got {len(body.logical_paths)} and {len(body.source_uris)}."
            ),
        )

    from unify_deploy.infra.pipeline_ops import UploadTarget, publish_submit

    sources = [
        UploadTarget(logical_path=path, upload_url="", object_uri=uri)
        for path, uri in zip(body.logical_paths, body.source_uris)
    ]

    try:
        job_ids = publish_submit(
            infra=_infra(),
            run_key=body.run_key,
            request_key=body.request_key,
            sources=sources,
            ingestion_mode=body.ingestion_mode,
            user_id=identity["user_id"],
            assistant_id=identity["assistant_id"],
            destination=body.destination,
            target_context=body.target_context,
            observability=body.observability,
        )
    except Exception as exc:
        raise _handled(exc) from exc

    return {"dispatch_id": body.run_key, "jobs": len(job_ids), "job_ids": job_ids}


@assistant_self_router.put("/pipeline/upload/{run_key}/{name}")
async def pipeline_upload(
    request: Request,
    run_key: str,
    name: str,
    assistant_id: int,
):
    """Accept object bytes for deployments whose store cannot sign a URL.

    The self-host path: its artifact store is a shared volume rather than an
    object store, so there is nothing to sign and the bytes come through here.
    Kept off the hosted path deliberately -- there a signed URL means this
    service never sees the payload at all.

    The key is composed here from the verified run, not taken from the caller,
    so an upload cannot be aimed at another run's namespace.
    """
    await _authorised_identity(request, assistant_id)

    from unify_deploy.infra.pipeline_ops import _safe_name

    safe = _safe_name(name)
    key = (
        f"jobs/{_safe_name(run_key)}/request.json"
        if safe == "request.json"
        else f"jobs/{_safe_name(run_key)}/sources/{safe}"
    )

    store = _infra().artifact_store
    signer = getattr(store, "signed_upload_url", None)
    if callable(signer):
        raise HTTPException(
            status_code=400,
            detail=(
                "This deployment issues signed upload URLs; upload to the URL "
                "returned by /infra/pipeline/submit rather than through here."
            ),
        )

    payload = await request.body()
    try:
        store.put_bytes(key, payload)
    except AttributeError as exc:
        raise HTTPException(
            status_code=500,
            detail="The configured artifact store cannot accept brokered uploads.",
        ) from exc
    except Exception as exc:
        raise _handled(exc) from exc

    return {"object_uri": key, "bytes": len(payload)}


# ---------------------------------------------------------------------------
# Observing and recovering
# ---------------------------------------------------------------------------


@assistant_self_router.get("/pipeline/status/{dispatch_id}")
async def pipeline_status(request: Request, dispatch_id: str, assistant_id: int):
    """Report the fleet's view of one dispatch, folded to a single state."""
    await _authorised_identity(request, assistant_id)

    from unify_deploy.infra.pipeline_ops import dispatch_status

    try:
        return dispatch_status(infra=_infra(), dispatch_id=dispatch_id)
    except Exception as exc:
        raise _handled(exc) from exc


@assistant_self_router.post("/pipeline/retry")
async def pipeline_retry(request: Request, body: PipelineRecoveryRequest):
    """Re-attempt part of a dispatch, serialised per job on a recovery lease."""
    await _authorised_identity(request, body.assistant_id)

    from unify_deploy.infra.pipeline_ops import retry_dispatch

    try:
        return await retry_dispatch(
            infra=_infra(),
            dispatch_id=body.dispatch_id,
            scope=body.scope,
        )
    except Exception as exc:
        raise _handled(exc) from exc


@assistant_self_router.post("/pipeline/cancel")
async def pipeline_cancel(request: Request, body: PipelineRecoveryRequest):
    """Abandon a dispatch's remaining work, keeping what already committed."""
    await _authorised_identity(request, body.assistant_id)

    from unify_deploy.infra.pipeline_ops import cancel_dispatch

    try:
        return cancel_dispatch(infra=_infra(), dispatch_id=body.dispatch_id)
    except Exception as exc:
        raise _handled(exc) from exc


@assistant_self_router.post("/pipeline/pause")
async def pipeline_pause(request: Request, body: PipelineRecoveryRequest):
    """Stop a dispatch but keep its outstanding work for a later resume."""
    await _authorised_identity(request, body.assistant_id)

    from unify_deploy.infra.pipeline_ops import pause_dispatch

    try:
        return pause_dispatch(infra=_infra(), dispatch_id=body.dispatch_id)
    except Exception as exc:
        raise _handled(exc) from exc


@assistant_self_router.post("/pipeline/resume")
async def pipeline_resume(request: Request, body: PipelineRecoveryRequest):
    """Replay a paused dispatch's parked work in its original order."""
    await _authorised_identity(request, body.assistant_id)

    from unify_deploy.infra.pipeline_ops import resume_dispatch

    try:
        return resume_dispatch(infra=_infra(), dispatch_id=body.dispatch_id)
    except Exception as exc:
        raise _handled(exc) from exc


@assistant_self_router.get("/pipeline/health")
async def pipeline_health():
    """Report whether a fleet is actually reachable from here.

    Unauthenticated on purpose: it answers "is there a control plane here at
    all", which is what the caller needs before it can decide where to run
    work, and it discloses nothing about any tenant. The answer is a probe of
    the queue and the store rather than a constant, because a plane that is
    configured but cannot reach its backends must not read as healthy -- the
    caller would dispatch work that then goes nowhere.
    """
    try:
        infra = _infra()
        bucket = getattr(infra.settings.artifact_store, "bucket", "")
        project = getattr(infra.settings.pubsub, "project_id", "")
        reachable = bool(bucket and project)
    except Exception as exc:
        logger.warning("Pipeline control plane is not usable: %s", exc)
        return {"ok": False, "detail": str(exc)}
    return {"ok": reachable, "bucket": bucket, "project": project}
