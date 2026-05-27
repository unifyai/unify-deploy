"""
Intelligence-gathering wrappers for the brain_operator deployment.

Each function here is a thin adapter onto a brain entrypoint that
produces a daily / weekly intel digest the operator consumes via
WhatsApp.  The canonical example is the HackerNews top-stories digest,
which uses :class:`ComputerPrimitives` to scrape ``news.ycombinator.com``
(no API), summarises the top stories via the LLM-backed
``observe()`` call, and sends the resulting bullet list to the
operator's WhatsApp number through the deployed Unity gateway.

The pattern is the canonical recipe for browser-driven scheduled
intel:

1.  Declare a ``BrainScheduledJob`` in
    ``brain/intel/<source>/scheduled_jobs.py``.
2.  Implement the body in
    ``brain/intel/<source>/<module>.py`` (returns a dataclass).
3.  Add a thin wrapper here that calls the body and forwards the
    serialised result through ``brain.outbound.whatsapp.send``.
4.  Re-run ``brain scheduled export-scenario --assistant-id <id>``
    so the YAML picks up the new tick.

See :mod:`brain.intel.hackernews` for the reference implementation
and ``docs/operations/scheduled-jobs.md`` for the operator-facing
recipe.
"""

from __future__ import annotations

import os
from typing import Any

from unity.function_manager.custom import custom_function


@custom_function()
async def run_hackernews_digest_to_whatsapp(
    *,
    recipient_phone: str | None = None,
    max_stories: int = 30,
    summary_max_chars: int = 1200,
    mock: bool = False,
) -> dict[str, Any]:
    """Scrape Hacker News, summarise the top stories, send via WhatsApp.

    Args:
        recipient_phone: Operator WhatsApp number (E.164, e.g.
            ``+447...``).  When omitted, falls back to the
            ``BRAIN_WHATSAPP_DEFAULT_RECIPIENT`` env var.  If neither
            is set, the function returns an error dict without sending.
        max_stories: Cap on the number of stories observed.  Larger
            values cost more LLM tokens for ``observe()``.
        summary_max_chars: Target length for the body sent to
            WhatsApp.  The brain summariser truncates / re-summarises
            to fit.
        mock: When True, skip the live browser scrape and return a
            synthetic digest.  Used by deploy smoke tests.

    Returns:
        Dict with ``status``, ``story_count``, ``captured_at``, and the
        WhatsApp send ``message_sid`` when the send went through.
    """

    from brain.intel.hackernews import summarise_top_stories
    from brain.outbound.whatsapp import send

    to = recipient_phone or os.environ.get("BRAIN_WHATSAPP_DEFAULT_RECIPIENT")
    if not to:
        return {
            "status": "error",
            "error": (
                "no recipient_phone passed and "
                "BRAIN_WHATSAPP_DEFAULT_RECIPIENT env var is not set"
            ),
        }

    digest = await summarise_top_stories(
        max_stories=max_stories,
        summary_max_chars=summary_max_chars,
        mock=mock,
    )
    send_result = send(
        to=to,
        body=digest.summary_text,
    )
    return {
        "status": "ok",
        "story_count": len(digest.stories),
        "captured_at": digest.captured_at.isoformat(),
        "summary_chars": len(digest.summary_text),
        "message_sid": send_result.get("sid"),
        "recipient_phone": to,
    }


# ── Unity outreach daily ticks (HN + Reddit + Discord summary) ─────


@custom_function()
async def run_unity_outreach_hackernews_daily(
    *,
    max_age_days: float = 84.0,
    daily_post_limit: int = 1,
    min_adjusted_score: int = 6,
    emit_review_cards: bool = True,
    review_webhook_env: str = "UNIFY_DISCORD_REVIEW_WEBHOOK",
) -> dict[str, Any]:
    """Run the Unity-outreach HackerNews half-tick at 08:00 Europe/London.

    Discovers Unity-relevant HN threads via Algolia over the past
    ``max_age_days``, scores against the cached Unity relevance
    brief, drafts a minimal reply for the top ``daily_post_limit``
    candidates, persists JSONs under
    ``data/intel/unity_outreach/candidates/<date>/hackernews/review/``,
    and optionally posts Discord review cards.

    NO HN comments are posted by this wrapper.  Posting happens in
    the operator-approval loop (Phase 6) or via the manual
    ``scripts/unity_outreach_post_approved.py`` CLI.
    """

    from brain.intel.unity_outreach import (
        OutreachRepository,
        emit_review_cards as do_emit_cards,
        find_and_draft_hackernews,
    )
    from brain.intel.unity_pitch import build_relevance_brief

    brief = await build_relevance_brief()
    repo = OutreachRepository()
    result = await find_and_draft_hackernews(
        relevance_brief=brief.text,
        max_age_days=max_age_days,
        daily_post_limit=daily_post_limit,
        min_adjusted_score=min_adjusted_score,
        repo=repo,
    )

    discord_status = "skipped"
    if emit_review_cards and result.top_candidates:
        try:
            do_emit_cards(
                webhook_env=review_webhook_env,
                candidates=result.top_candidates,
                execute=True,
            )
            discord_status = f"emitted {len(result.top_candidates)} cards"
        except Exception as exc:  # noqa: BLE001
            # Discord misconfiguration must not block the run.  The
            # JSONs are already on disk; the operator can review by
            # hand.
            discord_status = f"failed: {type(exc).__name__}: {exc}"

    return {
        "status": "ok",
        "discovered": result.discovered,
        "after_filter": result.after_filter,
        "scored": result.scored,
        "drafted": result.drafted,
        "top_count": len(result.top_candidates),
        "review_cards": discord_status,
        "candidate_paths": [str(p) for p in result.saved_paths],
    }


@custom_function()
async def run_unity_outreach_reddit_daily(
    *,
    max_age_days: float = 84.0,
    daily_post_limit: int = 5,
    min_adjusted_score: int = 6,
    emit_review_cards: bool = True,
    review_webhook_env: str = "UNIFY_DISCORD_REVIEW_WEBHOOK",
    reddit_username: str = "daniellenton",
) -> dict[str, Any]:
    """Run the Unity-outreach Reddit half-tick at 09:00 Europe/London.

    Same shape as :func:`run_unity_outreach_hackernews_daily` but
    over the configured Reddit subs + sitewide keyword queries.
    """

    from brain.intel.unity_outreach import (
        OutreachRepository,
        emit_review_cards as do_emit_cards,
        find_and_draft_reddit,
    )
    from brain.intel.unity_pitch import build_relevance_brief

    brief = await build_relevance_brief()
    repo = OutreachRepository()
    result = await find_and_draft_reddit(
        relevance_brief=brief.text,
        max_age_days=max_age_days,
        daily_post_limit=daily_post_limit,
        min_adjusted_score=min_adjusted_score,
        username=reddit_username,
        repo=repo,
    )

    discord_status = "skipped"
    if emit_review_cards and result.top_candidates:
        try:
            do_emit_cards(
                webhook_env=review_webhook_env,
                candidates=result.top_candidates,
                execute=True,
            )
            discord_status = f"emitted {len(result.top_candidates)} cards"
        except Exception as exc:  # noqa: BLE001
            discord_status = f"failed: {type(exc).__name__}: {exc}"

    return {
        "status": "ok",
        "discovered": result.discovered,
        "after_filter": result.after_filter,
        "scored": result.scored,
        "drafted": result.drafted,
        "top_count": len(result.top_candidates),
        "review_cards": discord_status,
        "candidate_paths": [str(p) for p in result.saved_paths],
    }


@custom_function()
async def run_unity_outreach_discord_daily_summary(
    *,
    digest_webhook_env: str = "UNIFY_DISCORD_DIGEST_WEBHOOK",
) -> dict[str, Any]:
    """Run the Unity-outreach evening Discord digest at 18:00 Europe/London.

    Reads ``data/intel/unity_outreach/candidates/<today>/{hackernews,reddit}/posted/``
    and posts one digest embed-list to the channel behind
    ``UNIFY_DISCORD_DIGEST_WEBHOOK``.  Idempotent — no state is
    written beyond the OutboundAction audit row.
    """

    from brain.intel.unity_outreach import summarise_to_discord

    # Run the (synchronous) summariser in a thread so the event loop
    # isn't blocked by the httpx POST.
    import asyncio

    result = await asyncio.to_thread(
        summarise_to_discord,
        webhook_env=digest_webhook_env,
        execute=True,
    )
    return {
        "status": "ok",
        "posted_count": result.get("posted_count", 0),
        "platforms": result.get("platforms", {}),
        "discord_message_id": result.get("message_id"),
        "outbound_action_id": result.get("outbound_action_id"),
    }
