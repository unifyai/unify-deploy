"""
Instagram/TikTok auto-publish wrappers for the brain_operator deployment.

Thin adapters onto :mod:`brain.social.pipeline` — the generate-then-publish
loop (ideate -> generate -> caption -> Discord review -> publish) with a
two-gate variant for video. Each function maps 1:1 to a
``BrainScheduledJob.entrypoint_function`` declared in
``brain/social/scheduled_jobs.py``.

IMPORTANT — these jobs ship **disabled** (``enabled=False`` in the brain
registry) and are not armed at deploy time. They are wrapped here so the
scenario's ``entrypoint_function`` names resolve and the operator can flip
them on later. Before enabling, the deployment needs:

* ``ffmpeg`` on the image and the marketing generators' Python deps
  (``replicate``, ``elevenlabs``, ``Pillow``, ``openai``) — add to
  ``deploy/Dockerfile`` (commented block ready) when turning this on.
* A writable scratch dir + durable media hosting (the repo tree is
  read-only on the pod; the marketing scripts write under it on a laptop).
* Secrets: ``OPENAI_API_KEY``, ``REPLICATE_API_TOKEN``,
  ``ELEVENLABS_API_KEY``, ``UNIFY_DISCORD_REVIEW_WEBHOOK``,
  ``UNIFY_DISCORD_BOT_TOKEN``, ``COMPOSIO_API_KEY`` (Instagram/TikTok posting
  runs through the Composio integration backend), and a media bucket
  (``BRAIN_SOCIAL_MEDIA_BUCKET``) so the platforms can fetch the media by URL.
"""

from __future__ import annotations

import os
from typing import Any

from unify.function_manager.custom import custom_function

# The pod's ``data/`` tree is baked into the image and sits on an ephemeral
# Cloud Run filesystem, so the file-backed candidate store would lose an
# in-flight review on a redeploy. Route the deployed operator to the durable
# DataManager backend (``Data/Social``); a laptop run keeps the file default.
os.environ.setdefault("BRAIN_SOCIAL_STORE", "datamanager")


@custom_function()
async def run_social_ideate_and_generate(
    *,
    n: int = 1,
    brief: str | None = None,
    platform: str | None = None,
    dry_run: bool = False,
    preview: bool = False,
    execute_cards: bool = True,
) -> dict[str, Any]:
    """Pitch + generate the next post(s) and post a Discord review card.

    Image assets land in ``review/`` (single gate); video assets generate
    a storyboard only and land in ``storyboard/`` (first of two gates).

    ``platform`` (``instagram`` | ``tiktok``) pins generation dimensions and
    publish routing to that platform.
    """
    from brain.social.assets import Platform
    from brain.social import specs
    from brain.social.pipeline import ideate_and_generate

    plat = Platform(platform) if platform else None
    platforms_allowed = [plat] if plat else None
    formats_allowed = specs.formats_for_platform(plat) if plat else None

    return await ideate_and_generate(
        n=n,
        brief=brief,
        platform=plat,
        platforms_allowed=platforms_allowed,
        formats_allowed=formats_allowed,
        dry_run=dry_run,
        preview=preview,
        execute_cards=execute_cards,
    )


@custom_function()
async def run_social_poll_reviews() -> dict[str, Any]:
    """Apply Discord ✅/❌ reactions across both review gates."""
    from brain.social.pipeline import poll_reviews

    return poll_reviews()


@custom_function()
async def run_social_render_storyboards(
    *,
    dry_run: bool = False,
    execute_cards: bool = True,
) -> dict[str, Any]:
    """Render videos whose storyboards were approved; post the final card."""
    from brain.social.pipeline import render_approved_storyboards

    return await render_approved_storyboards(
        dry_run=dry_run, execute_cards=execute_cards
    )


@custom_function()
async def run_social_publish_approved(*, live: bool = False) -> dict[str, Any]:
    """Publish operator-approved assets to their platforms; move to posted/.

    Defaults to ``live=False`` (dry-run) so an accidental tick can never
    post for real before the publishers are wired.
    """
    from brain.social.pipeline import publish_approved

    return await publish_approved(live=live)
