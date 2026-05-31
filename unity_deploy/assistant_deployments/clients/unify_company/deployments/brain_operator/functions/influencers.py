"""
Influencer-targeting browser jobs for the brain_operator deployment.

Right now this surface is a single wrapper around brain's existing
YouTube About-modal email extractor.  The runner is internally
idempotent (state in
``data/influencers/youtube/.run_state.json``); on a partial-run crash
re-firing the trigger picks up where the previous run left off.

The wrapper executes inline (``await ...``) rather than spawning a
shell so the FunctionManager call surface stays uniform.  The
underlying runner drives :class:`ComputerPrimitives` against the
agent-service that lives in the same pod (the supervisord-managed
desktop image at ``base/desktop/Dockerfile`` starts agent-service on
``localhost:3000`` by default).
"""

from __future__ import annotations

from typing import Any

from unity.function_manager.custom import custom_function


@custom_function()
async def run_youtube_browser_extraction(
    *,
    limit: int | None = None,
    include_below_1k: bool = True,
    visible: bool = False,
    reset: bool = False,
    retry_captcha_unsolved: bool = False,
) -> dict[str, Any]:
    """Drain the YouTube influencer channel list through the browser extractor.

    Returns the runner's standard summary dict (``run_id``,
    ``processed``, ``new_emails``, ``completed``, etc.) so the offline
    dispatcher persists a stable result shape.
    """

    from brain.influencers.youtube.login import DEFAULT_STORAGE_STATE_NAME
    from brain.influencers.youtube.runner import run_extraction_sync

    summary = run_extraction_sync(
        limit=limit,
        include_below_1k=include_below_1k,
        visible=visible,
        reset=reset,
        rebuild_csv=True,
        storage_state_name=DEFAULT_STORAGE_STATE_NAME,
        retry_captcha_unsolved=retry_captcha_unsolved,
    ).to_dict()
    return {"status": "ok", **summary}


# ── X automation ticks (reply bot + personalised DM outreach) ──────────


@custom_function()
async def run_x_reply_bot_session(
    *,
    x_user: str = "DanielLenton1",
    max_per_session: int = 1,
    daily_cap: int = 50,
) -> dict[str, Any]:
    """One automated X reply session (fired every 30 min by the schedule).

    Discovers fresh on-topic posts, ranks for early-reply value, and posts
    up to ``max_per_session`` short personalised replies (CodeActActor-
    drafted, grounded in the unity code) about how unity handles the
    poster's problem. Fully automated; idempotent + daily-capped. The body
    is sync (it drives its own actor event loop), so we run it off-thread.
    """

    import asyncio

    from brain.influencers.x.reply_bot import run_session

    summary = await asyncio.to_thread(
        run_session,
        x_user,
        max_per_session=max_per_session,
        daily_cap=daily_cap,
    )
    return {
        "status": "ok",
        "posted": len(summary.get("posted", [])),
        "eligible": summary.get("eligible", 0),
        "declined": summary.get("declined", 0),
        "note": summary.get("note", ""),
    }


@custom_function()
async def run_x_dm_campaign_session(
    *,
    x_user: str = "DanielLenton1",
    max_per_session: int = 1,
    daily_cap: int = 40,
    store_backend: str = "datamanager",
) -> dict[str, Any]:
    """One personalised DM-outreach session (fired every 30 min).

    Sends up to ``max_per_session`` personalised cold-outreach DMs to the
    next accounts on the real-influencer shortlist. A CodeActActor
    researches each target and writes a genuine DM (or skips). Warm-up
    ramp + daily cap throttle volume; the target list + send-log/dedup live
    in the Unity DataManager (``store_backend="datamanager"``), so dedup
    survives container restarts and the list is live-toppable without a
    redeploy. Built to run for months unattended. Sync body -> off-thread.
    """

    import asyncio

    from brain.influencers.x.dm_campaign import run_session

    summary = await asyncio.to_thread(
        run_session,
        x_user,
        max_per_session=max_per_session,
        daily_cap=daily_cap,
        store_backend=store_backend,
    )
    sent = [r for r in summary.get("sent", []) if r.get("status") == "sent"]
    return {
        "status": "ok",
        "sent": len(sent),
        "attempts": summary.get("attempts", 0),
        "declined": summary.get("declined", 0),
        "skipped_closed": summary.get("skipped_closed", 0),
        "note": summary.get("note", ""),
    }
