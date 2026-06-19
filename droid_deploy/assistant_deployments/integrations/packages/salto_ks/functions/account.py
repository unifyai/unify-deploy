"""Salto KS account / installation introspection — connectivity probe.

Use ``get_salto_account_info`` as the canonical "is the integration
working?" smoke test before relying on any other live function.
"""

from __future__ import annotations

from droid.function_manager.custom import custom_function


@custom_function()
async def get_salto_account_info(mock: bool = True) -> dict:
    """Return metadata about the Salto installation the OAuth client is bound to.

    Hits ``/v1.1/installations`` (current + active installation) — useful
    as a connectivity probe.  Returns ``{"error": ..., "status_code": ...}``
    on auth / scope failure; raise no exception so the actor can decide
    next steps.
    """
    if mock:
        return {
            "id": "inst_mock_001",
            "name": "Mock Property Group",
            "region": "eu",
            "product_line": "ks",
            "timezone": "Europe/London",
            "created_at": "2025-09-01T10:00:00Z",
        }

    from droid_deploy.assistant_deployments.integrations.packages.salto_ks.functions._client import (
        salto_get,
    )

    body = await salto_get("/v1.1/installations")
    if "error" in body:
        return body

    # Salto's installations endpoint may return a single object or a
    # ``{installations: [...]}`` wrapper depending on whether the client
    # is bound to one or many installations.  Normalise to "the active
    # one" — the first if multiple are returned.
    if isinstance(body, dict) and "installations" in body:
        items = body.get("installations") or []
        return items[0] if items else {}
    return body


@custom_function()
async def list_salto_installations(mock: bool = True) -> dict:
    """List every installation the OAuth client can address.

    Most clients are bound to exactly one installation (the BU issues
    credentials per-installation by default).  When the BU has issued
    credentials that span multiple installations, this function returns
    them all so the operator can pin one via ``SALTO_KS_CUSTOMER_ID``.
    """
    if mock:
        rows = [
            {
                "id": "inst_mock_001",
                "name": "Mock Property Group",
                "region": "eu",
                "product_line": "ks",
                "timezone": "Europe/London",
                "created_at": "2025-09-01T10:00:00Z",
            },
        ]
        return {"installations": rows, "count": len(rows)}

    from droid_deploy.assistant_deployments.integrations.packages.salto_ks.functions._client import (
        salto_get,
    )

    body = await salto_get("/v1.1/installations")
    if "error" in body:
        return body

    if isinstance(body, dict) and "installations" in body:
        items = body.get("installations") or []
    elif isinstance(body, dict):
        # Single installation came back as a flat object.
        items = [body]
    else:
        items = []
    return {"installations": items, "count": len(items)}
