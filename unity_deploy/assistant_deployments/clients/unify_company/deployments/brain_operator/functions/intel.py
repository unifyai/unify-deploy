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
