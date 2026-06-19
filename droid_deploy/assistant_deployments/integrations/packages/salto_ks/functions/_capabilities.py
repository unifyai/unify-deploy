"""Tier / capability probe for Salto KS.

Sweeps representative endpoints for each Salto Connect API resource
and records which return 200 vs. 403 / not-supported.  Useful as a
diagnostic when a customer reports "X isn't working" — most failures
under ROPC come from the service-account user's KS role being too
narrow rather than from OAuth scope gaps (Salto's Backend Server
flow uses a single coarse scope, ``user_api.full_access``).

Underscore-prefixed so FunctionManager skips this file at discovery.
There is no registered wrapper today — Salto KS is live-only with no
DataManager mirror, so the probe isn't part of a sync flow.  Callers
that want a coarse "is the connection healthy?" check should prefer
``get_salto_account_info`` over running this full sweep.
"""

from __future__ import annotations

# Probe endpoint per resource.  Each is a cheap GET that should
# succeed if the service-account user (SALTO_KS_USERNAME) has the KS
# role required for the resource.  Paths are placeholders following
# the Salto Connect API convention — verify exact shapes during BU
# onboarding.
_PROBE_ENDPOINTS: dict[str, str] = {
    "account": "/v1.1/installations",
    "users": "/v1.1/users?limit=1",
    "sites": "/v1.1/sites?limit=1",
    "locks": "/v1.1/locks?limit=1",
    "access_events": "/v1.1/access-events?limit=1",
    "access_rights": "/v1.1/access-rights?limit=1",
    "access_groups": "/v1.1/access-groups?limit=1",
    "credentials": "/v1.1/credentials?limit=1",
    "time_schedules": "/v1.1/time-schedules?limit=1",
}


async def run_tier_probe() -> dict:
    """Sweep every probe endpoint; return ``{resource: {available, status_code, hint?}}``.

    Under ROPC, 403s usually indicate the service-account user's KS
    role doesn't permit the resource — the hint surfaces that
    diagnostic via the standard ``_403_envelope``.  Caller decides
    what to do with the results (logging, reporting back to the
    actor, etc.).
    """
    from droid_deploy.assistant_deployments.integrations.packages.salto_ks.functions._client import (
        salto_get,
    )

    results: dict[str, dict] = {}
    for key, path in _PROBE_ENDPOINTS.items():
        body = await salto_get(path)
        if "error" in body:
            results[key] = {
                "available": False,
                "status_code": body.get("status_code"),
                "hint": body.get("hint"),
            }
        else:
            results[key] = {"available": True, "status_code": 200}
    return results
