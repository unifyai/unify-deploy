"""
Outbound evergreen-tick wrappers for the brain_operator deployment.

Brain's evergreen layer ships one ``BrainScheduledJob`` per
``(campaign_slug, tick_name)`` pair via
:mod:`brain.outbound.evergreen.schedule_registry`.  Each job's
``entrypoint_function`` names a wrapper here following the convention
``run_evergreen_tick__<tick_name>`` (one wrapper per tick name; the
campaign slug is passed in as ``params.campaign_slug`` so the same
wrapper serves every campaign).

The wrappers delegate to :func:`brain.outbound.evergreen.runner.run_tick`,
which is the canonical dispatcher brain has already shipped.  Keeping
the wrappers thin means cadence and parameters live in the
``BrainScheduledJob`` declarations; this module only adapts the
FunctionManager isolation contract.
"""

from __future__ import annotations

from typing import Any

from droid.function_manager.custom import custom_function


def _run_evergreen(tick_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Shared body for every evergreen tick wrapper."""

    from brain.outbound.evergreen import runner as runner_mod

    campaign_slug = params.pop("campaign_slug", None) or params.pop(
        "campaignSlug",
        None,
    )
    if not campaign_slug:
        return {
            "status": "error",
            "error": (
                "evergreen tick wrapper requires params.campaign_slug "
                "to be set by the BrainScheduledJob declaration"
            ),
            "tick": tick_name,
        }
    params.pop("tick_name", None)
    try:
        return runner_mod.run_tick(tick_name, campaign_slug, **params)
    except runner_mod.UnknownTick as e:
        return {"status": "error", "error": str(e), "tick": tick_name}
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "tick": tick_name,
            "campaign_slug": campaign_slug,
        }


@custom_function()
async def run_evergreen_tick__intake(**params: Any) -> dict[str, Any]:
    return _run_evergreen("intake", dict(params))


@custom_function()
async def run_evergreen_tick__enrich(**params: Any) -> dict[str, Any]:
    return _run_evergreen("enrich", dict(params))


@custom_function()
async def run_evergreen_tick__draft(**params: Any) -> dict[str, Any]:
    return _run_evergreen("draft", dict(params))


@custom_function()
async def run_evergreen_tick__capacity(**params: Any) -> dict[str, Any]:
    return _run_evergreen("capacity", dict(params))


@custom_function()
async def run_evergreen_tick__push(**params: Any) -> dict[str, Any]:
    return _run_evergreen("push", dict(params))


@custom_function()
async def run_evergreen_tick__activity(**params: Any) -> dict[str, Any]:
    return _run_evergreen("activity", dict(params))


@custom_function()
async def run_evergreen_tick__orchestrator(**params: Any) -> dict[str, Any]:
    return _run_evergreen("orchestrator", dict(params))


@custom_function()
async def run_evergreen_tick__funnel(**params: Any) -> dict[str, Any]:
    return _run_evergreen("funnel", dict(params))


@custom_function()
async def run_smartlead_reply_processor(*, limit: int = 20) -> dict[str, Any]:
    """Drain queued SmartLead reply jobs for REST-triggered webhook runs."""

    from brain.outbound.smartlead_replies.trigger_task import drain_smartlead_reply_jobs

    return drain_smartlead_reply_jobs(limit=limit)
