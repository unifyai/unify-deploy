"""GCP VM placement selection from an IANA timezone and live capabilities."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
from pathlib import Path
import threading
import time
from typing import Callable, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.api_core.exceptions import GoogleAPICallError, NotFound
from google.auth.exceptions import GoogleAuthError
from google.cloud import compute_v1

_DATA_DIR = Path(__file__).with_name("data")
_EARTH_RADIUS_KM = 6_371.0088
_LEGACY_LOCATION_ID = "us-central1"
_DEFAULT_CAPABILITY_CACHE_TTL_SECONDS = 300.0
_TIMEZONE_ALIASES = {
    "UTC": "Etc/UTC",
    "GMT": "Etc/GMT",
    "US/Eastern": "America/New_York",
    "US/Central": "America/Chicago",
    "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles",
}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PoolLocation:
    """A Compute Engine region supported by the placement catalog."""

    id: str
    region: str
    display_name: str
    latitude: float
    longitude: float
    zones: tuple[str, ...]


@dataclass(frozen=True)
class VmPlacement:
    """A location selected for an assistant VM and its regional IP."""

    location: PoolLocation
    zone: str
    source_timezone: str
    resolution: str

    @property
    def region(self) -> str:
        return self.location.region


def _read_json(name: str) -> object:
    return json.loads((_DATA_DIR / name).read_text(encoding="utf-8"))


def _load_locations() -> tuple[PoolLocation, ...]:
    rows = _read_json("gcp_vm_regions.json")
    if not isinstance(rows, list):
        raise RuntimeError("GCP VM region catalog must be a JSON list")
    locations = tuple(
        PoolLocation(
            id=str(row["id"]),
            region=str(row["id"]),
            display_name=str(row["display_name"]),
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
            zones=tuple(str(zone) for zone in row["zones"]),
        )
        for row in rows
        if isinstance(row, dict)
    )
    if not locations or len({location.id for location in locations}) != len(locations):
        raise RuntimeError("GCP VM region catalog contains missing or duplicate IDs")
    return locations


_LOCATIONS = _load_locations()
_LOCATIONS_BY_ID = {location.id: location for location in _LOCATIONS}
_TIMEZONE_CENTROIDS = _read_json("iana_timezone_centroids.json")
_capability_cache_lock = threading.Lock()
_capability_cache: dict[tuple[str, str, str], tuple[float, str | None, bool]] = {}


def list_pool_locations() -> tuple[PoolLocation, ...]:
    """Return all public Compute Engine regions known to this catalog."""
    return _LOCATIONS


def get_pool_location(location_id: str) -> PoolLocation:
    """Return one catalog location or fail clearly for an invalid setting."""
    try:
        return _LOCATIONS_BY_ID[location_id]
    except KeyError as exc:
        raise ValueError(f"Unknown GCP VM pool location: {location_id}") from exc


def validate_iana_timezone(timezone_name: str | None) -> str | None:
    """Validate and normalize an optional timezone without using its UTC offset."""
    if not timezone_name or not timezone_name.strip():
        return None
    candidate = _TIMEZONE_ALIASES.get(timezone_name.strip(), timezone_name.strip())
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {timezone_name}") from exc
    return candidate


def _distance_km(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    lat_a, lon_a, lat_b, lon_b = map(
        math.radians,
        (latitude_a, longitude_a, latitude_b, longitude_b),
    )
    return (
        _EARTH_RADIUS_KM
        * 2
        * math.asin(
            math.sqrt(
                math.sin((lat_b - lat_a) / 2) ** 2
                + math.cos(lat_a)
                * math.cos(lat_b)
                * math.sin((lon_b - lon_a) / 2) ** 2,
            ),
        )
    )


def _nearest_location(
    latitude: float,
    longitude: float,
    candidates: Iterable[PoolLocation],
) -> PoolLocation:
    available = tuple(candidates)
    if not available:
        raise ValueError("Cannot select a location from an empty pool")
    return min(
        available,
        key=lambda location: (
            _distance_km(latitude, longitude, location.latitude, location.longitude),
            location.id,
        ),
    )


def _locations_by_distance(
    latitude: float,
    longitude: float,
    candidates: Iterable[PoolLocation],
) -> tuple[PoolLocation, ...]:
    return tuple(
        sorted(
            candidates,
            key=lambda location: (
                _distance_km(
                    latitude,
                    longitude,
                    location.latitude,
                    location.longitude,
                ),
                location.id,
            ),
        ),
    )


def _timezone_coordinates(timezone_name: str) -> tuple[float, float] | None:
    entry = (
        _TIMEZONE_CENTROIDS.get(timezone_name)
        if isinstance(_TIMEZONE_CENTROIDS, dict)
        else None
    )
    if not isinstance(entry, dict):
        return None
    try:
        return float(entry["latitude"]), float(entry["longitude"])
    except (KeyError, TypeError, ValueError):
        return None


def resolve_pool_location(
    timezone_name: str | None,
    *,
    provisioned_locations: Mapping[str, str] | None = None,
    legacy_location_id: str = _LEGACY_LOCATION_ID,
) -> VmPlacement:
    """Select the nearest provisioned region for an assistant timezone.

    ``provisioned_locations`` maps catalog IDs to their active VM zone.  When
    omitted or empty, it intentionally preserves today's single Iowa pool.
    The full catalog still determines the preferred public region; only the
    final resource placement is constrained by provisioned capacity.
    """
    legacy = get_pool_location(legacy_location_id)
    normalized_timezone = validate_iana_timezone(timezone_name)
    coordinates = (
        _timezone_coordinates(normalized_timezone) if normalized_timezone else None
    )
    enabled = {
        location_id: zone
        for location_id, zone in (provisioned_locations or {}).items()
        if location_id in _LOCATIONS_BY_ID and zone
    }
    if not enabled:
        return VmPlacement(
            location=legacy,
            zone=legacy.zones[0],
            source_timezone=normalized_timezone or "",
            resolution="legacy_default",
        )

    candidates = tuple(_LOCATIONS_BY_ID[location_id] for location_id in enabled)
    if coordinates is None:
        selected = (
            legacy
            if legacy.id in enabled
            else sorted(candidates, key=lambda item: item.id)[0]
        )
        resolution = "legacy_fallback"
    else:
        selected = _nearest_location(*coordinates, candidates)
        resolution = "nearest_provisioned"
    zone = enabled[selected.id]
    if zone not in selected.zones:
        raise ValueError(
            f"Zone {zone} is not part of configured pool location {selected.id}",
        )
    return VmPlacement(
        location=selected,
        zone=zone,
        source_timezone=normalized_timezone or "",
        resolution=resolution,
    )


def clear_location_capability_cache() -> None:
    """Drop cached GCP preflight results (primarily for tests)."""
    with _capability_cache_lock:
        _capability_cache.clear()


def _probe_zone_capability(
    project_id: str,
    zone: str,
    machine_type: str | None,
) -> bool:
    """Return whether a zone can host the requested VM capability."""
    if machine_type:
        compute_v1.MachineTypesClient().get(
            project=project_id,
            zone=zone,
            machine_type=machine_type,
        )
    else:
        compute_v1.ZonesClient().get(project=project_id, zone=zone)
    return True


def _cached_capable_zone(
    *,
    project_id: str,
    location: PoolLocation,
    machine_type: str | None,
    cache_ttl_seconds: float,
    capability_probe: Callable[[str, str, str | None], bool],
) -> tuple[str | None, bool]:
    """Return a usable zone and whether GCP completed the preflight."""
    capability_name = machine_type or "__zone__"
    cache_key = (project_id, location.id, capability_name)
    now = time.monotonic()
    with _capability_cache_lock:
        cached = _capability_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1], cached[2]

    available_zone: str | None = None
    try:
        for zone in location.zones:
            try:
                if capability_probe(project_id, zone, machine_type):
                    available_zone = zone
                    break
            except NotFound:
                continue
    except (GoogleAPICallError, GoogleAuthError, OSError) as exc:
        # A transient IAM, credential, or transport failure is not evidence
        # that every catalog region is unusable. Preserve the Iowa fallback.
        logger.warning(
            "GCP location capability preflight unavailable for %s: %s",
            location.id,
            exc,
        )
        with _capability_cache_lock:
            _capability_cache[cache_key] = (
                time.monotonic() + cache_ttl_seconds,
                None,
                False,
            )
        return None, False

    with _capability_cache_lock:
        _capability_cache[cache_key] = (
            time.monotonic() + cache_ttl_seconds,
            available_zone,
            True,
        )
    return available_zone, True


def resolve_viable_pool_location(
    timezone_name: str | None,
    *,
    project_id: str,
    machine_type: str | None = None,
    legacy_location_id: str = _LEGACY_LOCATION_ID,
    legacy_zone: str | None = None,
    cache_ttl_seconds: float = _DEFAULT_CAPABILITY_CACHE_TTL_SECONDS,
    candidate_location_ids: Iterable[str] | None = None,
    capability_probe: Callable[[str, str, str | None], bool] = _probe_zone_capability,
) -> VmPlacement:
    """Select the nearest catalog location currently capable in this project.

    The preflight checks the requested machine type (or the zone itself) in
    distance order and caches its result locally. It neither reads nor mutates
    Cloud Run environment variables. If GCP cannot confirm any location, the
    existing Iowa placement remains the safe fallback.
    """
    if cache_ttl_seconds <= 0:
        raise ValueError("cache_ttl_seconds must be positive")
    legacy = get_pool_location(legacy_location_id)
    if legacy_zone is not None and legacy_zone not in legacy.zones:
        raise ValueError(
            f"Zone {legacy_zone} is not part of legacy pool location {legacy.id}",
        )
    normalized_timezone = validate_iana_timezone(timezone_name)
    coordinates = (
        _timezone_coordinates(normalized_timezone) if normalized_timezone else None
    )
    if candidate_location_ids is None:
        candidates = _LOCATIONS
    else:
        candidate_ids = tuple(candidate_location_ids)
        candidates = tuple(
            get_pool_location(location_id) for location_id in candidate_ids
        )
    if not candidates:
        raise ValueError("Cannot select a location from an empty pool")
    ordered = (
        _locations_by_distance(*coordinates, candidates)
        if coordinates is not None
        else (
            (legacy,)
            if legacy in candidates
            else tuple(sorted(candidates, key=lambda item: item.id))
        )
    )
    for location in ordered:
        zone, preflight_completed = _cached_capable_zone(
            project_id=project_id,
            location=location,
            machine_type=machine_type,
            cache_ttl_seconds=cache_ttl_seconds,
            capability_probe=capability_probe,
        )
        if zone:
            return VmPlacement(
                location=location,
                zone=zone,
                source_timezone=normalized_timezone or "",
                resolution="nearest_capable",
            )
        if not preflight_completed:
            break
    return VmPlacement(
        location=legacy,
        zone=legacy_zone or legacy.zones[0],
        source_timezone=normalized_timezone or "",
        resolution="legacy_fallback",
    )


def placement_from_ref(value: Mapping[str, object] | None) -> VmPlacement | None:
    """Read location routing metadata from an AssistantSession VM reference."""
    if not value:
        return None
    location_id = str(value.get("poolLocation") or value.get("region") or "").strip()
    declared_region = str(value.get("region") or "").strip()
    zone = str(value.get("zone") or "").strip()
    if not location_id or not zone:
        return None
    location = get_pool_location(location_id)
    if declared_region and declared_region != location.region:
        raise ValueError(
            f"Pool location {location_id} does not match declared region {declared_region}",
        )
    if zone not in location.zones:
        raise ValueError(f"Zone {zone} is not part of pool location {location_id}")
    return VmPlacement(
        location=location,
        zone=zone,
        source_timezone="",
        resolution="persisted",
    )
