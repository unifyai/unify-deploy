"""Webex live-read tool.

Exposes a single registered function — ``webex_request`` — that the
assistant can call to issue any GET/POST/PUT/DELETE against the Webex
public REST API.  Auth, refresh, retry, and 4xx envelope shaping are
handled by the underlying ``_client.webex_request`` helper, so the
assistant only needs to know the API surface.

Bulk pulls and snapshots go through ``sync_webex_*`` orchestration —
this tool returns one page only and does not auto-paginate.  When the
caller needs more than the first page, they pass cursor params on a
follow-up call (see Webex API docs), or kick a sync.
"""

from __future__ import annotations

from unify.function_manager.custom import custom_function


@custom_function()
async def webex_request(
    method: str,
    path: str,
    params: dict | None = None,
    body: dict | None = None,
) -> dict:
    """Issue a request against the Webex public REST API.

    Parameters
    ----------
    method : str
        HTTP verb: ``"GET"``, ``"POST"``, ``"PUT"``, or ``"DELETE"``.
    path : str
        URL path beginning with a slash, e.g. ``"/v1/meetings"`` or
        ``"/v1/people/me"``.  The Webex base URL is added automatically.
    params : dict, optional
        Query-string parameters.  Webex generally requires camelCase
        keys (``hostEmail``, ``meetingId``, ``orgId``, ``displayName``)
        and ISO 8601 timestamps with the ``Z`` UTC suffix (not
        ``+00:00``).
    body : dict, optional
        Request body for non-GET verbs.  JSON-encoded automatically.

    Returns
    -------
    dict
        On success: the parsed Webex JSON response (typically
        ``{"items": [...]}`` for list endpoints).  On failure: a
        structured error envelope ``{"error": ..., "status_code": ...,
        "hint": ...}`` — read ``hint`` for guidance (e.g. tier-gated
        403, refresh required, missing required filter).

    Notes
    -----
    * **Filters required for some endpoints.**  ``/v1/people`` requires
      one of ``email``, ``displayName``, ``id``, or ``orgId``.
    * **First page only.**  Bulk pulls go through sync; this helper
      does not auto-paginate.
    * **Capability gating.**  403 means the connected user's token
      lacks the scope or role for that endpoint — surface the hint to
      the user; do not retry blindly.
    """
    from unify_deploy.assistant_deployments.integrations.packages.webex.functions._client import (
        webex_request as _request,
    )

    return await _request(method, path, params=params, body=body)
