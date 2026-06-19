"""Internal CQC Syndication client + care-home matching for the Valos package.

Underscore-prefixed so :func:`droid.function_manager.custom_functions.collect_custom_functions`
skips this file.  Sibling function modules import from here inside their
function bodies to satisfy FunctionManager's isolation rule.

The Care Quality Commission (CQC) Syndication API is the register of
record for every regulated care home in England.  Access requires a
free subscription key (register at api-portal.service.cqc.org.uk),
passed as the ``Ocp-Apim-Subscription-Key`` header — the API sits
behind Azure API Management, so unauthenticated requests get a 401.
The key is read from ``CQC_PRIMARY_KEY`` in the environment.  Data is
published under the Open Government Licence v3.0; deliverables that
surface it must attribute "Contains public sector information licensed
under the Open Government Licence v3.0".

CQC's ``localAuthority`` filter keys off the *upper-tier* authority
(county for two-tier areas, e.g. "Cambridgeshire" — not the district
"South Cambridgeshire", nor "Cambridge"; the unitary name for unitary
areas).  We therefore resolve the catchment's authorities from
postcodes.io's ``admin_county`` (falling back to ``admin_district`` for
unitaries) so the filter values match CQC's vocabulary.

Why this exists
---------------
Carterwood competition lists name each competing care home but carry no
postcode or address, so name-only geocoding (Nominatim) is both
incomplete (most names don't resolve) and occasionally *wrong* (generic
names like "Montague House" match an unrelated building elsewhere in the
country).  This module resolves competitors authoritatively by listing
the CQC care homes that actually sit inside the catchment and matching
Carterwood names against only that radius-bounded candidate set — so a
wrong-entity match is geometrically impossible.

Pipeline (see :func:`find_care_homes_near`)
    1. Discover the local authorities the catchment circle touches
       (reverse-geocode the centre + circle-edge samples via postcodes.io).
    2. List CQC care homes in those authorities (name + postcode + id).
    3. Bulk-geocode the postcodes via postcodes.io to get coordinates.
    4. Keep the homes whose centroid falls within ``radius_km``.
    5. Optionally fuzzy-match a list of Carterwood names against that set
       and enrich the matches with CQC rating + bed count.
"""

from __future__ import annotations

import asyncio

_CQC_BASE = "https://api.service.cqc.org.uk/public/v1"
_USER_AGENT = "unify-valos/0.6 (https://unify.ai)"

# Earth mean radius (km) for haversine.
_EARTH_RADIUS_KM = 6371.0088

# Minimum normalised-name similarity for a Carterwood name to be accepted
# as a match against a CQC candidate.  Tunable via env at call sites.
_DEFAULT_MATCH_THRESHOLD = 0.6


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points, in kilometres."""
    import math

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def normalise_name(name: str) -> str:
    """Strip care-sector boilerplate and punctuation for fuzzy matching."""
    import re

    s = (name or "").lower()
    for token in (
        "care home",
        "nursing home",
        "residential home",
        "care centre",
        "care center",
        "care services",
        "rest home",
        "retirement home",
        "limited",
        "ltd",
    ):
        s = s.replace(token, " ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def name_similarity(a: str, b: str) -> float:
    """Similarity in [0, 1] combining sequence ratio and token overlap."""
    from difflib import SequenceMatcher

    na = normalise_name(a)
    nb = normalise_name(b)
    if not na or not nb:
        return 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    ta = set(na.split())
    tb = set(nb.split())
    overlap = len(ta & tb) / len(ta | tb) if (ta and tb) else 0.0
    return max(ratio, overlap)


# ---------------------------------------------------------------------------
# Low-level HTTP
# ---------------------------------------------------------------------------


def _subscription_key_or_none() -> str | None:
    import os

    key = os.environ.get("CQC_PRIMARY_KEY", "")
    return key or None


async def _cqc_get(client, path: str, *, params: dict | None = None) -> dict:
    """GET a CQC Syndication endpoint, returning parsed JSON or an error dict."""
    from droid_deploy.assistant_deployments.integrations.packages.valos.functions._metering import (
        PROVIDER_CQC,
        quota_envelope,
        record_and_check,
    )

    key = _subscription_key_or_none()
    if key is None:
        return {
            "error": "CQC_PRIMARY_KEY is not configured.",
            "status_code": None,
            "auth_error": True,
            "hint": (
                "valos_find_care_homes needs CQC_PRIMARY_KEY (a free CQC "
                "subscription key from api-portal.service.cqc.org.uk) in the "
                "assistant's /Secrets context."
            ),
        }

    exceeded, count, limit = record_and_check(PROVIDER_CQC)
    if exceeded:
        return quota_envelope(PROVIDER_CQC, count, limit)

    # The migrated API rejects a ``partnerCode`` query param (400) and
    # authenticates purely via the subscription-key header.
    resp = await client.get(
        f"{_CQC_BASE}{path}",
        params=dict(params or {}),
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "Ocp-Apim-Subscription-Key": key,
        },
    )
    if resp.status_code != 200:
        return {
            "error": f"CQC GET {path} returned {resp.status_code}",
            "status_code": resp.status_code,
            "auth_error": resp.status_code in (401, 403),
            "body": resp.text[:300] if resp.text else "",
        }
    return resp.json() or {}


async def cqc_care_homes_in_authority(
    client,
    local_authority: str,
    *,
    max_pages: int = 10,
    per_page: int = 1000,
) -> tuple[list[dict], dict | None]:
    """List care-home locations in a local authority.

    Returns ``(homes, error)`` where ``homes`` is
    ``[{"location_id", "name", "postcode"}, ...]`` and ``error`` is the
    upstream failure dict if one occurred (so the caller can distinguish
    "authority genuinely empty" from "auth/transport failure" rather than
    silently treating a 401 as an empty catchment).
    """
    homes: list[dict] = []
    page = 1
    while page <= max_pages:
        payload = await _cqc_get(
            client,
            "/locations",
            params={
                "careHome": "Y",
                "localAuthority": local_authority,
                "page": page,
                "perPage": per_page,
            },
        )
        if not isinstance(payload, dict) or payload.get("error"):
            return homes, (payload if isinstance(payload, dict) else None)
        for loc in payload.get("locations") or []:
            homes.append(
                {
                    "location_id": loc.get("locationId"),
                    "name": loc.get("locationName"),
                    "postcode": loc.get("postalCode"),
                },
            )
        total_pages = payload.get("totalPages") or 1
        if page >= total_pages:
            break
        page += 1
    return homes, None


async def cqc_location_detail(client, location_id: str) -> dict:
    """Fetch rating + bed count + authoritative coordinates for one location."""
    payload = await _cqc_get(client, f"/locations/{location_id}")
    if not isinstance(payload, dict) or payload.get("error"):
        return {"error": "detail unavailable", "location_id": location_id}
    ratings = payload.get("currentRatings") or {}
    overall = (ratings.get("overall") or {}).get("rating")
    return {
        "location_id": location_id,
        "name": payload.get("name"),
        "postcode": payload.get("postalCode"),
        "lat": _safe_float(payload.get("onspdLatitude")),
        "lon": _safe_float(payload.get("onspdLongitude")),
        "beds": payload.get("numberOfBeds"),
        "cqc_rating": overall,
    }


# ---------------------------------------------------------------------------
# postcodes.io support (LA discovery + bulk geocode)
# ---------------------------------------------------------------------------


async def _authorities_touching_catchment(
    client,
    lat: float,
    lon: float,
    radius_km: float,
) -> list[str]:
    """Reverse-geocode the centre + circle-edge samples to find local authorities."""
    import math

    points = [(lat, lon)]
    lat_r = math.radians(lat)
    for i in range(8):
        ang = 2 * math.pi * i / 8
        dy = radius_km * 1000.0 * math.sin(ang)
        dx = radius_km * 1000.0 * math.cos(ang)
        dlat = dy / 111_320.0
        dlon = dx / (111_320.0 * max(0.1, math.cos(lat_r)))
        points.append((lat + dlat, lon + dlon))

    authorities: list[str] = []
    seen: set[str] = set()
    for plat, plon in points:
        resp = await client.get(
            "https://api.postcodes.io/postcodes",
            params={"lon": plon, "lat": plat, "limit": 1},
            headers={"User-Agent": _USER_AGENT},
        )
        if resp.status_code != 200:
            continue
        result = (resp.json() or {}).get("result") or []
        if not result:
            continue
        # CQC's localAuthority filter is upper-tier: county for two-tier
        # areas, unitary name otherwise.  postcodes.io exposes the county
        # as admin_county (empty for unitaries, where admin_district is the
        # unitary).  Prefer county, fall back to district.
        res = result[0]
        la = res.get("admin_county") or res.get("admin_district")
        if la and la not in seen:
            seen.add(la)
            authorities.append(la)
    return authorities


async def _bulk_geocode_postcodes(client, postcodes: list[str]) -> dict[str, dict]:
    """Resolve many postcodes to coordinates via the postcodes.io bulk endpoint.

    Returns ``{normalised_postcode: {"lat", "lon"}}``.  Chunks at 100 per
    request (the postcodes.io bulk ceiling).
    """
    coords: dict[str, dict] = {}
    cleaned = [p for p in {(pc or "").strip() for pc in postcodes} if p]
    for start in range(0, len(cleaned), 100):
        chunk = cleaned[start : start + 100]
        resp = await client.post(
            "https://api.postcodes.io/postcodes",
            json={"postcodes": chunk},
            headers={"User-Agent": _USER_AGENT},
        )
        if resp.status_code != 200:
            continue
        for entry in (resp.json() or {}).get("result") or []:
            query = (entry.get("query") or "").upper().replace(" ", "")
            res = entry.get("result")
            if res and query:
                coords[query] = {
                    "lat": _safe_float(res.get("latitude")),
                    "lon": _safe_float(res.get("longitude")),
                }
    return coords


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def find_care_homes_near(
    lat: float,
    lon: float,
    radius_km: float,
    *,
    names: list[str] | None = None,
    timeout: float | None = None,
) -> dict:
    """Resolve CQC care homes within ``radius_km`` of a point.

    See module docstring for the pipeline.  When ``names`` is supplied,
    each name is fuzzy-matched against the radius-bounded candidate set
    and enriched with CQC rating + bed count.
    """
    import os

    import httpx

    timeout = timeout or float(os.environ.get("CQC_REQUEST_TIMEOUT_SECONDS", "30"))
    threshold = float(
        os.environ.get("VALOS_CQC_MATCH_THRESHOLD", str(_DEFAULT_MATCH_THRESHOLD)),
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        authorities = await _authorities_touching_catchment(
            client,
            lat,
            lon,
            radius_km,
        )
        if not authorities:
            return {
                "error": "Could not resolve any local authority for the catchment.",
                "centre": {"lat": lat, "lon": lon},
                "radius_km": radius_km,
            }

        listed: list[dict] = []
        last_error: dict | None = None
        for la in authorities:
            homes, err = await cqc_care_homes_in_authority(client, la)
            listed.extend(homes)
            if err is not None:
                last_error = err

        # If every authority listing failed and we got nothing, surface the
        # upstream failure (e.g. missing/invalid CQC key) rather than letting
        # it masquerade as an empty catchment.
        if not listed and last_error is not None:
            return {
                "error": (
                    "CQC lookup failed for the catchment authorities "
                    f"({', '.join(authorities)})."
                ),
                "centre": {"lat": lat, "lon": lon},
                "radius_km": radius_km,
                "authorities_searched": authorities,
                "upstream_error": last_error,
            }

        # De-duplicate by location id (authorities can overlap on edges).
        by_id = {h["location_id"]: h for h in listed if h.get("location_id")}
        candidates = list(by_id.values())

        coords = await _bulk_geocode_postcodes(
            client,
            [h.get("postcode") for h in candidates],
        )

        in_radius: list[dict] = []
        for home in candidates:
            key = (home.get("postcode") or "").upper().replace(" ", "")
            pc = coords.get(key)
            if not pc or pc["lat"] is None or pc["lon"] is None:
                continue
            dist = haversine_km(lat, lon, pc["lat"], pc["lon"])
            if dist <= radius_km:
                in_radius.append(
                    {
                        "name": home.get("name"),
                        "postcode": home.get("postcode"),
                        "lat": pc["lat"],
                        "lon": pc["lon"],
                        "distance_km": round(dist, 3),
                        "cqc_location_id": home.get("location_id"),
                        "cqc_rating": None,
                        "beds": None,
                    },
                )
        in_radius.sort(key=lambda h: h["distance_km"])

        matches: dict[str, dict | None] = {}
        unmatched: list[str] = []
        enrich_ids: set[str] = set()
        if names:
            for raw in names:
                best = None
                best_score = 0.0
                for cand in in_radius:
                    score = name_similarity(raw, cand.get("name") or "")
                    if score > best_score:
                        best_score = score
                        best = cand
                if best is not None and best_score >= threshold:
                    matches[raw] = {
                        "matched_name": best["name"],
                        "lat": best["lat"],
                        "lon": best["lon"],
                        "distance_km": best["distance_km"],
                        "confidence": round(best_score, 3),
                        "cqc_location_id": best["cqc_location_id"],
                    }
                    enrich_ids.add(best["cqc_location_id"])
                else:
                    matches[raw] = None
                    unmatched.append(raw)

        # Enrich the homes we'll actually surface (matched set when names
        # were given, otherwise the whole in-radius set) with rating + beds.
        targets = (
            [h for h in in_radius if h["cqc_location_id"] in enrich_ids]
            if names
            else in_radius
        )
        details = await asyncio.gather(
            *[cqc_location_detail(client, h["cqc_location_id"]) for h in targets],
        )
        detail_by_id = {d["location_id"]: d for d in details if not d.get("error")}
        for home in in_radius:
            d = detail_by_id.get(home["cqc_location_id"])
            if not d:
                continue
            home["cqc_rating"] = d.get("cqc_rating")
            home["beds"] = d.get("beds")
            # Prefer CQC's ONSPD coordinate over the postcode centroid.
            if d.get("lat") is not None and d.get("lon") is not None:
                home["lat"] = d["lat"]
                home["lon"] = d["lon"]
                home["distance_km"] = round(
                    haversine_km(lat, lon, d["lat"], d["lon"]),
                    3,
                )
        # Propagate enriched rating/beds into the match payloads.
        if names:
            enriched_by_id = {h["cqc_location_id"]: h for h in in_radius}
            for match in matches.values():
                if not match:
                    continue
                h = enriched_by_id.get(match["cqc_location_id"])
                if h:
                    match["cqc_rating"] = h.get("cqc_rating")
                    match["beds"] = h.get("beds")
                    match["lat"] = h["lat"]
                    match["lon"] = h["lon"]
                    match["distance_km"] = h["distance_km"]

    result = {
        "centre": {"lat": lat, "lon": lon},
        "radius_km": radius_km,
        "authorities_searched": authorities,
        "care_home_count": len(in_radius),
        "care_homes": in_radius,
        "attribution": (
            "Contains public sector information licensed under the Open "
            "Government Licence v3.0 (CQC)."
        ),
    }
    if names:
        result["matches"] = matches
        result["unmatched"] = unmatched
    return result


def _safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
