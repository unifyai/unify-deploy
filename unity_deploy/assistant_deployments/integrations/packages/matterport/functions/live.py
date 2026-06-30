"""Matterport live-read tool.

Exposes a single registered function — ``matterport_graphql_query`` —
that the assistant can call to issue any GraphQL query/mutation against
the Matterport Model API.  Auth (HTTP Basic with Token ID + Secret),
retry, 429 backoff, and 4xx/403 envelope shaping are handled by the
underlying ``_client.matterport_graphql`` helper, so the assistant only
needs to know the schema.

Bulk pulls and snapshots go through ``run_matterport_sync_tick`` —
this tool is for ad-hoc queries and one-off mutations.  GraphQL errors
come back inside a ``graphql_errors`` field on a 200 response;
HTTP-level failures come back as the standard ``{"error", "status_code",
"hint"}`` envelope.
"""

from __future__ import annotations

from unify.function_manager.custom import custom_function


@custom_function()
async def matterport_graphql_query(
    query: str,
    variables: dict | None = None,
) -> dict:
    """Execute a GraphQL query/mutation against the Matterport Model API.

    Parameters
    ----------
    query : str
        GraphQL query or mutation document.  Inspect the live schema
        before assuming field names — Matterport's schema evolves and
        not every documented field is available on every plan tier.
    variables : dict, optional
        Variables for the query.

    Returns
    -------
    dict
        On success: the parsed ``data`` block from the GraphQL response.
        On GraphQL-level errors: ``{"error", "status_code": 200,
        "graphql_errors": [...], "data": ...}``.  On HTTP-level errors
        (auth, 403, network): the standard error envelope with ``hint``.

    Notes
    -----
    * The Model API endpoint is ``/api/models/graph``; you do not need
      to construct the URL — pass only the query body.
    * 403 means the active Matterport plan does not include the
      requested surface (Developer Tools add-on missing or sandbox
      token); surface the hint to the user, do not retry blindly.
    * For bulk model or view-stats pulls, prefer ``run_matterport_sync_tick``
      which handles cursors and writes the canonical envelope into
      DataManager.
    """
    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._client import (
        matterport_graphql,
    )

    return await matterport_graphql(query, variables=variables)
