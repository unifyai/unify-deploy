"""Matterport models sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by the
top-level ``sync.run_matterport_sync_tick`` orchestrator via importlib;
not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_matterport_models(
    since: str | None = None,
    schema_version: str = "matterport.models.v1",
    mock: bool = False,
) -> dict:
    """Pull models updated since the watermark; emit DataManager rows.

    Returns the canonical ``{schema_version, tables, metadata}`` envelope.
    """
    import datetime as _dt

    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "models": [
                    {
                        "model_id": "mdl-mock-1",
                        "name": "401 Westwood Ave - Unit 4B",
                        "internal_label": "unit:4B",
                        "visibility": "unlisted",
                        "share_url": "https://my.matterport.com/show/?m=mdl-mock-1",
                        "status": "processed",
                        "address_line1": "401 Westwood Ave",
                        "city": "Denver",
                        "state": "CO",
                        "postal_code": "80205",
                        "country": "US",
                        "sqft": 850,
                        "last_modified": "2026-04-20T10:00:00Z",
                    },
                ],
            },
            "metadata": {
                "since": since,
                "fetched_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
                "mode": "mock",
            },
        }

    from droid_deploy.assistant_deployments.integrations.packages.matterport.functions._client import (
        matterport_graphql,
    )
    from droid_deploy.assistant_deployments.integrations.packages.matterport.functions._config import (
        get_matterport_config,
    )
    from droid_deploy.assistant_deployments.integrations.packages.matterport.functions._sync_helpers import (
        MODEL_FIELDS_FRAGMENT,
        normalize_model,
    )

    cfg = get_matterport_config()
    page_size = cfg["api_page_size"]
    max_pages = cfg["max_pages_per_sync"]
    cursor: str | None = None
    rows: list[dict] = []
    pages = 0
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    query = f"""
    query SyncModels($pageSize: Int, $cursor: String, $since: String) {{
      models(pageSize: $pageSize, cursor: $cursor, modifiedSince: $since) {{
        results {{ {MODEL_FIELDS_FRAGMENT} }}
        cursor
      }}
    }}
    """

    while True:
        body = await matterport_graphql(
            query,
            variables={"pageSize": page_size, "cursor": cursor, "since": since},
        )
        if isinstance(body, dict) and body.get("error"):
            if rows:
                break
            return body
        page = body.get("models") or {}
        for raw in page.get("results") or []:
            rows.append(normalize_model(raw))
        cursor = page.get("cursor")
        pages += 1
        if not cursor:
            break
        if max_pages is not None and pages >= max_pages:
            break

    return {
        "schema_version": schema_version,
        "tables": {"models": rows},
        "metadata": {
            "since": since,
            "fetched_at": started,
            "row_count": len(rows),
            "pages_fetched": pages,
            "mode": "live",
        },
    }
