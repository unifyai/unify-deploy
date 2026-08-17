from __future__ import annotations

import pytest

from communication.infra.gcp_region_catalog import (
    clear_location_capability_cache,
    get_pool_location,
    list_pool_locations,
    placement_from_ref,
    resolve_pool_location,
    resolve_viable_pool_location,
)
from communication.infra.vm_helpers import (
    _assistant_disk_name,
    _pool_ip_name,
    _pool_vm_name,
    vm_placement_scope,
)
from communication.infra.assistant_sessions import build_assistant_session_spec


def _all_catalog_locations() -> dict[str, str]:
    return {location.id: location.zones[0] for location in list_pool_locations()}


def test_catalog_contains_all_supported_compute_regions():
    locations = list_pool_locations()
    assert len(locations) >= 43
    assert len({location.id for location in locations}) == len(locations)
    assert {"us-central1", "europe-southwest1", "asia-northeast1"} <= {
        location.id for location in locations
    }


def test_casablanca_resolves_to_madrid_when_all_locations_are_provisioned():
    placement = resolve_pool_location(
        "Africa/Casablanca",
        provisioned_locations=_all_catalog_locations(),
    )

    assert placement.region == "europe-southwest1"
    assert placement.resolution == "nearest_provisioned"


def test_only_legacy_pool_preserves_current_placement():
    placement = resolve_pool_location(
        "Africa/Casablanca",
        provisioned_locations={"us-central1": "us-central1-a"},
    )

    assert placement.region == "us-central1"
    assert placement.zone == "us-central1-a"


def test_preflight_selects_nearest_capable_catalog_location():
    clear_location_capability_cache()
    checked_zones: list[str] = []

    def probe(_project: str, zone: str, _machine_type: str | None) -> bool:
        checked_zones.append(zone)
        return zone == "europe-southwest1-a"

    placement = resolve_viable_pool_location(
        "Africa/Casablanca",
        project_id="test-project",
        machine_type="e2-standard-2",
        capability_probe=probe,
    )

    assert placement.region == "europe-southwest1"
    assert placement.zone == "europe-southwest1-a"
    assert placement.resolution == "nearest_capable"
    assert checked_zones == ["europe-southwest1-a"]


def test_preflight_cache_avoids_rechecking_a_capable_location():
    clear_location_capability_cache()
    calls = 0

    def probe(_project: str, _zone: str, _machine_type: str | None) -> bool:
        nonlocal calls
        calls += 1
        return True

    kwargs = {
        "project_id": "test-project",
        "machine_type": "e2-standard-2",
        "candidate_location_ids": ("europe-southwest1",),
        "capability_probe": probe,
    }
    resolve_viable_pool_location("Africa/Casablanca", **kwargs)
    resolve_viable_pool_location("Africa/Casablanca", **kwargs)

    assert calls == 1


def test_preflight_failure_keeps_the_legacy_iowa_placement():
    clear_location_capability_cache()
    placement = resolve_viable_pool_location(
        "Africa/Casablanca",
        project_id="test-project",
        legacy_zone="us-central1-f",
        capability_probe=lambda _project, _zone, _machine_type: False,
    )

    assert placement.region == "us-central1"
    assert placement.zone == "us-central1-f"
    assert placement.resolution == "legacy_fallback"


def test_unconfigured_catalog_location_is_rejected():
    with pytest.raises(ValueError, match="Unknown GCP VM pool location"):
        get_pool_location("not-a-region")


def test_invalid_timezone_is_rejected():
    with pytest.raises(ValueError, match="Unknown IANA timezone"):
        resolve_pool_location("not/a-timezone")


def test_nonlegacy_placement_scopes_pool_and_disk_resource_names():
    placement = resolve_pool_location(
        "Africa/Casablanca",
        provisioned_locations={"europe-southwest1": "europe-southwest1-a"},
    )

    with vm_placement_scope(placement):
        assert "europe-southwest1" in _pool_vm_name("ubuntu", 1)
        assert "europe-southwest1" in _pool_ip_name("ubuntu", 1)
        assert "europe-southwest1" in _assistant_disk_name("123")


def test_vm_ref_rejects_a_region_that_disagrees_with_its_pool_location():
    with pytest.raises(ValueError, match="does not match declared region"):
        placement_from_ref(
            {
                "poolLocation": "europe-southwest1",
                "region": "us-central1",
                "zone": "europe-southwest1-a",
            },
        )


def test_session_spec_preserves_the_selected_desktop_placement():
    placement = resolve_pool_location(
        "Africa/Casablanca",
        provisioned_locations={"europe-southwest1": "europe-southwest1-a"},
    )
    spec = build_assistant_session_spec(
        assistant_id="123",
        user_id="456",
        medium="test",
        desktop_mode="ubuntu",
        startup_secret_ref="secret",
        activation_id="activation",
        desktop_placement={
            "poolLocation": placement.location.id,
            "region": placement.region,
            "zone": placement.zone,
        },
    )

    assert spec["desktop"]["placement"]["poolLocation"] == "europe-southwest1"
    assert spec["desktop"]["placement"]["zone"] == "europe-southwest1-a"
