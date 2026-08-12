from unittest.mock import MagicMock, call, patch
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.api_core.exceptions import NotFound

from communication.infra import vm_helpers
from communication.infra.gcp_region_catalog import VmPlacement, get_pool_location


def _address(assistant_id: str, *, ip: str = "34.1.2.3") -> MagicMock:
    address = MagicMock()
    address.name = vm_helpers.assistant_static_ip_name(assistant_id)
    address.address = ip
    address.status = "RESERVED"
    address.region = "regions/us-central1"
    address.labels = vm_helpers.assistant_static_ip_labels(assistant_id)
    return address


def test_reserve_assistant_static_ip_creates_unattached_address():
    assistant_id = "assistant-123"
    client = MagicMock()
    client.get.side_effect = [
        NotFound("missing"),
        _address(assistant_id),
        _address(assistant_id),
    ]

    with patch.object(vm_helpers.compute_v1, "AddressesClient", return_value=client):
        result = vm_helpers.reserve_assistant_static_ip(assistant_id)

    assert result["created"] is True
    assert result["address"] == "34.1.2.3"
    resource = client.insert.call_args.kwargs["address_resource"]
    assert resource.name == vm_helpers.assistant_static_ip_name(assistant_id)
    assert resource.labels == vm_helpers.assistant_static_ip_labels(assistant_id)


def test_reserve_assistant_static_ip_repairs_missing_labels_after_create():
    assistant_id = "assistant-123"
    created_address = _address(assistant_id)
    created_address.labels = {}
    created_address.label_fingerprint = "address-label-fingerprint"
    client = MagicMock()
    client.get.side_effect = [
        NotFound("missing"),
        created_address,
        _address(assistant_id),
    ]

    with patch.object(vm_helpers.compute_v1, "AddressesClient", return_value=client):
        vm_helpers.reserve_assistant_static_ip(assistant_id)

    request = client.set_labels.call_args.kwargs["region_set_labels_request_resource"]
    assert client.set_labels.call_args.kwargs["resource"] == (
        vm_helpers.assistant_static_ip_name(assistant_id)
    )
    assert request.labels == vm_helpers.assistant_static_ip_labels(assistant_id)
    assert request.label_fingerprint == "address-label-fingerprint"


def test_reserve_assistant_static_ip_reuses_only_owned_address():
    assistant_id = "assistant-123"
    client = MagicMock()
    client.get.return_value = _address(assistant_id)

    with patch.object(vm_helpers.compute_v1, "AddressesClient", return_value=client):
        result = vm_helpers.reserve_assistant_static_ip(assistant_id)

    assert result["created"] is False
    client.insert.assert_not_called()


def test_release_assistant_static_ip_is_idempotent_and_checks_ownership():
    assistant_id = "assistant-123"
    client = MagicMock()
    client.get.side_effect = [_address(assistant_id), NotFound("missing")]
    dns_delete = MagicMock(side_effect=[True, False])

    with (
        patch.object(vm_helpers.compute_v1, "AddressesClient", return_value=client),
        patch.object(vm_helpers, "_delete_dns_a_record", dns_delete),
    ):
        assert vm_helpers.release_assistant_static_ip(assistant_id) == {
            "released": True,
            "dns_deleted": True,
        }
        assert vm_helpers.release_assistant_static_ip(assistant_id) == {
            "released": False,
            "dns_deleted": False,
        }

    client.delete.assert_called_once_with(
        project=vm_helpers.SETTINGS.vm_project_id,
        region=vm_helpers.SETTINGS.vm_region,
        address=vm_helpers.assistant_static_ip_name(assistant_id),
    )
    # A release whose address is already gone still reaps the record, so a
    # partial release cannot strand one forever.
    hostname = vm_helpers.get_dns_hostname(assistant_id)
    assert dns_delete.call_args_list == [call(hostname), call(hostname)]


def test_release_assistant_static_ip_deletes_dns_before_address():
    """The record must go first, or a failed address delete strands it.

    With the address deleted first, its ``NotFound`` short-circuit on the retry
    returns before the record is ever considered.
    """

    assistant_id = "assistant-123"
    order: list[str] = []

    def delete_address(**_):
        order.append("address")
        return MagicMock()

    def delete_record(hostname: str) -> bool:
        order.append("dns")
        return True

    client = MagicMock()
    client.get.return_value = _address(assistant_id)
    client.delete.side_effect = delete_address

    with (
        patch.object(vm_helpers.compute_v1, "AddressesClient", return_value=client),
        patch.object(vm_helpers, "_delete_dns_a_record", delete_record),
    ):
        vm_helpers.release_assistant_static_ip(assistant_id)

    assert order == ["dns", "address"]


def test_release_assistant_static_ip_keeps_record_of_foreign_address():
    assistant_id = "assistant-123"
    foreign = _address(assistant_id)
    foreign.labels = {
        **vm_helpers.assistant_static_ip_labels("someone-else"),
    }
    client = MagicMock()
    client.get.return_value = foreign
    dns_delete = MagicMock()

    with (
        patch.object(vm_helpers.compute_v1, "AddressesClient", return_value=client),
        patch.object(vm_helpers, "_delete_dns_a_record", dns_delete),
        pytest.raises(ValueError),
    ):
        vm_helpers.release_assistant_static_ip(assistant_id)

    dns_delete.assert_not_called()
    client.delete.assert_not_called()


def _vm_with_external_ip(name: str, ip: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        network_interfaces=[
            SimpleNamespace(
                name="nic0",
                access_configs=[
                    SimpleNamespace(name="External NAT", nat_i_p=ip),
                ],
            ),
        ],
    )


def test_attach_assistant_static_ip_replaces_pool_ip_and_updates_stable_dns():
    vm_name = vm_helpers._pool_vm_name("ubuntu", 1)
    client = MagicMock()
    client.get.return_value = _vm_with_external_ip(
        vm_name,
        "34.0.0.1",
    )
    dns_upsert = MagicMock()
    report_attachment = MagicMock()

    with (
        patch.object(
            vm_helpers,
            "reserve_assistant_static_ip",
            return_value={
                "address": "34.0.0.9",
            },
        ),
        patch.object(
            vm_helpers,
            "_wait_for_pool_static_ip",
            return_value="34.0.0.1",
        ),
        patch.object(vm_helpers.compute_v1, "InstancesClient", return_value=client),
        patch.object(vm_helpers, "_upsert_dns_a_record", dns_upsert),
        patch.object(
            vm_helpers,
            "_report_assistant_static_ip_attachment",
            report_attachment,
        ),
    ):
        result = vm_helpers.attach_assistant_static_ip_to_pool_vm(
            vm_name,
            "assistant-123",
            "ubuntu",
        )

    client.delete_access_config.assert_called_once()
    added = client.add_access_config.call_args.kwargs["access_config_resource"]
    assert added.nat_i_p == "34.0.0.9"
    assert result == {
        "hostname": vm_helpers.get_dns_hostname("assistant-123"),
        "ip_address": "34.0.0.9",
    }
    dns_upsert.assert_called_once_with(result["hostname"], "34.0.0.9")
    report_attachment.assert_called_once_with(
        assistant_id="assistant-123",
        address_name=vm_helpers.assistant_static_ip_name("assistant-123"),
        address="34.0.0.9",
        hostname=result["hostname"],
    )


def test_restore_pool_static_ip_keeps_assistant_reservation():
    vm_name = vm_helpers._pool_vm_name("ubuntu", 1)
    client = MagicMock()
    client.get.return_value = _vm_with_external_ip(
        vm_name,
        "34.0.0.9",
    )

    with (
        patch.object(
            vm_helpers,
            "_wait_for_pool_static_ip",
            return_value="34.0.0.1",
        ),
        patch.object(
            vm_helpers,
            "get_assistant_static_ip",
            return_value={"address": "34.0.0.9"},
        ),
        patch.object(vm_helpers.compute_v1, "InstancesClient", return_value=client),
    ):
        vm_helpers.restore_pool_static_ip_on_vm(
            vm_name,
            "assistant-123",
            "ubuntu",
        )

    client.delete_access_config.assert_called_once()
    added = client.add_access_config.call_args.kwargs["access_config_resource"]
    assert added.nat_i_p == "34.0.0.1"


def test_restore_pool_static_ip_replaces_untracked_legacy_address():
    vm_name = vm_helpers._pool_vm_name("ubuntu", 1)
    client = MagicMock()
    client.get.return_value = _vm_with_external_ip(
        vm_name,
        "203.0.113.17",
    )

    with (
        patch.object(
            vm_helpers,
            "_wait_for_pool_static_ip",
            return_value="34.0.0.1",
        ),
        patch.object(
            vm_helpers,
            "get_assistant_static_ip",
            return_value=None,
        ),
        patch.object(vm_helpers.compute_v1, "InstancesClient", return_value=client),
    ):
        vm_helpers.restore_pool_static_ip_on_vm(
            vm_name,
            "assistant-123",
            "ubuntu",
        )

    client.delete_access_config.assert_called_once()
    added = client.add_access_config.call_args.kwargs["access_config_resource"]
    assert added.nat_i_p == "34.0.0.1"


def test_assistant_static_ip_routes_reconcile_read_and_release():
    from communication.infra import views

    app = FastAPI()
    app.include_router(views.router, prefix="/infra")
    client = TestClient(app)
    state = {
        "name": vm_helpers.assistant_static_ip_name("assistant-123"),
        "address": "34.1.2.3",
        "status": "RESERVED",
        "region": "regions/us-central1",
        "labels": vm_helpers.assistant_static_ip_labels("assistant-123"),
    }

    with (
        patch.object(
            views,
            "reserve_assistant_static_ip",
            return_value={**state, "created": True},
        ),
        patch.object(views, "get_assistant_static_ip", return_value=state),
        patch.object(
            views,
            "release_assistant_static_ip",
            return_value={"released": True, "dns_deleted": True},
        ),
    ):
        reconcile = client.post(
            "/infra/vm/assistant-static-ip/reconcile",
            json={"assistant_id": "assistant-123"},
        )
        read = client.get("/infra/vm/assistant-static-ip/assistant-123")
        release = client.delete(
            "/infra/vm/assistant-static-ip/assistant-123",
            params={"region": "us-central1"},
        )

    assert reconcile.status_code == 200
    assert reconcile.json()["created"] is True
    assert read.status_code == 200
    assert release.json() == {
        "assistant_id": "assistant-123",
        "name": vm_helpers.assistant_static_ip_name("assistant-123"),
        "released": True,
        "dns_deleted": True,
    }


def test_rotate_assistant_static_ip_uses_operation_candidate_and_verifies_nat():
    candidate = {
        "name": vm_helpers.assistant_static_ip_rotation_name("assistant-123", "op-1"),
        "address": "34.1.2.4",
        "status": "RESERVED",
        "region": "regions/us-central1",
        "labels": vm_helpers._assistant_rotation_labels("assistant-123", "op-1"),
    }
    instance_client = MagicMock()
    replacement = MagicMock()
    dns_upsert = MagicMock()

    with (
        patch.object(
            vm_helpers,
            "_acquire_binding_vm_lease",
            return_value=(MagicMock(), "default", "holder"),
        ),
        patch.object(vm_helpers, "_release_binding_vm_lease"),
        patch.object(
            vm_helpers,
            "reserve_assistant_static_ip_rotation_candidate",
            return_value=candidate,
        ),
        patch.object(
            vm_helpers,
            "_rotation_vm_state",
            return_value=(instance_client, "nic0", "External NAT", "34.1.2.3"),
        ),
        patch.object(vm_helpers, "_replace_vm_external_ip", replacement),
        patch.object(vm_helpers, "_verify_rotation_nat"),
        patch.object(vm_helpers, "_upsert_dns_a_record", dns_upsert),
    ):
        result = vm_helpers.rotate_assistant_static_ip(
            assistant_id="assistant-123",
            operation_id="op-1",
            vm_name="pool-1",
            binding_id="binding-1",
            expected_old_ip="34.1.2.3",
        )

    assert result["candidate"]["name"] == candidate["name"]
    assert result["idempotent"] is False
    assert replacement.call_args.kwargs["replacement_ip"] == "34.1.2.4"
    dns_upsert.assert_called_once_with(
        vm_helpers.get_dns_hostname("assistant-123"),
        "34.1.2.4",
    )


def test_rotate_assistant_static_ip_rejects_stale_expected_old_ip():
    candidate = {"address": "34.1.2.4"}
    with (
        patch.object(
            vm_helpers,
            "_acquire_binding_vm_lease",
            return_value=(MagicMock(), "default", "holder"),
        ),
        patch.object(vm_helpers, "_release_binding_vm_lease"),
        patch.object(
            vm_helpers,
            "reserve_assistant_static_ip_rotation_candidate",
            return_value=candidate,
        ),
        patch.object(
            vm_helpers,
            "_rotation_vm_state",
            return_value=(MagicMock(), "nic0", "External NAT", "34.1.2.9"),
        ),
    ):
        try:
            vm_helpers.rotate_assistant_static_ip(
                assistant_id="assistant-123",
                operation_id="op-1",
                vm_name="pool-1",
                binding_id="binding-1",
                expected_old_ip="34.1.2.3",
            )
        except ValueError as exc:
            assert "not expected old IP" in str(exc)
        else:
            raise AssertionError("stale expected_old_ip was accepted")


def test_finalize_rotation_deletes_only_unattached_inactive_candidate():
    candidate = {
        "name": vm_helpers.assistant_static_ip_rotation_name("assistant-123", "op-1"),
        "address": "34.1.2.3",
        "status": "RESERVED",
        "region": "regions/us-central1",
        "labels": {},
        "users": [],
    }
    address_client = MagicMock()
    with (
        patch.object(
            vm_helpers,
            "_acquire_binding_vm_lease",
            return_value=(MagicMock(), "default", "holder"),
        ),
        patch.object(vm_helpers, "_release_binding_vm_lease"),
        patch.object(
            vm_helpers,
            "_get_assistant_rotation_address",
            return_value=candidate,
        ),
        patch.object(
            vm_helpers,
            "_rotation_vm_state",
            return_value=(MagicMock(), "nic0", "External NAT", "34.1.2.4"),
        ),
        patch.object(
            vm_helpers.compute_v1,
            "AddressesClient",
            return_value=address_client,
        ),
    ):
        result = vm_helpers.finalize_assistant_static_ip_rotation(
            assistant_id="assistant-123",
            operation_id="op-1",
            vm_name="pool-1",
            binding_id="binding-1",
            expected_old_ip="34.1.2.3",
        )

    assert result["deleted"] is True
    address_client.delete.assert_called_once_with(
        project=vm_helpers.SETTINGS.vm_project_id,
        region=vm_helpers.SETTINGS.vm_region,
        address=candidate["name"],
    )


def test_prepare_cross_region_migration_copies_disk_and_reserves_target_ip():
    assistant_id = "assistant-123"
    migration_id = "move-eu"
    source = VmPlacement(
        location=get_pool_location("us-central1"),
        zone="us-central1-a",
        source_timezone="",
        resolution="test",
    )
    target = VmPlacement(
        location=get_pool_location("europe-west1"),
        zone="europe-west1-b",
        source_timezone="",
        resolution="test",
    )
    source_disk = SimpleNamespace(
        self_link="projects/project/zones/us-central1-a/disks/unity-disk-assistant-123",
    )
    snapshot = SimpleNamespace(
        name=vm_helpers.assistant_migration_snapshot_name(assistant_id, migration_id),
        self_link="projects/project/global/snapshots/migration",
        source_disk=source_disk.self_link,
        status="READY",
        labels=vm_helpers._assistant_migration_labels(assistant_id, migration_id),
    )
    target_disk = SimpleNamespace(
        self_link="projects/project/zones/europe-west1-b/disks/target",
        source_snapshot=snapshot.self_link,
        labels=vm_helpers._assistant_migration_labels(assistant_id, migration_id),
    )
    disk_client = MagicMock()
    disk_client.get.side_effect = [source_disk, NotFound("missing"), target_disk]
    snapshot_client = MagicMock()
    snapshot_client.get.side_effect = [NotFound("missing"), snapshot]

    with (
        patch.object(vm_helpers.compute_v1, "DisksClient", return_value=disk_client),
        patch.object(
            vm_helpers.compute_v1,
            "SnapshotsClient",
            return_value=snapshot_client,
        ),
        patch.object(
            vm_helpers,
            "reserve_assistant_static_ip",
            return_value={"name": "target-ip", "created": True},
        ) as reserve_address,
    ):
        result = vm_helpers.prepare_assistant_cross_region_migration(
            assistant_id=assistant_id,
            migration_id=migration_id,
            source=source,
            target=target,
        )

    assert result["snapshot"]["name"] == snapshot.name
    assert result["target_disk"]["zone"] == target.zone
    assert result["target_address"]["name"] == "target-ip"
    disk_client.delete.assert_not_called()
    snapshot_resource = snapshot_client.insert.call_args.kwargs["snapshot_resource"]
    assert snapshot_resource.source_disk == source_disk.self_link
    target_resource = disk_client.insert.call_args.kwargs["disk_resource"]
    assert target_resource.source_snapshot == snapshot.self_link
    reserve_address.assert_called_once_with(assistant_id, region=target.region)


def test_prepare_migration_route_requires_explicit_matching_placements():
    from communication.infra import views

    app = FastAPI()
    app.include_router(views.router, prefix="/infra")
    client = TestClient(app)

    response = client.post(
        "/infra/vm/assistant-migration/prepare",
        json={
            "assistant_id": "assistant-123",
            "migration_id": "move-eu",
            "source": {
                "pool_location": "us-central1",
                "region": "europe-west1",
                "zone": "us-central1-a",
            },
            "target": {
                "pool_location": "europe-west1",
                "region": "europe-west1",
                "zone": "europe-west1-b",
            },
        },
    )

    assert response.status_code == 409
    assert "does not match declared region" in response.json()["detail"]
