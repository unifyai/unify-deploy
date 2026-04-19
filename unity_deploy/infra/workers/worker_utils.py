"""Shared bootstrap utilities for GCP pipeline workers."""

from __future__ import annotations

import json
import logging
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from google.cloud import storage

from unity.common.pipeline.artifact_store import ArtifactStore
from unity.common.pipeline.cost_ledger import CostLedger
from unity.common.pipeline.deployment.types import (
    DeploymentBundleStore,
    DeploymentJobStore,
)
from unity.common.pipeline.run_ledger import RunLedger
from unity.common.pipeline.work_queue import WorkQueue

from unity_deploy.infra.gcp.settings import GcpPipelineSettings

logger = logging.getLogger(__name__)

_shutdown_requested = False


# ---------------------------------------------------------------------------
# Typed infrastructure container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerInfra:
    """Typed bag of protocol-level adapters assembled during worker bootstrap.

    All fields except ``settings`` and ``storage_client`` reference
    protocol types defined in ``unity.common.pipeline``, keeping the
    workers decoupled from the concrete GCP adapter classes.
    """

    artifact_store: ArtifactStore
    work_queue: WorkQueue
    run_ledger_factory: Callable[[str], RunLedger]
    cost_ledger_factory: Callable[[str], CostLedger]
    bundle_store: DeploymentBundleStore
    job_store: DeploymentJobStore
    settings: GcpPipelineSettings
    storage_client: storage.Client


# ---------------------------------------------------------------------------
# Shutdown flag
# ---------------------------------------------------------------------------


def is_shutdown_requested() -> bool:
    return _shutdown_requested


def install_signal_handlers() -> None:
    """Install SIGTERM/SIGINT handlers for graceful worker shutdown."""

    def _handler(signum: int, _frame: Any) -> None:
        global _shutdown_requested
        sig_name = signal.Signals(signum).name
        logger.info("Received %s — requesting graceful shutdown", sig_name)
        _shutdown_requested = True

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


# ---------------------------------------------------------------------------
# Environment bootstrap
# ---------------------------------------------------------------------------


def initialize_worker_environment(*, debug: bool = False) -> Path:
    """Bootstrap the worker process: load .env, configure logging.

    Returns the resolved project root path.
    """
    from unity_deploy.customization.scripts.ingest_utils import initialize_environment

    return initialize_environment(debug=debug, sdk_log=False)


# ---------------------------------------------------------------------------
# GCP client factory
# ---------------------------------------------------------------------------


def build_gcp_clients() -> tuple[
    storage.Client,
    "pubsub_v1.PublisherClient",
    "pubsub_v1.SubscriberClient",
]:
    """Create authenticated GCP clients from environment / SA key."""
    from google.cloud import pubsub_v1
    from google.oauth2 import service_account

    sa_key_json = os.environ.get("GCP_SA_KEY", "")
    if sa_key_json:
        info = json.loads(sa_key_json)
        credentials = service_account.Credentials.from_service_account_info(info)
        storage_client = storage.Client(credentials=credentials)
        publisher = pubsub_v1.PublisherClient(credentials=credentials)
        subscriber = pubsub_v1.SubscriberClient(credentials=credentials)
    else:
        storage_client = storage.Client()
        publisher = pubsub_v1.PublisherClient()
        subscriber = pubsub_v1.SubscriberClient()

    return storage_client, publisher, subscriber


# ---------------------------------------------------------------------------
# Infrastructure assembly
# ---------------------------------------------------------------------------


def build_worker_infra(
    *,
    settings: GcpPipelineSettings | None = None,
) -> WorkerInfra:
    """Assemble the full GCP adapter stack for a worker process."""
    from unity_deploy.infra.gcp.artifact_store import GcsArtifactStore
    from unity_deploy.infra.gcp.deployment_stores import (
        GcsDeploymentBundleStore,
        GcsDeploymentJobStore,
    )
    from unity_deploy.infra.gcp.work_queue import PubSubWorkQueue

    if settings is None:
        settings = GcpPipelineSettings()

    storage_client, publisher, subscriber = build_gcp_clients()

    artifact_store = GcsArtifactStore(
        client=storage_client,
        settings=settings.artifact_store,
    )
    job_store = GcsDeploymentJobStore(artifact_store=artifact_store)

    work_queue = PubSubWorkQueue(
        publisher=publisher,
        subscriber=subscriber,
        settings=settings.pubsub,
        environment=settings.environment,
        cancellation_store=artifact_store,
    )

    bundle_store = GcsDeploymentBundleStore(artifact_store=artifact_store)

    def run_ledger_factory(run_id: str) -> RunLedger:
        from unity_deploy.infra.gcp.ledgers import GcsRunLedger

        return GcsRunLedger(
            client=storage_client,
            settings=settings.ledger,  # type: ignore[union-attr]
            run_id=run_id,
            environment=settings.environment,  # type: ignore[union-attr]
        )

    def cost_ledger_factory(run_id: str) -> CostLedger:
        from unity_deploy.infra.gcp.ledgers import GcsCostLedger

        return GcsCostLedger(
            client=storage_client,
            settings=settings.ledger,  # type: ignore[union-attr]
            run_id=run_id,
            environment=settings.environment,  # type: ignore[union-attr]
        )

    return WorkerInfra(
        artifact_store=artifact_store,
        work_queue=work_queue,
        run_ledger_factory=run_ledger_factory,
        cost_ledger_factory=cost_ledger_factory,
        bundle_store=bundle_store,
        job_store=job_store,
        settings=settings,
        storage_client=storage_client,
    )


# ---------------------------------------------------------------------------
# Unify context activation (lightweight — no EventBus / LLM hooks)
# ---------------------------------------------------------------------------


def activate_unify_context(
    project_name: str = "Assistants",
    *,
    user_id: str | None = None,
    assistant_id: str | None = None,
) -> None:
    """Lightweight Unify context activation for worker processes.

    Performs only what ``DataManager.ingest`` requires:

    1. ``unify.activate(project_name)``
    2. ``unify.set_context("{user_id}/{assistant_id}")``

    Does **not** initialise the EventBus, LLM hooks, spending-limit
    hooks, billing context, or ``SESSION_DETAILS``.  Does **not**
    require ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY``.

    Identity is resolved from explicit arguments first, falling back to
    ``USER_ID`` / ``ASSISTANT_ID`` environment variables, then to safe
    defaults (``"default"`` / ``"0"``).

    ``UNIFY_KEY`` must be present in the environment for authenticated
    Unify SDK calls.
    """
    import unify as _unify

    if not os.environ.get("UNIFY_KEY"):
        raise EnvironmentError(
            "UNIFY_KEY must be set for Unify SDK calls. "
            "Ensure the K8s deployment or .env file sets it.",
        )

    project_name = os.environ.get("UNIFY_PROJECT_NAME", project_name)
    uid = user_id or os.environ.get("USER_ID", "default")
    aid = assistant_id or os.environ.get("ASSISTANT_ID", "0")

    if not _unify.active_project():
        _unify.activate(project_name)

    ctx = f"{uid}/{aid}"
    try:
        _unify.set_context(ctx)
    except Exception as e:
        if "already exists" in str(e).lower():
            _unify.set_context(ctx, skip_create=True)
        else:
            raise

    logger.info(
        "Unify context activated: project=%s, context=%s",
        project_name,
        ctx,
    )
