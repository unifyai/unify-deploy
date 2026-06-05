"""Callable CRM job wrappers for the Unify company brain."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
def sync_fireflies_and_associate() -> dict:
    """Pull Fireflies transcripts and regenerate local CRM associations."""
    from brain.sync.fireflies import associate_cached_transcripts, sync_transcripts

    export = sync_transcripts()
    assoc = associate_cached_transcripts()
    return {
        "summaries_visible": export.summaries_visible,
        "full_transcripts_cached": export.full_transcripts_cached,
        "full_transcripts_missing": export.full_transcripts_missing,
        "matched_transcripts": assoc.transcripts_with_account_matches,
    }


@custom_function()
def refresh_fireflies_crm_evidence(*, execute: bool = False) -> dict:
    """Ingest cached Fireflies account associations into CRM evidence tables."""
    from brain.sync.fireflies import ingest_associations_to_crm

    result = ingest_associations_to_crm(dry_run=not execute)
    return result.__dict__


@custom_function()
def ingest_gmail_mailbox(*, mailbox: str, execute: bool = False) -> dict:
    """Ingest cached Gmail messages for one mailbox into CRM evidence tables."""
    from brain.sync.gmail import ingest_cached_mailbox

    result = ingest_cached_mailbox(mailbox=mailbox, dry_run=not execute)
    return result.__dict__
