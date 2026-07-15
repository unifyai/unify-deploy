"""AssistantJobs lifecycle helpers for the Unity container.

Thin wrapper around ``assistant_jobs_api`` that reads session-specific
values (``SESSION_DETAILS``, ``SETTINGS``) and records Prometheus
metrics.  All actual HTTP operations live in ``assistant_jobs_api.py``
which is shared with the job-watcher operator.

``log_job_startup`` creates the AssistantJobs audit record with all
assistant/user info from ``SESSION_DETAILS`` plus the container-specific
``job_name``.  ``update_liveview_url`` may later add the desktop URL.
Both write through ownership-scoped ``/infra/assistant-jobs/*`` using the
assistant's own ``UNIFY_KEY`` — pods never hold a shared or admin key.
The job-watcher operator handles crash-safe VM release independently.
"""

from dotenv import load_dotenv

load_dotenv()
import os
import threading
import time
import traceback
from datetime import datetime, timezone

from unify.logger import LOGGER
from unify.common.hierarchical_logger import ICONS
from unify_deploy.runtime.assistant_jobs_api import (
    patch_job_label,
    patch_liveview_via_infra,
    post_startup_via_infra,
    release_pool_vm,
    stop_assistant_session,
)
from unify.conversation_manager.metrics import (
    session_duration as _m_session_dur,
)
from unify.session_details import SESSION_DETAILS
from unify.settings import SETTINGS

# Session start time (perf_counter), set by log_job_startup, read by mark_job_done
_session_start_perf: float | None = None

# Signalled by log_job_startup (success or failure) so that update_liveview_url
# can wait for the record to exist before attempting to patch it.
_log_created = threading.Event()


def _warn_legacy_shared_key() -> None:
    """Warn if a stale deploy still mounts SHARED_UNIFY_KEY on the pod."""
    if os.environ.get("SHARED_UNIFY_KEY"):
        LOGGER.warning(
            f"{ICONS['assistant_jobs']} [assistant_jobs] SHARED_UNIFY_KEY is set but ignored; "
            f"AssistantJobs writes use /infra/assistant-jobs/* with UNIFY_KEY.",
        )


def mark_job_label(
    job_name: str,
    status: str,
    assistant_id: str | None = None,
    ack_ts: str | None = None,
    timeout: float = 30,
    retries: int = 0,
) -> bool:
    """Patch the K8s Job unity-status label via the communication service.

    Authenticates as this assistant (its own ``UNIFY_KEY``); Comms self-scopes
    the patch to the caller's bound Job, so the assistant id is always sent.
    Returns True on success, False on failure or if config is missing.
    """
    comms_url = SETTINGS.conversation.COMMS_URL.rstrip("/")
    unify_key = SESSION_DETAILS.unify_key
    if assistant_id is None:
        assistant_id = (
            str(SESSION_DETAILS.assistant.agent_id)
            if SESSION_DETAILS.assistant.agent_id is not None
            else None
        )
    if not comms_url or not unify_key:
        LOGGER.debug(
            f"{ICONS['assistant_jobs']} [assistant_jobs] Skipping label update: COMMS_URL or UNIFY_KEY not configured",
        )
        return False
    ok = patch_job_label(
        comms_url,
        unify_key,
        job_name,
        status,
        assistant_id,
        ack_ts=ack_ts,
        timeout=timeout,
        retries=retries,
    )
    if ok:
        LOGGER.debug(
            f"{ICONS['assistant_jobs']} [assistant_jobs] Marked job as {status}: {job_name}",
        )
    else:
        LOGGER.warning(
            f"{ICONS['assistant_jobs']} [assistant_jobs] Failed to mark job as {status}: {job_name}",
        )
    return ok


def log_job_startup(
    job_name: str,
    user_id: str,
    assistant_id: str,
    medium: str = "",
):
    """Create an AssistantJobs audit record for this container session.

    Logs all available assistant/user info from ``SESSION_DETAILS`` plus
    the container-specific ``job_name``.  ``update_liveview_url`` may
    later add the desktop URL.
    """
    _warn_legacy_shared_key()

    comms_url = SETTINGS.conversation.COMMS_URL.rstrip("/")
    unify_key = SESSION_DETAILS.unify_key
    if not comms_url or not unify_key:
        LOGGER.debug(
            f"{ICONS['assistant_jobs']} [assistant_jobs] Skipping log_job_startup: "
            f"COMMS_URL or UNIFY_KEY not configured",
        )
        _log_created.set()
        return

    try:
        sd = SESSION_DETAILS
        ok = post_startup_via_infra(
            comms_url,
            unify_key,
            assistant_id=str(assistant_id),
            user_id=user_id,
            job_name=job_name,
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            medium=medium,
            user_name=f"{sd.user.first_name} {sd.user.surname}".strip(),
            assistant_name=f"{sd.assistant.first_name} {sd.assistant.surname}".strip(),
            user_number=sd.user.number,
            assistant_number=sd.assistant.number,
            user_email=sd.user.email,
            assistant_email=sd.assistant.email,
        )
        if ok:
            LOGGER.debug(
                f"{ICONS['assistant_jobs']} [assistant_jobs] Created audit record: "
                f"job_name={job_name}, assistant_id={assistant_id}",
            )
            global _session_start_perf
            _session_start_perf = time.perf_counter()
        else:
            LOGGER.error(
                f"{ICONS['assistant_jobs']} [assistant_jobs] Failed creating job record "
                f"via infra for assistant_id={assistant_id}",
            )
    except Exception as e:
        LOGGER.error(
            f"{ICONS['assistant_jobs']} [assistant_jobs] Error creating job record: {e}",
        )
        traceback.print_exc()
    finally:
        _log_created.set()


def update_liveview_url(assistant_id: str, user_id: str, liveview_url: str) -> None:
    """Update the AssistantJobs record with the resolved liveview_url.

    Called by the ``AssistantDesktopReady`` event handler once the VM is
    confirmed ready.  Finds the record by ``assistant_id`` + ``job_name``
    (unique to this container session).
    """
    _warn_legacy_shared_key()

    comms_url = SETTINGS.conversation.COMMS_URL.rstrip("/")
    unify_key = SESSION_DETAILS.unify_key
    job_name = SETTINGS.conversation.JOB_NAME
    if not comms_url or not unify_key or not job_name:
        return

    if not _log_created.wait(timeout=60):
        LOGGER.warning(
            f"{ICONS['assistant_jobs']} [assistant_jobs] Timed out waiting for audit record creation; "
            f"liveview_url update skipped",
        )
        return

    try:
        ok = patch_liveview_via_infra(
            comms_url,
            unify_key,
            assistant_id=str(assistant_id),
            job_name=job_name,
            liveview_url=liveview_url,
        )
        if ok:
            LOGGER.debug(
                f"{ICONS['assistant_jobs']} [assistant_jobs] Updated record with liveview_url={liveview_url}",
            )
        else:
            LOGGER.warning(
                f"{ICONS['assistant_jobs']} [assistant_jobs] No audit record updated for "
                f"assistant_id={assistant_id}, job_name={job_name}; liveview_url not persisted",
            )
    except Exception as e:
        LOGGER.error(
            f"{ICONS['assistant_jobs']} [assistant_jobs] Error updating liveview_url: {e}",
        )


def mark_job_done(
    job_name: str,
    inactivity_timeout: float = 0.0,
    shutdown_reason: str | None = None,
):
    """Mark a job as done, release VM, and record session duration.

    When ``shutdown_reason`` is ``"idle_timeout"``, Unity also asks Comms to
    stop the current AssistantSession before the Job becomes terminal. This
    preserves the user's intent to go offline while leaving crash paths on the
    existing restart behavior.

    The job-watcher operator performs crash-safe VM release independently.
    """
    assistant_id_value = SESSION_DETAILS.assistant.agent_id
    assistant_id = str(assistant_id_value) if assistant_id_value is not None else ""
    comms_url = SETTINGS.conversation.COMMS_URL.rstrip("/")
    # Lifecycle calls authenticate as this assistant (its own UNIFY_KEY), which
    # Comms self-scopes to this session; no platform admin key on the pod.
    unify_key = SESSION_DETAILS.unify_key

    if shutdown_reason == "idle_timeout" and comms_url and unify_key and assistant_id:
        stop_assistant_session(comms_url, unify_key, assistant_id)

    mark_job_label(job_name, "done", assistant_id=assistant_id)

    # U9: session duration (log_job_startup -> mark_job_done), excluding idle tail
    if _session_start_perf is not None:
        total_dur = time.perf_counter() - _session_start_perf
        active_dur = max(0.0, total_dur - inactivity_timeout)
        _m_session_dur.record(active_dur)
        LOGGER.debug(
            f"{ICONS['assistant_jobs']} [assistant_jobs] Session duration: "
            f"{total_dur:.1f}s total, {inactivity_timeout:.1f}s idle, {active_dur:.1f}s active",
        )

    # Release pool VM if applicable (managed VM, not user's own desktop)
    if (
        comms_url
        and unify_key
        and SESSION_DETAILS.assistant.desktop_mode in ("windows", "ubuntu")
    ):
        binding_id = SESSION_DETAILS.assistant.binding_id
        if not binding_id:
            LOGGER.warning(
                "%s [assistant_jobs] Skipping pool VM release for %s because binding_id is missing",
                ICONS["assistant_jobs"],
                assistant_id,
            )
            return
        release_pool_vm(
            comms_url,
            unify_key,
            assistant_id,
            binding_id,
            job_name=job_name,
        )


class HostedAssistantJobsBackend:
    def mark_job_label(
        self,
        job_name: str,
        status: str,
        assistant_id: str | None = None,
        ack_ts: str | None = None,
        timeout: float = 30,
        retries: int = 0,
    ) -> bool:
        return mark_job_label(
            job_name,
            status,
            assistant_id=assistant_id,
            ack_ts=ack_ts,
            timeout=timeout,
            retries=retries,
        )

    def log_job_startup(
        self,
        job_name: str,
        user_id: str,
        assistant_id: str,
        medium: str = "",
    ) -> None:
        log_job_startup(job_name, user_id, assistant_id, medium=medium)

    def update_liveview_url(
        self,
        assistant_id: str,
        user_id: str,
        liveview_url: str,
    ) -> None:
        update_liveview_url(assistant_id, user_id, liveview_url)

    def mark_job_done(
        self,
        job_name: str,
        inactivity_timeout: float = 0.0,
        shutdown_reason: str | None = None,
    ) -> None:
        mark_job_done(
            job_name,
            inactivity_timeout=inactivity_timeout,
            shutdown_reason=shutdown_reason,
        )
