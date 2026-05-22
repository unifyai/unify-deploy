"""Valos live-read tool.

Exposes a single registered function — ``valos_request`` — that the
assistant can call to issue any GET/POST/PATCH/PUT/DELETE against the
Valos API.  Auth (placeholder bearer key) and basic transport are
handled by ``_client.valos_request``.

PLACEHOLDER: the real Valos auth scheme and rate-limit semantics are
unknown until developer documentation is obtained.  Once the API
contract is finalised, harden ``_client`` (real auth, retries) and
this tool inherits the change.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def valos_request(
    method: str,
    path: str,
    params: dict | None = None,
    body: dict | None = None,
) -> dict:
    """Issue a request against the Valos API.

    Parameters
    ----------
    method : str
        HTTP verb: ``"GET"``, ``"POST"``, ``"PATCH"``, ``"PUT"``, or
        ``"DELETE"``.
    path : str
        URL path beginning with a slash, e.g. ``"/v1/properties/search"``.
        The Valos base URL is added automatically (default
        ``https://api.valos.ai``; override via ``VALOS_API_BASE_URL``).
    params : dict, optional
        Query-string parameters.
    body : dict, optional
        Request body for non-GET verbs.  JSON-encoded automatically.

    Returns
    -------
    dict
        On success: the parsed Valos JSON response.  On failure:
        ``{"error", "status_code"}``.

    Notes
    -----
    PLACEHOLDER package — endpoint paths and response shapes are not
    yet documented publicly.  Confirm the API contract with the Valos
    developer team before relying on specific paths.
    """
    from unity_deploy.assistant_deployments.integrations.packages.valos.functions._client import (
        valos_request as _request,
    )

    return await _request(method, path, params=params, body=body)
