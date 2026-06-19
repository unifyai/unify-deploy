"""Salto KS live-read tool.

Exposes a single registered function — ``salto_request`` — that the
assistant can call to issue any GET/POST/PATCH/PUT/DELETE against the
Salto Connect (KS) API.  Auth (OAuth client credentials with
in-process access-token caching), retry, 429 backoff, and 4xx/403
envelope shaping are handled by the underlying ``_client.salto_request``
helper, so the assistant only needs to know the API surface.

Salto KS does not have sync orchestration — there's no DataManager
mirror.  Live reads are the only path; rely on Salto's own audit log
for historical state.

Writes (lock state changes, credential issuance, user creation) are
not gated in code.  The actor must surface the proposed action to the
user and get explicit confirmation before issuing a destructive verb,
because Salto operations affect physical access control on real
buildings.
"""

from __future__ import annotations

from droid.function_manager.custom import custom_function


@custom_function()
async def salto_request(
    method: str,
    path: str,
    params: dict | None = None,
    body: dict | None = None,
) -> dict:
    """Issue a request against the Salto Connect API.

    Parameters
    ----------
    method : str
        HTTP verb: ``"GET"``, ``"POST"``, ``"PATCH"``, ``"PUT"``, or
        ``"DELETE"``.
    path : str
        URL path beginning with a slash, e.g. ``"/v1.1/installations"``,
        ``"/v1.1/users"``, ``"/v1.1/sites"``, ``"/v1.1/locks/{id}"``,
        ``"/v1.1/locks/{id}/state"``.  The Salto base URL is added
        automatically (defaults to EU production; override with
        ``SALTO_KS_BASE_URL`` for non-EU regions or sandbox).
    params : dict, optional
        Query-string parameters.  Salto list endpoints typically accept
        ``limit`` and ``cursor`` for pagination.
    body : dict, optional
        Request body for non-GET verbs.  JSON-encoded automatically.

    Returns
    -------
    dict
        On success: the parsed Salto JSON response (typically
        ``{"items": [...], "links": {...}}`` for list endpoints).  On
        failure: ``{"error", "status_code", "hint"}``.

    Notes
    -----
    * **First page only.**  Bulk pulls re-issue with the cursor from
      ``links.next`` in the response.
    * **403 means missing scope.**  Surface the hint to the user; do
      not retry blindly.
    * **Physical access control.**  Salto operations affect real
      doors and credentials.  Confirm with the user before any write.
    """
    from droid_deploy.assistant_deployments.integrations.packages.salto_ks.functions._client import (
        salto_request as _request,
    )

    return await _request(method, path, params=params, body=body)
