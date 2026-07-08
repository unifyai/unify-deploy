"""
Instagram/TikTok auto-publish wrappers for the brain_operator deployment.

Thin adapters onto :mod:`brain.social.create.pipeline` — the generate-then-publish
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
async def run_social_linkedin_discover_draft(
    *,
    include_personal_feeds: bool = True,
    include_hackernews: bool = False,
    include_x: bool = False,
    include_arxiv: bool = False,
    max_age_days: float = 14.0,
    daily_post_limit: int = 3,
    emit_review_cards: bool = True,
    review_webhook_env: str = "UNIFY_DISCORD_REVIEW_WEBHOOK",
) -> dict[str, Any]:
    """Discover LinkedIn home-feed posts (browser scrape) -> draft -> Discord.

    Browser-scrape discovery for LinkedIn: restores a durable LinkedIn session
    from DataManager (``BRAIN_SOCIAL_STORE=datamanager``), optionally auto-logs
    in when logged out (``BRAIN_LINKEDIN_AUTOLOGIN=1``, using the seeded
    credentials + TOTP + email-PIN dispatcher), scrapes the operator's home
    feed, drafts commentary, and posts Discord review cards. Persists the
    rotated session back after a successful run. Publishes nothing.

    Ships **disabled** — arm only after a LinkedIn session has been seeded
    (``brain social curate login --site linkedin`` on the live pod, or
    ``--seed-file``) and the agent-service is reachable in the job pod.
    """
    from brain.intel.droid_pitch import build_relevance_brief
    from brain.social.curate import (
        SocialPostRepository,
        discover_and_draft,
        emit_review_cards as do_emit_cards,
    )
    from brain.social.platforms import Platform

    brief = await build_relevance_brief()
    repo = SocialPostRepository(platforms=(Platform.LINKEDIN.value,))
    result = await discover_and_draft(
        relevance_brief=brief.text,
        destination=Platform.LINKEDIN,
        include_hackernews=include_hackernews,
        include_x=include_x,
        include_arxiv=include_arxiv,
        include_personal_feeds=include_personal_feeds,
        max_age_days=max_age_days,
        daily_post_limit=daily_post_limit,
        repo=repo,
    )

    discord_status = "skipped"
    if emit_review_cards and result.top_candidates:
        try:
            do_emit_cards(
                candidates=result.top_candidates,
                repo=repo,
                webhook_env=review_webhook_env,
                execute=True,
            )
            discord_status = f"emitted {len(result.top_candidates)} cards"
        except Exception as exc:  # noqa: BLE001
            discord_status = f"failed: {type(exc).__name__}: {exc}"

    return {
        "status": "ok",
        "discovered": result.discovered,
        "after_filter": result.after_filter,
        "drafted": result.drafted,
        "top_count": len(result.top_candidates),
        "review_cards": discord_status,
        "source_counts": result.source_counts,
        "source_errors": result.source_errors,
    }


@custom_function()
async def run_social_linkedin_login(
    *,
    sentinel_path: str | None = None,
    hold_timeout: float = 900.0,
) -> dict[str, Any]:
    """Open a VISIBLE LinkedIn login in-pod for operator co-pilot over VNC.

    Reactive trigger meant for the **live assistant pod** (which runs the
    desktop stack + agent-service): opens a visible Chromium at the LinkedIn
    login, then blocks until the operator finishes the login by hand (2FA /
    captcha included) and ``touch``es the sentinel file over the pod's VNC.
    On completion it saves the authenticated browser state and pushes it to the
    assistant's durable DataManager store so scheduled scrapes can restore it.

    Not for offline job pods (no visible display). Reach the desktop via
    ``kubectl port-forward <pod> 5900:5900`` + a VNC viewer.
    """
    from brain.social.discovery.sources import capture_login_state
    from brain.social.discovery.sources.linkedin_auth import DEFAULT_LOGIN_SENTINEL

    sentinel = sentinel_path or DEFAULT_LOGIN_SENTINEL
    name = await capture_login_state(
        site="linkedin",
        wait_for_enter=False,
        hold_sentinel=sentinel,
        hold_timeout=hold_timeout,
        persist_to_store=True,
    )
    return {"status": "ok", "storage_state": name, "sentinel": sentinel}


@custom_function()
async def run_social_linkedin_post_approved(
    *,
    daily_post_limit: int = 3,
    summarise_to_discord: bool = True,
    digest_webhook_env: str = "UNIFY_DISCORD_DIGEST_WEBHOOK",
) -> dict[str, Any]:
    """Publish operator-approved LinkedIn drafts as comments, then digest.

    Drains the LinkedIn ``approved/`` queue and posts each as a **comment** on
    the source post via the Composio LinkedIn adapter (which resolves the
    captured ``urn:li:activity:<id>`` to a commentable share/ugcPost URN).
    Mirrors the X post-approved tick: an empty ``approved/`` is a no-op.
    """
    from brain.social.curate import (
        SocialPostRepository,
        post_approved,
        summarise_to_discord as do_summary,
    )
    from brain.social.platforms import CurateMode, Platform

    repo = SocialPostRepository(platforms=(Platform.LINKEDIN.value,))
    result = await post_approved(
        repo=repo,
        destination=Platform.LINKEDIN,
        curate_mode=CurateMode.COMMENT,
        daily_post_limit=daily_post_limit,
        dry_run=False,
    )

    digest_status = "skipped"
    if summarise_to_discord:
        import asyncio

        try:
            await asyncio.to_thread(
                do_summary,
                webhook_env=digest_webhook_env,
                platform="linkedin",
                execute=True,
            )
            digest_status = "posted"
        except Exception as exc:  # noqa: BLE001
            digest_status = f"failed: {type(exc).__name__}: {exc}"

    return {
        "status": "ok",
        "posted": len(result.posted),
        "failed": len(result.failed),
        "comment_urls": [o.post_url for o in result.posted if o.post_url],
        "errors": [o.error for o in result.failed if o.error],
        "discord_digest": digest_status,
    }


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
    from brain.social.create.assets import Platform
    from brain.social.create import specs
    from brain.social.create.pipeline import ideate_and_generate

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
    from brain.social.create.pipeline import poll_reviews

    return poll_reviews()


@custom_function()
async def run_social_render_storyboards(
    *,
    dry_run: bool = False,
    execute_cards: bool = True,
) -> dict[str, Any]:
    """Render videos whose storyboards were approved; post the final card."""
    from brain.social.create.pipeline import render_approved_storyboards

    return await render_approved_storyboards(
        dry_run=dry_run, execute_cards=execute_cards
    )


@custom_function()
async def run_social_publish_approved(*, live: bool = False) -> dict[str, Any]:
    """Publish operator-approved assets to their platforms; move to posted/.

    Defaults to ``live=False`` (dry-run) so an accidental tick can never
    post for real before the publishers are wired.
    """
    from brain.social.create.pipeline import publish_approved

    return await publish_approved(live=live)
