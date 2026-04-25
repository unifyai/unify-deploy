"""Shared bootstrap utilities for GCP pipeline workers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from unity.common.pipeline.artifact_store import ArtifactStore
from unity.common.pipeline.cost_ledger import CostLedger
from unity.common.pipeline.deployment.types import (
    DeploymentBundleStore,
    DeploymentJobStore,
)
from unity.common.pipeline.run_ledger import RunLedger
from unity.common.pipeline.work_queue import WorkQueue

from unity_deploy.infra.gcp.settings import GcpPipelineSettings

from google.cloud import storage

if TYPE_CHECKING:
    from google.cloud import pubsub_v1


logger = logging.getLogger(__name__)

# Module-level shutdown Event used by all worker loops. Set on
# SIGTERM/SIGINT via ``install_signal_handlers``. Kept as an
# ``asyncio.Event`` (rather than a bare bool) so that async sleeps can
# wake immediately when shutdown is requested via ``shutdown_aware_sleep``.
_shutdown_event: asyncio.Event | None = None


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
    # ``heartbeat_ledger_factory`` is a parallel ledger keyed by the
    # same ``run_id`` but writing to ``heartbeats.jsonl`` instead of
    # ``run_ledger.jsonl``. Used exclusively by ``LeaseExtender`` to
    # record periodic liveness signals without contending with the
    # main stage/file/run ledger buffer.
    heartbeat_ledger_factory: Callable[[str], RunLedger]
    cost_ledger_factory: Callable[[str], CostLedger]
    bundle_store: DeploymentBundleStore
    job_store: DeploymentJobStore
    settings: GcpPipelineSettings
    storage_client: storage.Client


# ---------------------------------------------------------------------------
# Shutdown flag
# ---------------------------------------------------------------------------


def is_shutdown_requested() -> bool:
    """Return True once SIGTERM or SIGINT has been observed."""
    return _shutdown_event is not None and _shutdown_event.is_set()


def get_shutdown_event() -> asyncio.Event:
    """Return the shared shutdown Event.

    ``install_signal_handlers`` must have been called first; otherwise
    no signal → Event bridge exists and callers would wait forever.
    """
    if _shutdown_event is None:
        raise RuntimeError(
            "install_signal_handlers() must be called before " "get_shutdown_event()",
        )
    return _shutdown_event


class LeaseExtender:
    """Background task that periodically extends a Pub/Sub lease.

    One instance per in-flight message: started after ``receive()``,
    stopped (in a ``finally`` block) when the handler terminates via
    success, retry, or dead-letter.

    Rationale:

    * Messages take variable amounts of time (parsing a 100-page PDF
      can exceed the 10-minute default ack deadline). Without lease
      extension, Pub/Sub would redeliver to a second pod mid-work,
      producing duplicate load.
    * Keeping extension fully decoupled from the handler means the
      handler never has to manage its own Pub/Sub concerns. If the
      handler hangs, the extender keeps the lease alive until the pod
      itself terminates (24h grace period), at which point the process
      dies, extensions stop, and Pub/Sub redelivers. Pod liveness
      becomes the ultimate watchdog.
    * ``last_progress_at`` timestamps each extension as a coarse
      heartbeat visible in kubectl logs — ops can tell at a glance if
      a particular pod is actively working on a message.
    * When a ``run_ledger`` is supplied, each successful extension
      also writes a ``PipelineHeartbeatManifest`` to the ledger. This
      turns the lease-extension tick into a persisted liveness signal
      ops can query across all active runs to detect hung pods
      (``max(now - last_progress_at) > threshold`` pages on-call).
      Heartbeat ledger writes are best-effort: a GCS outage must NOT
      stop the Pub/Sub lease from being extended, so failures there
      are logged-and-swallowed.

    Extension cadence is chosen so the deadline never lapses: we
    extend by ``extension_seconds`` (default 600 = 10 min) every
    ``period_seconds`` (default 300 = 5 min), leaving a 5-minute
    safety margin against clock drift / request latency.
    """

    def __init__(
        self,
        *,
        work_queue: WorkQueue,
        receipt_id: str,
        job_id: str | None = None,
        period_seconds: float = 300.0,
        extension_seconds: int = 600,
        run_ledger: RunLedger | None = None,
        run_id: str | None = None,
        stage: str | None = None,
    ):
        self._work_queue = work_queue
        self._receipt_id = receipt_id
        self._job_id = job_id
        self._period_s = period_seconds
        self._extension_s = extension_seconds
        self._run_ledger = run_ledger
        self._run_id = run_id
        self._stage = stage
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._started_at: float = 0.0
        self._last_progress_at: float = 0.0
        self._extensions: int = 0

        # Heartbeat persistence requires run_id + stage. If only one
        # is supplied we treat it as a wiring bug (loud error instead
        # of silently dropping heartbeats), but only when the caller
        # actually asked for a ledger.
        if self._run_ledger is not None and (
            self._run_id is None or self._stage is None
        ):
            raise ValueError(
                "LeaseExtender with run_ledger requires run_id and stage "
                "so heartbeats can be attributed to a specific run.",
            )

    def start(self) -> None:
        self._started_at = time.monotonic()
        self._last_progress_at = self._started_at
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Signal the extender to stop and wait for it to exit."""
        self._stop_event.set()
        task = self._task
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _write_heartbeat(self, elapsed: float) -> None:
        """Append a liveness record to the heartbeat ledger.

        Best-effort: any failure here is caught and logged so a GCS
        outage never interferes with the Pub/Sub lease extension
        itself (which is the load-bearing part of this class).
        """
        ledger = self._run_ledger
        if ledger is None or self._run_id is None or self._stage is None:
            return
        try:
            from unity.common.pipeline import PipelineHeartbeatManifest

            manifest = PipelineHeartbeatManifest(
                run_id=self._run_id,
                stage=self._stage,  # type: ignore[arg-type]
                elapsed_seconds=elapsed,
                extensions_emitted=self._extensions,
                receipt_id=self._receipt_id,
                job_id=self._job_id,
            )
            ledger.write(manifest)
        except Exception:
            logger.exception(
                "Heartbeat write failed (run=%s stage=%s receipt=%s)",
                self._run_id,
                self._stage,
                self._receipt_id,
            )

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._period_s,
                    )
                    return
                except asyncio.TimeoutError:
                    pass

                try:
                    await self._work_queue.extend_lease(
                        self._receipt_id,
                        self._extension_s,
                    )
                    self._extensions += 1
                    self._last_progress_at = time.monotonic()
                    elapsed = self._last_progress_at - self._started_at
                    logger.info(
                        "Lease extended (job=%s receipt=%s "
                        "elapsed=%.0fs extensions=%d last_progress_at=+%.0fs)",
                        self._job_id or "?",
                        self._receipt_id,
                        elapsed,
                        self._extensions,
                        elapsed,
                    )
                    # Persist heartbeat AFTER a successful extension so
                    # a heartbeat row never implies "still alive" when
                    # the lease has actually lapsed.
                    self._write_heartbeat(elapsed)
                except Exception:
                    logger.exception(
                        "Lease extension failed (job=%s receipt=%s)",
                        self._job_id or "?",
                        self._receipt_id,
                    )
        except asyncio.CancelledError:
            pass


async def shutdown_aware_sleep(seconds: float) -> bool:
    """Sleep for ``seconds`` or until shutdown is requested.

    Returns True if shutdown was requested during the sleep (caller
    should exit its consumer loop), False if the full duration elapsed
    without a shutdown signal.

    Workers use this on the empty-queue backoff path so a SIGTERM mid-
    sleep wakes the loop immediately rather than blocking for up to
    the full backoff window.
    """
    event = get_shutdown_event()
    if event.is_set():
        return True
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


def install_signal_handlers() -> None:
    """Install SIGTERM/SIGINT handlers for graceful worker shutdown.

    Creates the module-level ``_shutdown_event`` and wires the POSIX
    signals to ``asyncio.Event.set``. On Linux (the deployment target)
    this uses ``loop.add_signal_handler`` which runs the handler on the
    event loop itself, so the Event transitions are visible to
    ``asyncio.wait_for`` without any cross-thread bridging. For
    environments where ``add_signal_handler`` is unavailable (Windows,
    non-main threads), we fall back to the sync ``signal.signal``
    bridge — the Event mutation is safe because Python's signal
    dispatch runs between bytecodes on the main thread.
    """
    global _shutdown_event
    loop = asyncio.get_running_loop()
    _shutdown_event = asyncio.Event()

    def _on_signal(signum: int) -> None:
        sig_name = (
            signal.Signals(signum).name if isinstance(signum, int) else str(signum)
        )
        logger.info("Received %s — requesting graceful shutdown", sig_name)
        if _shutdown_event is not None:
            _shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda s, _f: _on_signal(s))


# ---------------------------------------------------------------------------
# Environment bootstrap
# ---------------------------------------------------------------------------


def initialize_worker_environment(*, debug: bool = False) -> Path:
    """Bootstrap a worker/runtime process without touching repo-local ``.env``.

    Worker pods should read configuration only from their real process
    environment / settings objects. They must not delegate to the old
    standalone ingest-script bootstrap, which loads ``<repo>/.env`` for
    local developer convenience.

    Returns the resolved repository root path for callers that need it.
    """
    from unity_deploy.utils.load_repo_env import unity_deploy_repo_root

    level = logging.DEBUG if debug else logging.INFO
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root_logger.addHandler(console)

    return unity_deploy_repo_root()


# ---------------------------------------------------------------------------
# GCP client factory
# ---------------------------------------------------------------------------


def build_gcp_clients() -> tuple[
    storage.Client,
    pubsub_v1.PublisherClient,
    pubsub_v1.SubscriberClient,
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

    # Ledgers intentionally share ``artifact_store`` settings — the
    # bucket is env-scoped via the bucket name (e.g.
    # ``unity-pipeline-artifacts-staging``), so staging ledgers cannot
    # leak into the production bucket by construction.
    ledger_settings = settings.artifact_store

    def run_ledger_factory(run_id: str) -> RunLedger:
        from unity_deploy.infra.gcp.ledgers import GcsRunLedger

        return GcsRunLedger(
            client=storage_client,
            settings=ledger_settings,
            run_id=run_id,
        )

    def heartbeat_ledger_factory(run_id: str) -> RunLedger:
        from unity_deploy.infra.gcp.ledgers import GcsRunLedger

        # Flush threshold of 1 = every heartbeat tick round-trips to
        # GCS immediately, so ops monitoring sees near-real-time
        # progress (heartbeats are tiny, cost is negligible).
        return GcsRunLedger(
            client=storage_client,
            settings=ledger_settings,
            run_id=run_id,
            blob_basename="heartbeats.jsonl",
            flush_threshold=1,
        )

    def cost_ledger_factory(run_id: str) -> CostLedger:
        from unity_deploy.infra.gcp.ledgers import GcsCostLedger

        return GcsCostLedger(
            client=storage_client,
            settings=ledger_settings,
            run_id=run_id,
        )

    return WorkerInfra(
        artifact_store=artifact_store,
        work_queue=work_queue,
        run_ledger_factory=run_ledger_factory,
        heartbeat_ledger_factory=heartbeat_ledger_factory,
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
    user_id: str,
    assistant_id: str,
    managers: list | None = None,
) -> None:
    """Lightweight ``unity.init``-style activation for worker processes.

    Mirrors the core sequence from :pyfunc:`unity.init` — project
    activation, SDK context setup, and ``ContextRegistry`` provisioning —
    but scoped to an explicitly supplied identity and manager list rather
    than ``SESSION_DETAILS`` and the full manager catalogue.

    Steps (matching ``unity.init`` order):

    1. ``unify.activate(project_name)``  (once per process)
    2. Reset the SDK context via ``unset_context()`` to prevent the
       relative-join accumulation bug across sequential messages.
    3. ``unify.set_context("{user_id}/{assistant_id}")``  — idempotent,
       tolerates concurrent creation.
    4. ``ContextRegistry.clear()`` + ``setup_for_managers(managers)`` —
       purge stale cached paths and provision only the contexts the
       caller actually needs.

    Does **not** initialise the EventBus, LLM hooks, spending-limit
    hooks, billing context, or ``SESSION_DETAILS``.  Does **not**
    require ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY``.

    Parameters
    ----------
    project_name :
        Unify project to activate.
    user_id, assistant_id :
        Identity from the ``IngestBinding`` on the current message.
    managers :
        Manager **classes** whose ``Config.required_contexts`` should be
        provisioned (e.g. ``[FileManager, DataManager]``).  When *None*
        the ``ContextRegistry`` is cleared but no contexts are created;
        downstream managers will still resolve lazily on first access.
    """
    import unify as _unify

    from unity.common.context_registry import ContextRegistry

    if not os.environ.get("UNIFY_KEY"):
        raise EnvironmentError(
            "UNIFY_KEY must be set for Unify SDK calls. "
            "Ensure the per-message resolver installed it before "
            "activate_unify_context() runs.",
        )

    # --- 1. Project activation (once per process) ---
    project_name = os.environ.get("UNIFY_PROJECT_NAME", project_name)
    if not _unify.active_project():
        _unify.activate(project_name)

    # --- 2+3. Context reset & set (mirrors unity.init lines 92-102) ---
    # The SDK's set_context defaults to relative=True, which _joins_ the
    # new path onto the current one.  In a long-lived worker that
    # processes many messages, this would produce
    # "user/1821/user/1821/..." after the second call.  Resetting first
    # ensures an absolute set regardless of prior state.
    _unify.unset_context()

    ctx = f"{user_id}/{assistant_id}"
    try:
        _unify.set_context(ctx)
    except Exception as e:
        if "already exists" in str(e).lower():
            _unify.set_context(ctx, skip_create=True)
        else:
            raise

    # --- 4. ContextRegistry (mirrors unity.init line 104) ---
    ContextRegistry.clear()
    if managers:
        ContextRegistry.setup_for_managers(managers)

    logger.info(
        "Unify context activated: project=%s, context=%s",
        project_name,
        ctx,
    )
