"""Matterport Showcase embed-URL builder.

Pure URL construction — no API call, no auth needed.  The default
share URL works for any model whose ``visibility`` is public or
unlisted.  Private models require a signed-token endpoint that
Matterport gates behind partner-tier; out of scope for v1.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_SHOWCASE_BASE = "https://my.matterport.com/show/"

# Showcase URL options users care about most.  See
# https://support.matterport.com/s/article/Showcase-URL-Parameters
_DEFAULT_OPTIONS: dict[str, int | str] = {
    "qs": 1,  # quickstart - skip the loading splash
    "play": 1,  # auto-rotate on load
    "brand": 0,  # hide Matterport branding
    "mt": 0,  # hide Mattertags
    "dh": 1,  # display hover hotspots
}


@custom_function()
async def generate_matterport_embed_url(
    model_id: str,
    options: dict | None = None,
    mock: bool = True,
) -> dict:
    """Build a Showcase URL for a Matterport model.

    ``options`` overrides any of the Showcase URL parameters.  Common
    keys: ``qs``, ``play``, ``brand``, ``mt`` (Mattertags toggle),
    ``dh`` (hover hotspots), ``hr`` (high-res), ``help``.  See
    Matterport's Showcase URL Parameters documentation for the full list.
    """
    from urllib.parse import urlencode

    merged: dict[str, int | str] = dict(_DEFAULT_OPTIONS)
    if options:
        merged.update(options)
    merged["m"] = str(model_id)

    url = _SHOWCASE_BASE + "?" + urlencode(merged)

    if mock:
        return {
            "url": url,
            "model_id": str(model_id),
            "options_applied": merged,
            "_note": "mock mode returns the same URL the live mode would.",
        }

    return {
        "url": url,
        "model_id": str(model_id),
        "options_applied": merged,
    }
