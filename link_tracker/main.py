"""Public redirect for ``r.unify.ai`` shortlinks.

Brain rewrites every outbound URL to ``https://r.unify.ai/<short_id>`` and
stores the mapping, so this service is the other half of that contract: resolve
the id, record the click, send the visitor on. Without it every tracked link in
every email, LinkedIn note and deck is dead — which is exactly what happened
until this existed.

Two things it must never do:

* **Fail the redirect because attribution failed.** A click row is worth less
  than a prospect reaching the page, so a logging error is swallowed and the
  visitor is still redirected.
* **Log the destination.** Canonical URLs carry one-time credit-grant tokens
  and similar secrets in their query strings, so only the short id is logged.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import unisdk
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)
logger = logging.getLogger(__name__)

ORCHESTRA_PROJECT = os.environ.get("LINK_TRACKER_PROJECT", "Assistants")
SHORTLINKS_CONTEXT = os.environ.get(
    "LINK_TRACKER_SHORTLINKS_CONTEXT",
    "Teams/11/Data/CRM/ShortlinkLookups",
)
CLICKS_CONTEXT = os.environ.get(
    "LINK_TRACKER_CLICKS_CONTEXT",
    "Teams/11/Data/CRM/LinkClicks",
)

# The id is interpolated into an Orchestra filter expression, so it is matched
# against the generated alphabet before it goes anywhere near a query.
_SHORT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

app = FastAPI(title="Unify link tracker", docs_url=None, redoc_url=None)


def _api_key() -> str | None:
    return os.environ.get("UNIFY_KEY") or None


def build_redirect_url(
    *,
    canonical_url: str,
    utm_source: str | None,
    utm_medium: str | None,
    utm_campaign: str | None,
    utm_content: str | None,
) -> str:
    """Append the four UTMs to ``canonical_url`` without disturbing other params.

    Mirrors ``brain.outbound.evergreen.link_tracking.build_redirect_url``. The
    two must agree: brain stores the UTMs at shorten time and this applies them
    at redirect time, so a difference here silently changes how every campaign
    attributes its traffic. ``tests/link_tracker`` pins the behaviour.
    """

    parts = urlparse(canonical_url)
    existing = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in (
        ("utm_source", utm_source),
        ("utm_medium", utm_medium),
        ("utm_campaign", utm_campaign),
        ("utm_content", utm_content),
    ):
        if value and key not in existing:
            existing[key] = value
    return urlunparse(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            parts.params,
            urlencode(existing, doseq=True),
            parts.fragment,
        ),
    )


def _hash_ip(ip: str | None) -> str | None:
    """Hash a visitor IP with the deployment salt, or drop it when unsalted.

    Without the salt a raw address would be the only identifying value on the
    row, so the field is left empty rather than stored in the clear.
    """

    salt = os.environ.get("LINK_TRACKER_IP_HASH_SALT")
    if not ip or not salt:
        return None
    return hashlib.sha256(f"{salt}:{ip}".encode("utf-8")).hexdigest()


def _client_ip(request: Request) -> str | None:
    """First hop in ``X-Forwarded-For``, which on Cloud Run is the caller."""

    forwarded = request.headers.get("x-forwarded-for") or ""
    first = forwarded.split(",")[0].strip()
    if first:
        return first
    return request.client.host if request.client else None


def lookup_shortlink(short_id: str) -> dict[str, Any] | None:
    """Resolve a short id to its stored shortlink row."""

    logs = unisdk.get_logs(
        project=ORCHESTRA_PROJECT,
        context=SHORTLINKS_CONTEXT,
        filter=f'short_id == "{short_id}"',
        limit=1,
        api_key=_api_key(),
    )
    if not logs:
        return None
    return dict(logs[0].entries)


def record_click(shortlink: dict[str, Any], request: Request) -> None:
    """Write one ``LinkClicks`` row. Never raises."""

    entries = {
        "short_id": shortlink.get("short_id"),
        "canonical_url": shortlink.get("canonical_url"),
        "contact_id": shortlink.get("contact_id"),
        "campaign_id": shortlink.get("campaign_id"),
        "channel": shortlink.get("channel") or "other",
        "step": shortlink.get("step"),
        "user_agent": request.headers.get("user-agent"),
        "ip_hash": _hash_ip(_client_ip(request)),
        "referrer": request.headers.get("referer"),
    }
    unisdk.create_logs(
        project=ORCHESTRA_PROJECT,
        context=CLICKS_CONTEXT,
        entries=entries,
        api_key=_api_key(),
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/{short_id}")
def redirect(short_id: str, request: Request) -> Any:
    if not _SHORT_ID_RE.match(short_id):
        return JSONResponse({"detail": "Unknown link."}, status_code=404)

    shortlink = lookup_shortlink(short_id)
    if shortlink is None:
        logger.info("shortlink miss: %s", short_id)
        return JSONResponse({"detail": "Unknown link."}, status_code=404)

    try:
        record_click(shortlink, request)
    except Exception as exc:  # noqa: BLE001 — attribution must not break the visit
        logger.warning("click not recorded for %s: %r", short_id, exc)

    destination = build_redirect_url(
        canonical_url=str(shortlink.get("canonical_url") or ""),
        utm_source=shortlink.get("utm_source"),
        utm_medium=shortlink.get("utm_medium"),
        utm_campaign=shortlink.get("utm_campaign"),
        utm_content=shortlink.get("utm_content"),
    )
    logger.info("shortlink hit: %s", short_id)
    # 302 with no-store: a cached permanent redirect would make every later
    # click invisible.
    return RedirectResponse(
        url=destination,
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )
