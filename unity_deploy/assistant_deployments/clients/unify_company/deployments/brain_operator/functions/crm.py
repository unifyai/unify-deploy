"""
CRM scheduled-job wrappers for the brain_operator deployment.

Each function here matches a ``BrainScheduledJob.entrypoint_function``
value declared in :mod:`brain.crm.scheduled_jobs`.  The wrappers stay
intentionally thin: they import brain lazily, call the right brain
helper, and return a JSON-serialisable summary the offline-dispatcher
persists as the task result.

The legacy ``default`` deployment's ``functions/crm_jobs.py`` is kept
in place for backwards compatibility; over time the canonical wrappers
will be the ones here.
"""

from __future__ import annotations

from typing import Any

from droid.function_manager.custom import custom_function


@custom_function()
async def ingest_gmail_mailbox(
    *,
    mailbox: str,
    execute: bool = False,
) -> dict[str, Any]:
    """Ingest cached Gmail messages for one mailbox into CRM evidence tables.

    Mirrors the existing helper in the default deployment so the
    brain_operator can take ownership of the recurring CRM Gmail sync
    tick declared in :mod:`brain.crm.scheduled_jobs`.
    """

    from brain.sync.gmail import ingest_cached_mailbox

    result = ingest_cached_mailbox(mailbox=mailbox, dry_run=not execute)
    return result.__dict__


@custom_function()
async def sync_fireflies_and_associate() -> dict[str, Any]:
    """Pull Fireflies transcripts and regenerate CRM associations."""

    from brain.sync.fireflies import (
        associate_cached_transcripts,
        sync_transcripts,
    )

    export = sync_transcripts()
    assoc = associate_cached_transcripts()
    return {
        "summaries_visible": export.summaries_visible,
        "full_transcripts_cached": export.full_transcripts_cached,
        "full_transcripts_missing": export.full_transcripts_missing,
        "matched_transcripts": assoc.transcripts_with_account_matches,
    }


@custom_function()
async def refresh_account_digests() -> dict[str, Any]:
    """Refresh CRM account digests.

    Stub: brain doesn't yet ship a deterministic digest builder.  The
    function exists so the registry can plant the Tasks row (which
    surfaces "needs implementation" to the operator) without breaking
    the install path.
    """

    return {
        "status": "not_implemented",
        "summary": (
            "refresh_account_digests is a stub.  Implement "
            "brain.crm.dashboards.refresh_account_digests() and wire "
            "it here when the digest builder lands."
        ),
    }


@custom_function()
async def run_crm_hygiene_review() -> dict[str, Any]:
    """Flag companies missing owner / stage / next action / evidence."""

    from brain.crm.dashboards import accounts_missing_next_action

    payload = accounts_missing_next_action(limit=100)
    return {
        "status": "ok",
        "missing_next_action_count": len(payload),
        "items": payload,
    }


@custom_function()
async def build_pipeline_review_pack() -> dict[str, Any]:
    """Build the weekly pipeline-review pack from CRM read models."""

    from brain.crm.dashboards import crm_overview, pipeline_by_stage

    return {
        "status": "ok",
        "overview": crm_overview(),
        "pipeline": pipeline_by_stage(),
    }
