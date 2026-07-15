from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.api_core.exceptions import NotFound

from communication.infra import vm_helpers


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

    request = client.set_labels.call_args.kwargs[
        "region_set_labels_request_resource"
    ]
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

    with patch.object(vm_helpers.compute_v1, "AddressesClient", return_value=client):
        assert vm_helpers.release_assistant_static_ip(assistant_id) is True
        assert vm_helpers.release_assistant_static_ip(assistant_id) is False

    client.delete.assert_called_once_with(
        project=vm_helpers.SETTINGS.vm_project_id,
        region=vm_helpers.SETTINGS.vm_region,
        address=vm_helpers.assistant_static_ip_name(assistant_id),
    )


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

    with (
        patch.object(vm_helpers, "reserve_assistant_static_ip", return_value={
            "address": "34.0.0.9",
        }),
        patch.object(
            vm_helpers,
            "_wait_for_pool_static_ip",
            return_value="34.0.0.1",
        ),
        patch.object(vm_helpers.compute_v1, "InstancesClient", return_value=client),
        patch.object(vm_helpers, "_upsert_dns_a_record", dns_upsert),
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
        patch.object(views, "release_assistant_static_ip", return_value=True),
    ):
        reconcile = client.post(
            "/infra/vm/assistant-static-ip/reconcile",
            json={"assistant_id": "assistant-123"},
        )
        read = client.get("/infra/vm/assistant-static-ip/assistant-123")
        release = client.delete("/infra/vm/assistant-static-ip/assistant-123")

    assert reconcile.status_code == 200
    assert reconcile.json()["created"] is True
    assert read.status_code == 200
    assert release.json() == {
        "assistant_id": "assistant-123",
        "name": vm_helpers.assistant_static_ip_name("assistant-123"),
        "released": True,
    }
