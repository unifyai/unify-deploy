"""
GTM pipeline wrappers for the brain_operator deployment.

Thin adapters onto :mod:`brain.gtm.ticks` — stargazer poll/enrich,
SmartLead reconcile, and outbound replenish. Each function maps 1:1 to a
``BrainScheduledJob.entrypoint_function`` declared in
``brain/gtm/scheduled_jobs.py``.
"""

from __future__ import annotations

import os
from typing import Any

from unify.function_manager.custom import custom_function

# Durable GTM inventory lives in DataManager (``Data/GTM`` on the operator
# assistant). The pod's baked ``data/`` tree is ephemeral; file fallback is
# for laptop tests only.
os.environ.setdefault("BRAIN_GTM_STORE", "datamanager")


@custom_function()
async def run_gtm_stargazer_poll_tick(**params: Any) -> dict[str, Any]:
    from brain.gtm.ticks import run_gtm_stargazer_poll_tick

    return run_gtm_stargazer_poll_tick(**params)


@custom_function()
async def run_gtm_stargazer_enrich_tick(**params: Any) -> dict[str, Any]:
    from brain.gtm.ticks import run_gtm_stargazer_enrich_tick

    return run_gtm_stargazer_enrich_tick(**params)


@custom_function()
async def run_gtm_smartlead_reconcile_tick(**params: Any) -> dict[str, Any]:
    from brain.gtm.ticks import run_gtm_smartlead_reconcile_tick

    return run_gtm_smartlead_reconcile_tick(**params)


@custom_function()
async def run_gtm_outbound_replenish_inventory(**params: Any) -> dict[str, Any]:
    from brain.gtm.ticks import run_gtm_outbound_replenish_inventory

    return run_gtm_outbound_replenish_inventory(**params)
