"""Matterport Mattertags — annotations on a model."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_matterport_tags(model_id: str, mock: bool = True) -> dict:
    """Mattertag inventory per model.  Informational only; not synced."""
    if mock:
        return {
            "tags": [
                {
                    "id": "tag-1",
                    "label": "Kitchen island",
                    "description": "Quartz countertop, induction cooktop",
                    "anchor": {"x": 0.5, "y": 1.1, "z": -2.3},
                },
            ]
        }

    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._client import (
        matterport_graphql,
    )

    query = """
    query Tags($id: ID!) {
      model(id: $id) {
        mattertags { id label description anchor { x y z } }
      }
    }
    """
    body = await matterport_graphql(query, variables={"id": model_id})
    if isinstance(body, dict) and body.get("error"):
        return body
    items = ((body.get("model") or {}).get("mattertags")) or []
    return {"tags": items}
