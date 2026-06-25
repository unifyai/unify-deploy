"""Cross-object HubSpot search."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def search_hubspot(
    query: str,
    object_types: list[str] | None = None,
    limit_per_type: int = 5,
    mock: bool = True,
) -> dict:
    """Search across multiple HubSpot CRM object types in one call.

    ``object_types`` defaults to ``["contacts", "companies", "deals"]``."""
    types = object_types or ["contacts", "companies", "deals"]
    if mock:
        return {
            "query": query,
            "results": {
                t: [
                    {"id": f"{t}-mock-{i}", "match": query}
                    for i in range(min(limit_per_type, 2))
                ]
                for t in types
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )

    out: dict = {"query": query, "results": {}}
    for t in types:
        body = await hubspot_search(t, query=query, limit=min(limit_per_type, 100))
        out["results"][t] = body.get("results", []) if "error" not in body else []
        if "error" in body:
            out.setdefault("errors", []).append(
                {"object_type": t, "error": body["error"]},
            )
    return out
