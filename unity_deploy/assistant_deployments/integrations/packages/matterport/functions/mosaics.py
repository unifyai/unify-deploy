"""Matterport mosaics — multi-model collections."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_matterport_mosaics(
    model_id: str | None = None,
    mock: bool = True,
) -> dict:
    """List mosaics; optionally filtered to those that include a model."""
    if mock:
        return {
            "mosaics": [
                {
                    "id": "mosaic-mock-1",
                    "name": "Westwood Ave Building Tour",
                    "model_count": 4,
                },
            ]
        }

    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._client import (
        matterport_graphql,
    )

    if model_id:
        query = """
        query MosaicsForModel($id: ID!) {
          model(id: $id) { mosaics { id name } }
        }
        """
        body = await matterport_graphql(query, variables={"id": model_id})
        if isinstance(body, dict) and body.get("error"):
            return body
        items = ((body.get("model") or {}).get("mosaics")) or []
    else:
        query = "query { mosaics { id name modelCount } }"
        body = await matterport_graphql(query)
        if isinstance(body, dict) and body.get("error"):
            return body
        items = body.get("mosaics") or []
    return {"mosaics": items}
