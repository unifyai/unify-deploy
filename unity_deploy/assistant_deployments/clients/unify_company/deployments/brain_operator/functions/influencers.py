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
