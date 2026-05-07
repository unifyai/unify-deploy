"""Matterport 3D models — list / get / search / sync."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_MODEL_FIELDS = """
  id
  name
  internalLabel
  visibility
  shareUrl
  status
  modifiedAt
  sqft
  address {
    addressLine1
    addressLine2
    city
    state
    postalCode
    country
  }
"""


@custom_function()
async def list_matterport_models(
    after: str | None = None,
    limit: int = 25,
    mock: bool = True,
) -> dict:
    """Page through the active org's 3D models.

    Returns ``{results, next_after}``.  ``next_after`` is the cursor for
    the next page; ``None`` when the list is exhausted.
    """
    if mock:
        return {
            "results": [
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
            "next_after": None,
        }

    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._client import (
        matterport_graphql,
    )
    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._sync_helpers import (
        normalize_model,
    )

    query = f"""
    query Models($pageSize: Int, $cursor: String) {{
      models(pageSize: $pageSize, cursor: $cursor) {{
        results {{ {_MODEL_FIELDS} }}
        cursor
      }}
    }}
    """
    body = await matterport_graphql(
        query,
        variables={"pageSize": limit, "cursor": after},
    )
    if isinstance(body, dict) and body.get("error"):
        return body
    page = body.get("models") or {}
    return {
        "results": [normalize_model(r) for r in (page.get("results") or [])],
        "next_after": page.get("cursor"),
    }


@custom_function()
async def get_matterport_model(model_id: str, mock: bool = True) -> dict:
    """Fetch a single model by id including unit-linking metadata
    (address, internal label, custom label fields)."""
    if mock:
        return {
            "model_id": str(model_id),
            "name": "401 Westwood Ave - Unit 4B",
            "internal_label": "unit:4B",
            "visibility": "unlisted",
            "share_url": f"https://my.matterport.com/show/?m={model_id}",
            "status": "processed",
            "address_line1": "401 Westwood Ave",
            "city": "Denver",
            "state": "CO",
            "postal_code": "80205",
            "country": "US",
            "sqft": 850,
            "last_modified": "2026-04-20T10:00:00Z",
        }

    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._client import (
        matterport_graphql,
    )
    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._sync_helpers import (
        normalize_model,
    )

    query = f"""
    query Model($id: ID!) {{
      model(id: $id) {{ {_MODEL_FIELDS} }}
    }}
    """
    body = await matterport_graphql(query, variables={"id": model_id})
    if isinstance(body, dict) and body.get("error"):
        return body
    raw = body.get("model")
    if not raw:
        return {"error": f"Matterport model {model_id} not found", "status_code": 404}
    return normalize_model(raw)


@custom_function()
async def search_matterport_models_by_address(
    address_query: str,
    limit: int = 10,
    mock: bool = True,
) -> dict:
    """Best-effort fuzzy search for models matching an address string.

    Used to bootstrap unit-mapping from a RealPage unit's address.  The
    Matterport schema doesn't always expose a server-side address filter,
    so this paginates and filters client-side when needed.
    """
    if mock:
        return {
            "results": [
                {
                    "model_id": "mdl-mock-1",
                    "name": "401 Westwood Ave - Unit 4B",
                    "address_line1": "401 Westwood Ave",
                    "city": "Denver",
                    "match_score": 0.92,
                },
            ],
            "query": address_query,
        }

    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._client import (
        matterport_graphql,
    )
    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._sync_helpers import (
        normalize_model,
    )

    query = f"""
    query SearchModels($query: String, $pageSize: Int) {{
      models(filter: {{ address: $query }}, pageSize: $pageSize) {{
        results {{ {_MODEL_FIELDS} }}
      }}
    }}
    """
    body = await matterport_graphql(
        query,
        variables={"query": address_query, "pageSize": limit},
    )
    if isinstance(body, dict) and body.get("error"):
        return body
    raw_results = (body.get("models") or {}).get("results") or []
    normalized = [normalize_model(r) for r in raw_results]
    needle = address_query.strip().lower()
    scored = []
    for r in normalized:
        haystack = " ".join(
            filter(None, [r.get("address_line1"), r.get("city"), r.get("postal_code")])
        ).lower()
        score = (
            1.0
            if needle in haystack
            else 0.5 if any(tok in haystack for tok in needle.split()) else 0.0
        )
        if score > 0:
            scored.append({**r, "match_score": score})
    scored.sort(key=lambda r: r["match_score"], reverse=True)
    return {"results": scored[:limit], "query": address_query}


@custom_function()
async def sync_matterport_models(
    since: str | None = None,
    schema_version: str = "matterport.models.v1",
    mock: bool = True,
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

    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._client import (
        matterport_graphql,
    )
    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._config import (
        get_matterport_config,
    )
    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._sync_helpers import (
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
        results {{ {_MODEL_FIELDS} }}
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
