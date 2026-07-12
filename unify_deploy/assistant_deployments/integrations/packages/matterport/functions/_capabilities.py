"""Tier / capability probe for Matterport.

Sweeps representative GraphQL queries for each sync object type and
records which return data vs. 403/error.  Cached in DataManager at
``Matterport/Meta/Capabilities``.

Underscore-prefixed so FunctionManager skips this file at discovery.
The runtime-callable wrapper lives in ``sync.py`` as
``probe_matterport_tier``.
"""

from __future__ import annotations

# Probe query per capability key.  Each is a cheap GraphQL call that
# should succeed if the credentials cover the surface.

_PROBES: dict[str, str] = {
    "models": "query { models(pageSize: 1) { results { id } } }",
    "view_stats": (
        "query { models(pageSize: 1) { results { " "id stats { totalViews } } } }"
    ),
    "mosaics": ("query { models(pageSize: 1) { results { id mosaics { id } } } }"),
    "tags": ("query { models(pageSize: 1) { results { id mattertags { id } } } }"),
}


async def run_tier_probe() -> dict:
    """Sweep every probe; return ``{capability_key: {available, status_code, hint?}}``."""
    from unify_deploy.assistant_deployments.integrations.packages.matterport.functions._client import (
        matterport_graphql,
    )

    results: dict[str, dict] = {}
    for key, query in _PROBES.items():
        body = await matterport_graphql(query)
        if isinstance(body, dict) and body.get("error"):
            results[key] = {
                "available": False,
                "status_code": body.get("status_code"),
                "hint": body.get("hint"),
            }
        else:
            results[key] = {"available": True, "status_code": 200}
    return results
