"""
VM Lifecycle Management Helpers

This module provides functions for managing VMs on GCP (Windows and Ubuntu), including:
- Static IP reservation and release
- DNS A record management
- VM creation, start, stop, and deletion
- Full provisioning and deprovisioning orchestration
- SSH key generation for file sync
"""

from datetime import datetime, timezone
import hashlib
import json
import logging
import random
import re
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Optional, Dict, Any, Tuple

import requests
from google.cloud import compute_v1
from google.cloud import dns
from google.cloud import secretmanager
from google.api_core.exceptions import NotFound, Conflict, PreconditionFailed
from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from common.settings import SETTINGS
from .observability import causal_log_fields
from .gcp_region_catalog import VmPlacement, get_pool_location, list_pool_locations


from .vm_config import (
    DNS_ZONE_NAME,
    DOMAIN_SUFFIX,
    VM_DISK_TYPE,
    VM_NETWORK,
    MAK_KEY,
    SSH_SYNC_PORT,
    VM_WILDCARD_CERT_SECRET,
    VM_WILDCARD_KEY_SECRET,
    WINDOWS_VM_MACHINE_TYPE,
    WINDOWS_VM_DISK_SIZE_GB,
    WINDOWS_VM_IMAGE_PROJECT,
    WINDOWS_VM_TAGS,
    WINDOWS_INIT_SCRIPT_PATH,
    WINDOWS_POOL_WATCHER_PATH,
    UBUNTU_VM_MACHINE_TYPE,
    UBUNTU_VM_DISK_SIZE_GB,
    UBUNTU_VM_IMAGE_PROJECT,
    UBUNTU_VM_TAGS,
    UBUNTU_INIT_SCRIPT_PATH,
    UBUNTU_POOL_WATCHER_PATH,
    UBUNTU_SUPERVISORD_CONF_PATH,
    POOL_SSH_USERNAME,
    POOL_TARGET_IDLE,
    POOL_TARGET_STOPPED,
    POOL_BOOT_TIMEOUT_SECONDS,
    POOL_RELEASE_TIMEOUT_SECONDS,
    POOL_VM_CONTRACT_GENERATION,
    POOL_ASSISTANT_DISK_SIZE_GB,
    POOL_ASSISTANT_DISK_TYPE,
    pool_vm_name_prefix,
    pool_vm_name_prefixes,
    POOL_RETIRED_ENV_SUFFIXES,
    POOL_ASSISTANT_ARCHIVE_BUCKET,
    POOL_ASSISTANT_DISK_IDLE_HOURS,
    POOL_ASSISTANT_DISK_HARD_CAP_HOURS,
    POOL_ASSISTANT_DISK_ARCHIVE_FRESHNESS_SKEW_SECONDS,
    POOL_UBUNTU_VM_IMAGE_FAMILY,
    POOL_WINDOWS_VM_IMAGE_FAMILY,
    POOL_GOVERNANCE_LABELS,
)

logger = logging.getLogger(__name__)

_ACTIVE_VM_PLACEMENT: ContextVar[VmPlacement | None] = ContextVar(
    "active_vm_placement",
    default=None,
)


def _legacy_vm_placement() -> VmPlacement:
    """Return the existing pool as a placement for old callers and bindings."""
    location = get_pool_location(SETTINGS.vm_region)
    return VmPlacement(
        location=location,
        zone=SETTINGS.vm_zone,
        source_timezone="",
        resolution="legacy_default",
    )


def _current_vm_placement() -> VmPlacement:
    return _ACTIVE_VM_PLACEMENT.get() or _legacy_vm_placement()


@contextmanager
def vm_placement_scope(placement: VmPlacement | None):
    """Route a synchronous VM lifecycle operation to one explicit location."""
    token = _ACTIVE_VM_PLACEMENT.set(placement)
    try:
        yield
    finally:
        _ACTIVE_VM_PLACEMENT.reset(token)


def _run_in_vm_placement(
    placement: VmPlacement,
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run a worker-thread callback with its caller's placement context.

    ``ContextVar`` values do not propagate into ``ThreadPoolExecutor`` worker
    threads. Pool replenish uses those workers for VM creation, so carrying
    this scope explicitly prevents a regional demand from provisioning into
    the legacy Iowa pool.
    """
    with vm_placement_scope(placement):
        return fn(*args, **kwargs)


def _placement_for_vm_name(vm_name: str) -> VmPlacement | None:
    """Find a VM's configured pool location for callback-only lifecycle paths."""
    client = compute_v1.InstancesClient()
    for location_id, zone in SETTINGS.vm_provisioned_locations.items():
        location = get_pool_location(location_id)
        try:
            client.get(
                project=SETTINGS.vm_project_id,
                zone=zone,
                instance=vm_name,
            )
        except NotFound:
            continue
        return VmPlacement(
            location=location,
            zone=zone,
            source_timezone="",
            resolution="discovered",
        )
    return None


POOL_ROLE_LABEL = "pool-role"
ASSISTANT_ID_LABEL = "assistant-id"
BINDING_ID_LABEL = "binding-id"
POOL_ROLE_RELEASING = "releasing"
POOL_CONTRACT_GENERATION_LABEL = "pool-contract-generation"
POOL_TRANSITION_EPOCH_LABEL = "pool-transition-epoch"
POOL_PROGRESS_PHASE_LABEL = "pool-progress-phase"
POOL_PROGRESS_EPOCH_LABEL = "pool-progress-epoch"
RELEASE_TRIGGER_METADATA_KEYS = ("unify-key", "vnc-password", "ssh-public-key")
RELEASE_GENERATION_METADATA_KEY = "release-generation"
MAX_RELEASE_GENERATION = 2
POOL_STATIC_IP_READY_TIMEOUT_SECONDS = 15.0
POOL_STATIC_IP_READY_POLL_INTERVAL_SECONDS = 0.5
POOL_STATIC_IP_DELETE_MAX_ATTEMPTS = 5
POOL_STATIC_IP_DELETE_RETRY_SECONDS = 1.0
POOL_ORPHANED_NETWORK_RESOURCE_GRACE_SECONDS = 600.0
REGIONAL_POOL_REAPER_CONFIG_MAP = "unity-regional-pool-reaper"
REGIONAL_POOL_REAPER_GRACE_SECONDS = 60 * 60
REGIONAL_POOL_REAPER_REAPABLE_ROLES = frozenset({"idle", "stopped", "quarantined"})
# Keep freshly-idle VMs claimable across process boundaries. With
# POOL_TARGET_IDLE=0, replenish boots a VM for demand in one process while
# trim in another (pool controller) would otherwise stop it the moment it
# becomes idle — before the assign poll can claim it.
POOL_IDLE_TRIM_GRACE_SECONDS = float(POOL_BOOT_TIMEOUT_SECONDS)
RECYCLEABLE_STALE_POOL_ROLES = frozenset(
    {
        "idle",
        "stopped",
        "starting",
        "provisioning",
        POOL_ROLE_RELEASING,
        "quarantined",
    },
)
# Roles a stuck-release retire may destroy. "quarantined" belongs here because
# the health scrubber reaches a VM whose guest never finished releasing before
# the session's own recovery does, and quarantining clears "assistant-id" while
# leaving "binding-id" intact. Excluding that role stranded the session: retire
# skipped as vm_not_owned on every reconcile tick, the disk stayed attached, and
# the session never left Releasing.
RETIRABLE_RELEASE_ROLES = frozenset({"assigned", POOL_ROLE_RELEASING, "quarantined"})
INFLIGHT_ROLE_TIMEOUT_SECONDS = {
    "provisioning": POOL_BOOT_TIMEOUT_SECONDS,
    "booting": POOL_BOOT_TIMEOUT_SECONDS,
    "starting": POOL_BOOT_TIMEOUT_SECONDS,
    POOL_ROLE_RELEASING: POOL_RELEASE_TIMEOUT_SECONDS,
}
VM_BINDING_LEASE_DURATION_SECONDS = SETTINGS.lease_duration_seconds
VM_BINDING_RELEASE_LEASE_WAIT_SECONDS = 30.0
VM_BINDING_LEASE_POLL_INTERVAL_SECONDS = 1.0
ASSISTANT_STATIC_IP_MANAGED_BY_LABEL = "managed-by"
ASSISTANT_STATIC_IP_OWNER_LABEL = "resource-owner"
ASSISTANT_STATIC_IP_ASSISTANT_LABEL = "assistant-id"
ASSISTANT_STATIC_IP_MANAGED_BY_VALUE = "communication"
ASSISTANT_STATIC_IP_OWNER_VALUE = "assistant"
ASSISTANT_STATIC_IP_ROTATION_OPERATION_LABEL = "rotation-operation"
ORPHANED_ASSISTANT_DNS_MAX_DELETIONS = 200
ORPHANED_ASSISTANT_DNS_DELETE_CHUNK = 25
ASSISTANT_RESOURCE_ENV_SUFFIXES = ("-staging", "-preview")
# Anchored so a production pass (empty env suffix) cannot match a ``-staging``
# record, and so pool hostnames never parse as assistant hostnames.
ASSISTANT_DNS_RECORD_PATTERN = re.compile(
    r"^unity-assistant-(?P<id>[a-z0-9-]+?)"
    rf"(?P<suffix>{'|'.join(ASSISTANT_RESOURCE_ENV_SUFFIXES)})?"
    rf"\.{re.escape(DOMAIN_SUFFIX)}\.$",
)


class AssistantDiskInUseError(RuntimeError):
    """Permanent disk deletion was requested before the disk finished detaching."""


def _assistant_static_ip_id_component(assistant_id: str, *, max_length: int) -> str:
    """Return a stable GCP-resource-safe component for an assistant ID."""

    normalized = re.sub(r"[^a-z0-9-]+", "-", str(assistant_id).lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-") or "unknown"
    if len(normalized) <= max_length:
        return normalized

    digest = hashlib.sha256(str(assistant_id).encode()).hexdigest()[:8]
    return f"{normalized[: max_length - len(digest) - 1].rstrip('-')}-{digest}"


def assistant_static_ip_name(assistant_id: str) -> str:
    """Return the deterministic regional static-IP name for an assistant."""

    prefix = "unity-assistant-ip-"
    suffix = SETTINGS.env_suffix
    component = _assistant_static_ip_id_component(
        assistant_id,
        max_length=63 - len(prefix) - len(suffix),
    )
    return f"{prefix}{component}{suffix}"


def assistant_static_ip_labels(assistant_id: str) -> dict[str, str]:
    """Return ownership labels for an assistant-managed regional address."""

    return {
        ASSISTANT_STATIC_IP_MANAGED_BY_LABEL: ASSISTANT_STATIC_IP_MANAGED_BY_VALUE,
        ASSISTANT_STATIC_IP_OWNER_LABEL: ASSISTANT_STATIC_IP_OWNER_VALUE,
        ASSISTANT_STATIC_IP_ASSISTANT_LABEL: _assistant_static_ip_id_component(
            assistant_id,
            max_length=63,
        ),
        "environment": (
            "staging" if SETTINGS.deploy_env == "staging" else "production"
        ),
    }


def _assistant_static_ip_details(
    address: Any,
    *,
    assistant_id: str | None = None,
) -> dict[str, Any]:
    """Serialize the stable public details of a GCP regional address."""

    return {
        "name": str(getattr(address, "name", "") or ""),
        "address": str(getattr(address, "address", "") or "") or None,
        "status": str(getattr(address, "status", "") or "") or None,
        "region": str(getattr(address, "region", "") or "") or None,
        "hostname": get_dns_hostname(assistant_id) if assistant_id else None,
        "labels": dict(getattr(address, "labels", None) or {}),
        "users": list(getattr(address, "users", None) or []),
    }


def _assert_assistant_static_ip_ownership(address: Any, assistant_id: str) -> None:
    expected_labels = assistant_static_ip_labels(assistant_id)
    actual_labels = dict(getattr(address, "labels", None) or {})
    ownership_keys = (
        ASSISTANT_STATIC_IP_MANAGED_BY_LABEL,
        ASSISTANT_STATIC_IP_OWNER_LABEL,
        ASSISTANT_STATIC_IP_ASSISTANT_LABEL,
    )
    if any(actual_labels.get(key) != expected_labels[key] for key in ownership_keys):
        raise ValueError(
            f"Regional address {assistant_static_ip_name(assistant_id)} exists but "
            "is not owned by the requested assistant",
        )


def get_assistant_static_ip(
    assistant_id: str,
    *,
    region: str | None = None,
) -> dict[str, Any] | None:
    """Read an assistant-owned regional external address, if it exists."""

    address_name = assistant_static_ip_name(assistant_id)
    region = region or _current_vm_placement().region
    client = compute_v1.AddressesClient()
    try:
        address = client.get(
            project=SETTINGS.vm_project_id,
            region=region,
            address=address_name,
        )
    except NotFound:
        return None

    _assert_assistant_static_ip_ownership(address, assistant_id)
    return _assistant_static_ip_details(address, assistant_id=assistant_id)


def reclaim_stale_assistant_ip_owners(assistant_id: str) -> list[str]:
    """Retire quarantined VMs that still hold an assistant-owned address."""

    allocation = get_assistant_static_ip(assistant_id)
    retired: list[str] = []
    client = compute_v1.InstancesClient()
    for user in allocation.get("users", []) if allocation else []:
        if "/instances/" not in user:
            continue
        vm_name = user.rsplit("/", 1)[-1]
        vm = client.get(
            project=SETTINGS.vm_project_id,
            zone=_current_vm_placement().zone,
            instance=vm_name,
        )
        role = str((vm.labels or {}).get(POOL_ROLE_LABEL, "") or "")
        if role != "quarantined":
            raise AssistantDiskInUseError(
                f"Assistant address is still attached to {vm_name} pool_role={role}",
            )
        _delete_pool_vm_instance(
            client,
            vm_name,
            vm_type=str((vm.labels or {}).get("vm-type", "ubuntu")),
        )
        retired.append(vm_name)
    return retired


def reserve_assistant_static_ip(
    assistant_id: str,
    *,
    region: str | None = None,
) -> dict[str, Any]:
    """Idempotently reserve the assistant's regional external address.

    This deliberately reserves an address only. Attaching it to an instance is
    owned by a separate VM lifecycle operation. GCP can accept labels on an
    insert request yet return the new address without them, so a successful
    create explicitly repairs its labels before ownership validation.
    """

    region = region or _current_vm_placement().region
    existing = get_assistant_static_ip(assistant_id, region=region)
    if existing is not None:
        return {**existing, "created": False}

    address_name = assistant_static_ip_name(assistant_id)
    client = compute_v1.AddressesClient()
    created = False
    try:
        client.insert(
            project=SETTINGS.vm_project_id,
            region=region,
            address_resource=compute_v1.Address(
                name=address_name,
                address_type="EXTERNAL",
                network_tier="PREMIUM",
                description=f"Assistant-owned static IP for {assistant_id}",
                labels=assistant_static_ip_labels(assistant_id),
            ),
        ).result()
        created = True
    except Conflict:
        # Another reconciler won the create race. Re-read and validate owner.
        pass

    if created:
        created_address = client.get(
            project=SETTINGS.vm_project_id,
            region=region,
            address=address_name,
        )
        expected_labels = assistant_static_ip_labels(assistant_id)
        actual_labels = dict(getattr(created_address, "labels", None) or {})
        if actual_labels != expected_labels:
            client.set_labels(
                project=SETTINGS.vm_project_id,
                region=region,
                resource=address_name,
                region_set_labels_request_resource=compute_v1.RegionSetLabelsRequest(
                    labels=expected_labels,
                    label_fingerprint=getattr(
                        created_address,
                        "label_fingerprint",
                        None,
                    ),
                ),
            ).result()

    reserved = get_assistant_static_ip(assistant_id, region=region)
    if reserved is None:
        raise RuntimeError(f"Reserved address {address_name} could not be read")
    return {**reserved, "created": created}


def release_assistant_static_ip(
    assistant_id: str,
    *,
    region: str | None = None,
) -> dict[str, bool]:
    """Idempotently release an assistant-owned address and its DNS record.

    An assistant's A record has exactly the lifetime of the address it names,
    so release deletes both. The record goes first: it has no owner of its own,
    and dropping the address ahead of it strands the record behind the missing
    address short-circuit on every later retry. Releasing an assistant whose
    address is already gone therefore still reaps a record left by an earlier
    partial release.
    """

    address_name = assistant_static_ip_name(assistant_id)
    hostname = get_dns_hostname(assistant_id)
    region = region or _current_vm_placement().region
    client = compute_v1.AddressesClient()
    try:
        address = client.get(
            project=SETTINGS.vm_project_id,
            region=region,
            address=address_name,
        )
    except NotFound:
        return {"released": False, "dns_deleted": _delete_dns_a_record(hostname)}

    _assert_assistant_static_ip_ownership(address, assistant_id)
    dns_deleted = _delete_dns_a_record(hostname)
    try:
        client.delete(
            project=SETTINGS.vm_project_id,
            region=region,
            address=address_name,
        ).result()
    except NotFound:
        return {"released": False, "dns_deleted": dns_deleted}
    return {"released": True, "dns_deleted": dns_deleted}


def assistant_migration_snapshot_name(
    assistant_id: str,
    migration_id: str,
) -> str:
    """Return an operation-scoped global snapshot name safe for GCE."""

    prefix = "unity-migration-"
    suffix = SETTINGS.env_suffix
    assistant = _assistant_static_ip_id_component(assistant_id, max_length=63)
    migration = _assistant_static_ip_id_component(migration_id, max_length=63)
    digest = hashlib.sha256(
        f"{assistant_id}:{migration_id}".encode(),
    ).hexdigest()[:8]
    available = 63 - len(prefix) - len(suffix) - len(digest) - 2
    assistant_length = max(1, available // 2)
    migration_length = max(1, available - assistant_length)
    return (
        f"{prefix}{assistant[:assistant_length].rstrip('-')}-"
        f"{migration[:migration_length].rstrip('-')}-{digest}{suffix}"
    )


def _assistant_disk_name_at_placement(
    assistant_id: str,
    placement: VmPlacement,
) -> str:
    with vm_placement_scope(placement):
        return _assistant_disk_name(assistant_id)


def _migration_snapshot_details(snapshot: Any) -> dict[str, Any]:
    return {
        "name": str(getattr(snapshot, "name", "") or ""),
        "self_link": str(getattr(snapshot, "self_link", "") or "") or None,
        "status": str(getattr(snapshot, "status", "") or "") or None,
        "source_disk": str(getattr(snapshot, "source_disk", "") or "") or None,
    }


def _assistant_migration_labels(
    assistant_id: str,
    migration_id: str,
) -> dict[str, str]:
    return {
        "managed-by": ASSISTANT_STATIC_IP_MANAGED_BY_VALUE,
        "resource-owner": ASSISTANT_STATIC_IP_OWNER_VALUE,
        "assistant-id": _assistant_static_ip_id_component(assistant_id, max_length=63),
        "migration-id": _assistant_static_ip_id_component(migration_id, max_length=63),
    }


def _assert_assistant_migration_ownership(
    resource: Any,
    *,
    assistant_id: str,
    migration_id: str,
    resource_name: str,
) -> None:
    expected = _assistant_migration_labels(assistant_id, migration_id)
    actual = dict(getattr(resource, "labels", None) or {})
    if any(actual.get(key) != value for key, value in expected.items()):
        raise ValueError(
            f"Migration resource {resource_name} is not owned by the requested migration",
        )


def prepare_assistant_cross_region_migration(
    *,
    assistant_id: str,
    migration_id: str,
    source: VmPlacement,
    target: VmPlacement,
) -> dict[str, Any]:
    """Copy an assistant disk to an explicit target zone and reserve its IP.

    This is preparation only: it never detaches, changes, or deletes source
    resources. Repeating the same migration ID reuses its immutable snapshot
    and target disk after validating their source linkage.
    """

    if source.region == target.region:
        raise ValueError("Source and target placements must use different regions")

    source_disk_name = _assistant_disk_name_at_placement(assistant_id, source)
    target_disk_name = _assistant_disk_name_at_placement(assistant_id, target)
    disks = compute_v1.DisksClient()
    try:
        source_disk = disks.get(
            project=SETTINGS.vm_project_id,
            zone=source.zone,
            disk=source_disk_name,
        )
    except NotFound as exc:
        raise ValueError(
            f"Source assistant disk {source_disk_name} was not found in {source.zone}",
        ) from exc

    source_disk_link = str(getattr(source_disk, "self_link", "") or "")
    if not source_disk_link:
        raise RuntimeError(f"Source assistant disk {source_disk_name} has no self link")

    snapshot_name = assistant_migration_snapshot_name(assistant_id, migration_id)
    migration_labels = _assistant_migration_labels(assistant_id, migration_id)
    snapshots = compute_v1.SnapshotsClient()
    try:
        snapshot = snapshots.get(
            project=SETTINGS.vm_project_id,
            snapshot=snapshot_name,
        )
    except NotFound:
        try:
            snapshots.insert(
                project=SETTINGS.vm_project_id,
                snapshot_resource=compute_v1.Snapshot(
                    name=snapshot_name,
                    source_disk=source_disk_link,
                    description=(
                        f"Migration {migration_id} snapshot for assistant {assistant_id}"
                    ),
                    labels=migration_labels,
                ),
            ).result()
        except Conflict:
            pass
        snapshot = snapshots.get(
            project=SETTINGS.vm_project_id,
            snapshot=snapshot_name,
        )

    if str(getattr(snapshot, "source_disk", "") or "") != source_disk_link:
        raise ValueError(
            f"Migration snapshot {snapshot_name} does not belong to the requested source disk",
        )
    _assert_assistant_migration_ownership(
        snapshot,
        assistant_id=assistant_id,
        migration_id=migration_id,
        resource_name=snapshot_name,
    )

    snapshot_link = str(getattr(snapshot, "self_link", "") or "")
    if not snapshot_link:
        raise RuntimeError(f"Migration snapshot {snapshot_name} has no self link")

    target_created = False
    try:
        target_disk = disks.get(
            project=SETTINGS.vm_project_id,
            zone=target.zone,
            disk=target_disk_name,
        )
    except NotFound:
        try:
            disks.insert(
                project=SETTINGS.vm_project_id,
                zone=target.zone,
                disk_resource=compute_v1.Disk(
                    name=target_disk_name,
                    source_snapshot=snapshot_link,
                    type_=(f"zones/{target.zone}/diskTypes/{POOL_ASSISTANT_DISK_TYPE}"),
                    description=(
                        f"Migration {migration_id} copy for assistant {assistant_id}"
                    ),
                    labels=migration_labels,
                ),
            ).result()
            target_created = True
        except Conflict:
            pass
        target_disk = disks.get(
            project=SETTINGS.vm_project_id,
            zone=target.zone,
            disk=target_disk_name,
        )

    _assert_assistant_migration_ownership(
        target_disk,
        assistant_id=assistant_id,
        migration_id=migration_id,
        resource_name=target_disk_name,
    )
    target_source_snapshot = str(getattr(target_disk, "source_snapshot", "") or "")
    if target_source_snapshot and target_source_snapshot != snapshot_link:
        raise ValueError(
            f"Target disk {target_disk_name} does not belong to migration {migration_id}",
        )

    target_address = reserve_assistant_static_ip(assistant_id, region=target.region)
    _log_vm_pool_event(
        "prepare_cross_region_migration",
        assistant_id=assistant_id,
        migration_id=migration_id,
        source_zone=source.zone,
        target_zone=target.zone,
        source_disk=source_disk_name,
        target_disk=target_disk_name,
        snapshot=snapshot_name,
        target_disk_created=target_created,
        target_address_created=target_address.get("created", False),
    )
    return {
        "snapshot": _migration_snapshot_details(snapshot),
        "target_disk": {
            "name": target_disk_name,
            "self_link": str(getattr(target_disk, "self_link", "") or "") or None,
            "zone": target.zone,
            "created": target_created,
        },
        "target_address": target_address,
    }


def assistant_static_ip_rotation_name(assistant_id: str, operation_id: str) -> str:
    """Return a deterministic, operation-unique address name for a rotation."""

    prefix = "unity-assistant-ip-"
    suffix = SETTINGS.env_suffix
    operation_normalized = _assistant_static_ip_id_component(
        operation_id,
        max_length=63,
    )
    operation_digest = hashlib.sha256(str(operation_id).encode()).hexdigest()[:8]
    operation_component = (
        f"{operation_normalized[:7].rstrip('-') or 'op'}-{operation_digest}"
    )
    assistant_component = _assistant_static_ip_id_component(
        assistant_id,
        max_length=63 - len(prefix) - len(suffix) - len(operation_component) - 1,
    )
    return f"{prefix}{assistant_component}-{operation_component}{suffix}"


def _assistant_rotation_labels(
    assistant_id: str,
    operation_id: str,
) -> dict[str, str]:
    return {
        **assistant_static_ip_labels(assistant_id),
        ASSISTANT_STATIC_IP_ROTATION_OPERATION_LABEL: _assistant_static_ip_id_component(
            operation_id,
            max_length=63,
        ),
    }


def _get_assistant_rotation_address(
    assistant_id: str,
    operation_id: str,
) -> dict[str, Any] | None:
    name = assistant_static_ip_rotation_name(assistant_id, operation_id)
    client = compute_v1.AddressesClient()
    try:
        address = client.get(
            project=SETTINGS.vm_project_id,
            region=_current_vm_placement().region,
            address=name,
        )
    except NotFound:
        return None
    _assert_assistant_static_ip_ownership(address, assistant_id)
    labels = dict(getattr(address, "labels", None) or {})
    expected_operation = _assistant_rotation_labels(assistant_id, operation_id)[
        ASSISTANT_STATIC_IP_ROTATION_OPERATION_LABEL
    ]
    if labels.get(ASSISTANT_STATIC_IP_ROTATION_OPERATION_LABEL) != expected_operation:
        raise ValueError(f"Regional address {name} is not owned by this operation")
    return {
        **_assistant_static_ip_details(address),
        "users": list(getattr(address, "users", None) or []),
    }


def reserve_assistant_static_ip_rotation_candidate(
    assistant_id: str,
    operation_id: str,
) -> dict[str, Any]:
    """Idempotently reserve the operation-specific candidate address."""

    existing = _get_assistant_rotation_address(assistant_id, operation_id)
    if existing is not None:
        return {**existing, "created": False}

    name = assistant_static_ip_rotation_name(assistant_id, operation_id)
    client = compute_v1.AddressesClient()
    created = False
    try:
        client.insert(
            project=SETTINGS.vm_project_id,
            region=_current_vm_placement().region,
            address_resource=compute_v1.Address(
                name=name,
                address_type="EXTERNAL",
                network_tier="PREMIUM",
                description=(
                    f"Rotation candidate for assistant {assistant_id}, "
                    f"operation {operation_id}"
                ),
                labels=_assistant_rotation_labels(assistant_id, operation_id),
            ),
        ).result()
        created = True
    except Conflict:
        pass

    if created:
        created_address = client.get(
            project=SETTINGS.vm_project_id,
            region=_current_vm_placement().region,
            address=name,
        )
        expected_labels = _assistant_rotation_labels(assistant_id, operation_id)
        if dict(getattr(created_address, "labels", None) or {}) != expected_labels:
            client.set_labels(
                project=SETTINGS.vm_project_id,
                region=_current_vm_placement().region,
                resource=name,
                region_set_labels_request_resource=compute_v1.RegionSetLabelsRequest(
                    labels=expected_labels,
                    label_fingerprint=getattr(
                        created_address,
                        "label_fingerprint",
                        None,
                    ),
                ),
            ).result()
    candidate = _get_assistant_rotation_address(assistant_id, operation_id)
    if candidate is None or not candidate.get("address"):
        raise RuntimeError(f"Rotation candidate {name} could not be read")
    return {**candidate, "created": created}


def _rotation_vm_state(
    vm_name: str,
    binding_id: str,
    assistant_id: str,
) -> tuple[Any, str, str, str]:
    """Read and validate the exact assigned VM before a network mutation."""

    client = compute_v1.InstancesClient()
    instance = client.get(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        instance=vm_name,
    )
    labels = dict(getattr(instance, "labels", None) or {})
    if labels.get(POOL_ROLE_LABEL) != "assigned":
        raise ValueError(f"VM {vm_name} is not currently assigned")
    if labels.get(BINDING_ID_LABEL) != binding_id.lower().replace("_", "-"):
        raise ValueError(f"VM {vm_name} is not assigned to binding {binding_id}")
    if labels.get(ASSISTANT_ID_LABEL) != assistant_id.lower().replace("_", "-"):
        raise ValueError(f"VM {vm_name} is not assigned to assistant {assistant_id}")
    network_interface, access_config_name, current_ip = _external_access_config(
        instance,
    )
    return client, network_interface, access_config_name, current_ip


def _verify_rotation_nat(
    client: Any,
    vm_name: str,
    expected_ip: str,
) -> None:
    instance = client.get(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        instance=vm_name,
    )
    _, _, actual_ip = _external_access_config(instance)
    if actual_ip != expected_ip:
        raise RuntimeError(
            f"GCE did not confirm {expected_ip} on {vm_name}; observed {actual_ip}",
        )


def _rotation_response_address(details: dict[str, Any]) -> dict[str, Any]:
    return {
        key: details.get(key)
        for key in ("name", "address", "status", "region", "labels")
    }


def rotate_assistant_static_ip(
    *,
    assistant_id: str,
    operation_id: str,
    vm_name: str,
    binding_id: str,
    expected_old_ip: str,
) -> dict[str, Any]:
    """Atomically swap an assigned binding from its old address to a candidate."""

    coord_api, namespace, holder_id = _acquire_binding_vm_lease(
        binding_id,
        holder_prefix="ip-rotate",
        wait_timeout_seconds=VM_BINDING_RELEASE_LEASE_WAIT_SECONDS,
    )
    if coord_api is None:
        raise RuntimeError(f"VM lifecycle lease is busy for binding {binding_id}")
    hostname = get_dns_hostname(assistant_id)
    try:
        candidate = reserve_assistant_static_ip_rotation_candidate(
            assistant_id,
            operation_id,
        )
        candidate_ip = str(candidate["address"])
        client, nic, access_config, current_ip = _rotation_vm_state(
            vm_name,
            binding_id,
            assistant_id,
        )
        if current_ip == candidate_ip:
            _verify_rotation_nat(client, vm_name, candidate_ip)
            _upsert_dns_a_record(hostname, candidate_ip)
            return {
                "hostname": hostname,
                "old": {"address": expected_old_ip},
                "candidate": _rotation_response_address(candidate),
                "idempotent": True,
            }
        if current_ip != expected_old_ip:
            raise ValueError(
                f"VM {vm_name} has {current_ip}, not expected old IP {expected_old_ip}",
            )
        try:
            _replace_vm_external_ip(
                client,
                vm_name,
                network_interface=nic,
                access_config_name=access_config,
                current_ip=current_ip,
                replacement_ip=candidate_ip,
            )
            _verify_rotation_nat(client, vm_name, candidate_ip)
            _upsert_dns_a_record(hostname, candidate_ip)
        except Exception:
            # A DNS failure happens after NAT replacement. Restore both public
            # observations before surfacing the original operation failure.
            try:
                _, _, _, observed_ip = _rotation_vm_state(
                    vm_name,
                    binding_id,
                    assistant_id,
                )
                if observed_ip != expected_old_ip:
                    _replace_vm_external_ip(
                        client,
                        vm_name,
                        network_interface=nic,
                        access_config_name=access_config,
                        current_ip=observed_ip,
                        replacement_ip=expected_old_ip,
                    )
                _upsert_dns_a_record(hostname, expected_old_ip)
            except Exception:
                logger.exception("Failed restoring NAT/DNS after rotation failure")
            raise
        return {
            "hostname": hostname,
            "old": {"address": expected_old_ip},
            "candidate": _rotation_response_address(candidate),
            "idempotent": False,
        }
    finally:
        _release_binding_vm_lease(coord_api, binding_id, namespace, holder_id)


def rollback_assistant_static_ip_rotation(
    *,
    assistant_id: str,
    operation_id: str,
    vm_name: str,
    binding_id: str,
    expected_old_ip: str,
) -> dict[str, Any]:
    """Swap an operation candidate back to the retained old address and DNS."""

    coord_api, namespace, holder_id = _acquire_binding_vm_lease(
        binding_id,
        holder_prefix="ip-rollback",
        wait_timeout_seconds=VM_BINDING_RELEASE_LEASE_WAIT_SECONDS,
    )
    if coord_api is None:
        raise RuntimeError(f"VM lifecycle lease is busy for binding {binding_id}")
    hostname = get_dns_hostname(assistant_id)
    try:
        candidate = _get_assistant_rotation_address(assistant_id, operation_id)
        if candidate is None or not candidate.get("address"):
            raise ValueError("Rotation candidate does not exist")
        candidate_ip = str(candidate["address"])
        client, nic, access_config, current_ip = _rotation_vm_state(
            vm_name,
            binding_id,
            assistant_id,
        )
        if current_ip not in (candidate_ip, expected_old_ip):
            raise ValueError(f"VM {vm_name} has unexpected external IP {current_ip}")
        if current_ip == expected_old_ip:
            _upsert_dns_a_record(hostname, expected_old_ip)
            return {
                "hostname": hostname,
                "old": {"address": expected_old_ip},
                "candidate": _rotation_response_address(candidate),
                "idempotent": True,
            }
        try:
            _replace_vm_external_ip(
                client,
                vm_name,
                network_interface=nic,
                access_config_name=access_config,
                current_ip=candidate_ip,
                replacement_ip=expected_old_ip,
            )
            _verify_rotation_nat(client, vm_name, expected_old_ip)
            _upsert_dns_a_record(hostname, expected_old_ip)
        except Exception:
            try:
                _upsert_dns_a_record(hostname, candidate_ip)
            except Exception:
                logger.exception(
                    "Failed restoring candidate DNS after rollback failure",
                )
            raise
        return {
            "hostname": hostname,
            "old": {"address": expected_old_ip},
            "candidate": _rotation_response_address(candidate),
            "idempotent": False,
        }
    finally:
        _release_binding_vm_lease(coord_api, binding_id, namespace, holder_id)


def finalize_assistant_static_ip_rotation(
    *,
    assistant_id: str,
    operation_id: str,
    vm_name: str,
    binding_id: str,
    expected_old_ip: str,
) -> dict[str, Any]:
    """Delete only an unattached operation candidate that is no longer active."""

    # The binding fields are deliberately validated even though finalization
    # only deletes an address: they prevent an old operation from deleting an
    # address while its binding has been reassigned.
    coord_api, namespace, holder_id = _acquire_binding_vm_lease(
        binding_id,
        holder_prefix="ip-finalize",
        wait_timeout_seconds=VM_BINDING_RELEASE_LEASE_WAIT_SECONDS,
    )
    if coord_api is None:
        raise RuntimeError(f"VM lifecycle lease is busy for binding {binding_id}")
    hostname = get_dns_hostname(assistant_id)
    try:
        candidate = _get_assistant_rotation_address(assistant_id, operation_id)
        if candidate is None:
            return {
                "hostname": hostname,
                "old": {"address": expected_old_ip},
                "candidate": {
                    "name": assistant_static_ip_rotation_name(
                        assistant_id,
                        operation_id,
                    ),
                },
                "idempotent": True,
                "deleted": False,
            }
        candidate_ip = str(candidate.get("address") or "")
        if candidate_ip != expected_old_ip:
            raise ValueError("Candidate address does not match expected old IP")
        _, _, _, active_ip = _rotation_vm_state(vm_name, binding_id, assistant_id)
        if active_ip == candidate_ip or candidate.get("users"):
            raise ValueError("Refusing to finalize an active or attached candidate")
        compute_v1.AddressesClient().delete(
            project=SETTINGS.vm_project_id,
            region=_current_vm_placement().region,
            address=str(candidate["name"]),
        ).result()
        return {
            "hostname": hostname,
            "old": {"address": expected_old_ip},
            "candidate": _rotation_response_address(candidate),
            "idempotent": False,
            "deleted": True,
        }
    finally:
        _release_binding_vm_lease(coord_api, binding_id, namespace, holder_id)


def _compact_vm_log_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in fields.items()
        if value not in (None, "", [], {}, ())
    }


def _log_vm_pool_event(event: str, **fields: Any) -> None:
    logger.info(
        "OBS_EVENT %s",
        json.dumps(
            {
                "event": f"vm_pool.{event}",
                **causal_log_fields(),
                **_compact_vm_log_fields(fields),
            },
            sort_keys=True,
            default=str,
        ),
    )


def _run_vm_pool_stage(
    *,
    operation: str,
    stage: str,
    fn: Callable[[], Any],
    **fields: Any,
) -> Any:
    """Emit stage-scoped observability for long-running VM pool operations."""

    started_at = time.monotonic()
    _log_vm_pool_event(
        f"{operation}_stage",
        stage=stage,
        stage_state="started",
        **fields,
    )
    try:
        result = fn()
    except Exception as exc:
        _log_vm_pool_event(
            f"{operation}_stage",
            stage=stage,
            stage_state="failed",
            duration_ms=int((time.monotonic() - started_at) * 1000),
            error_type=type(exc).__name__,
            error=str(exc),
            **fields,
        )
        raise
    _log_vm_pool_event(
        f"{operation}_stage",
        stage=stage,
        stage_state="completed",
        duration_ms=int((time.monotonic() - started_at) * 1000),
        **fields,
    )
    return result


# ---------------------------------------------------------------------------
# Demand tracking for pool replenishment
# ---------------------------------------------------------------------------
PoolScopeKey = tuple[str, str, str]

_pending_claims: Dict[PoolScopeKey, int] = {}
_pending_lock = threading.Lock()

_replenish_locks: Dict[PoolScopeKey, threading.Lock] = {}
_replenish_locks_guard = threading.Lock()

_trim_locks: Dict[PoolScopeKey, threading.Lock] = {}
_trim_locks_guard = threading.Lock()

_vm_claim_locks: Dict[str, threading.Lock] = {}
_vm_claim_locks_guard = threading.Lock()


def _pool_scope_key(vm_type: str) -> PoolScopeKey:
    placement = _current_vm_placement()
    return vm_type, placement.location.id, placement.zone


def _get_replenish_lock(vm_type: str) -> threading.Lock:
    key = _pool_scope_key(vm_type)
    with _replenish_locks_guard:
        if key not in _replenish_locks:
            _replenish_locks[key] = threading.Lock()
        return _replenish_locks[key]


def _get_trim_lock(vm_type: str) -> threading.Lock:
    key = _pool_scope_key(vm_type)
    with _trim_locks_guard:
        if key not in _trim_locks:
            _trim_locks[key] = threading.Lock()
        return _trim_locks[key]


def _get_vm_claim_lock(vm_name: str) -> threading.Lock:
    with _vm_claim_locks_guard:
        if vm_name not in _vm_claim_locks:
            _vm_claim_locks[vm_name] = threading.Lock()
        return _vm_claim_locks[vm_name]


def _parse_gce_timestamp(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _pool_transition_epoch_value(now: Optional[datetime] = None) -> str:
    reference = now or datetime.now(timezone.utc)
    return str(int(reference.timestamp()))


def _pool_progress_epoch_value(now: Optional[datetime] = None) -> str:
    """Return a Unix epoch string for the current VM progress phase."""

    reference = now or datetime.now(timezone.utc)
    return str(int(reference.timestamp()))


def _pool_transition_reference_time(instance) -> Optional[datetime]:
    labels = dict(instance.labels) if instance.labels else {}
    epoch_value = labels.get(POOL_TRANSITION_EPOCH_LABEL)
    if epoch_value:
        try:
            return datetime.fromtimestamp(int(epoch_value), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            logger.warning(
                "Invalid %s label on %s",
                POOL_TRANSITION_EPOCH_LABEL,
                instance.name,
            )
    return None


def _pool_progress_phase(instance) -> str:
    """Return the explicit VM progress phase tracked on the instance."""

    labels = dict(instance.labels) if instance.labels else {}
    return str(labels.get(POOL_PROGRESS_PHASE_LABEL, "") or "")


def _pool_progress_reference_time(instance) -> Optional[datetime]:
    """Return the timestamp for the current progress phase."""

    labels = dict(instance.labels) if instance.labels else {}
    epoch_value = labels.get(POOL_PROGRESS_EPOCH_LABEL)
    if epoch_value:
        try:
            return datetime.fromtimestamp(int(epoch_value), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            logger.warning(
                "Invalid %s label on %s",
                POOL_PROGRESS_EPOCH_LABEL,
                instance.name,
            )
    return None


def _progress_phase_for_role(role: str) -> str:
    """Map the coarse pool role to its default progress phase."""

    if role in {"assigned", "idle", "stopped", "quarantined"}:
        return role
    if role == POOL_ROLE_RELEASING:
        return POOL_ROLE_RELEASING
    if role in {"provisioning", "starting"}:
        return role
    return ""


def _pool_contract_generation(instance) -> str:
    labels = dict(instance.labels) if instance.labels else {}
    return labels.get(POOL_CONTRACT_GENERATION_LABEL, "")


def _has_current_pool_contract(instance) -> bool:
    return _pool_contract_generation(instance) == POOL_VM_CONTRACT_GENERATION


def _instance_boot_reference_time(instance) -> Optional[datetime]:
    return (
        _pool_progress_reference_time(instance)
        or _pool_transition_reference_time(instance)
        or _parse_gce_timestamp(
            getattr(instance, "last_start_timestamp", None)
            or getattr(instance, "creation_timestamp", None),
        )
    )


def _binding_vm_lease_name(binding_id: str) -> str:
    """Return the shared lease key for binding-scoped VM mutations."""

    return f"vm-{binding_id}"


def _acquire_binding_vm_lease(
    binding_id: str,
    *,
    holder_prefix: str,
    wait_timeout_seconds: float = 0.0,
) -> tuple[object | None, str, str | None]:
    """Acquire the shared binding VM lease, optionally waiting for it.

    Assignment and release both mutate VM labels/metadata for the same binding.
    Serializing those writes prevents one path from resurrecting stale metadata
    after the other has already advanced the lifecycle.
    """

    from .helpers import acquire_assignment_lease, setup_kubernetes_client

    _, _, _, coord_api = setup_kubernetes_client()
    namespace = SETTINGS.default_namespace
    holder_id = f"{holder_prefix}-{uuid.uuid4().hex[:8]}"
    deadline = time.monotonic() + max(wait_timeout_seconds, 0.0)
    lease_id = _binding_vm_lease_name(binding_id)

    while True:
        acquired = acquire_assignment_lease(
            coord_api,
            lease_id,
            namespace,
            holder_id,
            duration=VM_BINDING_LEASE_DURATION_SECONDS,
        )
        if acquired:
            return coord_api, namespace, holder_id
        if wait_timeout_seconds <= 0 or time.monotonic() >= deadline:
            return None, namespace, None
        time.sleep(VM_BINDING_LEASE_POLL_INTERVAL_SECONDS)


def _release_binding_vm_lease(
    coord_api,
    binding_id: str,
    namespace: str,
    holder_id: str | None,
) -> None:
    """Release the shared binding VM lease when held."""

    from .helpers import release_assignment_lease

    if coord_api is None or not holder_id:
        return
    release_assignment_lease(
        coord_api,
        _binding_vm_lease_name(binding_id),
        namespace,
        holder_id,
    )


def _stopped_pool_reference_time(instance) -> Optional[datetime]:
    """Return the best timestamp for deciding stopped reserve retention."""

    return _parse_gce_timestamp(getattr(instance, "last_stop_timestamp", None)) or (
        _instance_boot_reference_time(instance)
    )


def _normalize_release_generation(value: object) -> int | None:
    """Return a positive release generation integer, if one was provided."""

    try:
        generation = int(value)
    except (TypeError, ValueError):
        return None
    return generation if generation > 0 else None


def _read_instance_release_generation(instance) -> int | None:
    """Return the current release generation stored in instance metadata."""

    return _normalize_release_generation(
        _read_instance_metadata(instance, RELEASE_GENERATION_METADATA_KEY),
    )


def _release_metadata_updates(
    *,
    clear_assignment: bool,
    release_generation: int | None = None,
) -> Dict[str, str]:
    """Return the metadata keys that must be cleared during VM release."""

    updates = {key: "" for key in RELEASE_TRIGGER_METADATA_KEYS}
    normalized_release_generation = _normalize_release_generation(release_generation)
    updates[RELEASE_GENERATION_METADATA_KEY] = (
        str(normalized_release_generation) if normalized_release_generation else ""
    )
    if clear_assignment:
        updates.update(
            {
                ASSISTANT_ID_LABEL: "",
                BINDING_ID_LABEL: "",
                "disk-device": "",
            },
        )
    return updates


def _metadata_update_actions(updates: Dict[str, str]) -> Dict[str, str]:
    """Return a value-safe summary of metadata writes."""

    return {
        key: ("cleared" if value in ("", None) else "set")
        for key, value in updates.items()
    }


def _release_metadata_still_present(instance) -> bool:
    """Return whether the watcher-triggering release metadata is still present."""
    return any(
        _read_instance_metadata(instance, key) for key in RELEASE_TRIGGER_METADATA_KEYS
    )


def _refresh_releasing_progress_epoch(
    client: compute_v1.InstancesClient,
    vm_name: str,
) -> None:
    """Refresh the inflight release age labels without changing ownership."""

    _set_pool_labels(
        client,
        vm_name,
        {POOL_ROLE_LABEL: POOL_ROLE_RELEASING},
        expected_role=POOL_ROLE_RELEASING,
    )


def _is_stale_inflight_vm(
    instance,
    now: Optional[datetime] = None,
    timeout_seconds: Optional[int] = None,
) -> bool:
    labels = dict(instance.labels) if instance.labels else {}
    progress_phase = _pool_progress_phase(instance) or labels.get(POOL_ROLE_LABEL, "")
    if progress_phase not in INFLIGHT_ROLE_TIMEOUT_SECONDS:
        return False
    reference_time = _pool_progress_reference_time(
        instance,
    ) or _instance_boot_reference_time(
        instance,
    )
    if reference_time is None:
        return False
    now = now or datetime.now(timezone.utc)
    max_age_seconds = (
        timeout_seconds
        if timeout_seconds is not None
        else INFLIGHT_ROLE_TIMEOUT_SECONDS[progress_phase]
    )
    return (now - reference_time).total_seconds() > max_age_seconds


def _quarantine_pool_vm(
    client: compute_v1.InstancesClient,
    instance,
    *,
    reason: str,
) -> Optional[str]:
    labels = dict(instance.labels) if instance.labels else {}
    current_role = labels.get("pool-role", "")
    if current_role == "quarantined":
        return None

    ok = _set_pool_labels(
        client,
        instance.name,
        {
            "pool-role": "quarantined",
            "assistant-id": "",
        },
        expected_role=current_role or None,
    )
    if not ok:
        logger.warning(
            "Quarantine: label CAS failed for %s (expected role %s)",
            instance.name,
            current_role,
        )
        _log_vm_pool_event(
            "quarantine_skipped",
            vm_name=instance.name,
            expected_role=current_role,
            reason=reason,
        )
        return None

    if instance.status in ("RUNNING", "STAGING"):
        _request_vm_stop(
            client,
            instance.name,
            vm_type=labels.get("vm-type", "ubuntu"),
            reason=f"quarantine: {reason}",
        )

    action = f"Quarantined unhealthy VM {instance.name}: {reason}"
    logger.warning(action)
    _log_vm_pool_event(
        "quarantine",
        vm_name=instance.name,
        status=instance.status,
        reason=reason,
    )
    return action


def _request_vm_stop(
    client: compute_v1.InstancesClient,
    vm_name: str,
    *,
    vm_type: str,
    reason: str,
) -> None:
    """Submit a best-effort GCE stop request without waiting for completion."""
    try:
        op = client.stop(
            project=SETTINGS.vm_project_id,
            zone=_current_vm_placement().zone,
            instance=vm_name,
        )
        logger.info(
            "Stop request submitted for pool VM %s (%s): %s",
            vm_name,
            vm_type,
            reason,
        )
        _log_vm_pool_event(
            "stop_requested",
            vm_name=vm_name,
            vm_type=vm_type,
            reason=reason,
            operation_name=getattr(op, "name", None),
        )
    except Exception as exc:
        logger.error("Failed to submit stop request for %s: %s", vm_name, exc)


def _refresh_inflight_progress_phase(client: compute_v1.InstancesClient, instance):
    """Advance provisioning/starting VMs into booting once GCE reports RUNNING."""

    role = (dict(instance.labels) if instance.labels else {}).get(POOL_ROLE_LABEL, "")
    progress_phase = _pool_progress_phase(instance)
    if role not in {"provisioning", "starting"}:
        return instance
    if instance.status != "RUNNING":
        return instance
    if progress_phase == "booting":
        return instance

    updated = _set_pool_labels(
        client,
        instance.name,
        {
            POOL_PROGRESS_PHASE_LABEL: "booting",
            POOL_PROGRESS_EPOCH_LABEL: _pool_progress_epoch_value(),
        },
        expected_role=role,
    )
    if not updated:
        return client.get(
            project=SETTINGS.vm_project_id,
            zone=_current_vm_placement().zone,
            instance=instance.name,
        )
    return client.get(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        instance=instance.name,
    )


def _quarantine_stale_inflight_vms(vm_type: str) -> list[str]:
    client = compute_v1.InstancesClient()
    request = compute_v1.ListInstancesRequest(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        filter=f"labels.vm-type={vm_type}",
    )
    now = datetime.now(timezone.utc)
    actions: list[str] = []
    for instance in client.list(request=request):
        refreshed = client.get(
            project=SETTINGS.vm_project_id,
            zone=_current_vm_placement().zone,
            instance=instance.name,
        )
        refreshed = _refresh_inflight_progress_phase(client, refreshed)
        if not _is_stale_inflight_vm(refreshed, now=now):
            continue
        reference_time = _instance_boot_reference_time(refreshed)
        if reference_time is None:
            continue
        age_seconds = int((now - reference_time).total_seconds())
        action = _quarantine_pool_vm(
            client,
            refreshed,
            reason=(
                f"pool-role={dict(refreshed.labels or {}).get(POOL_ROLE_LABEL, '')}, "
                f"status={refreshed.status}, age={age_seconds}s"
            ),
        )
        if action:
            actions.append(action)
    return actions


def probe_vm_agent_service(hostname: str, timeout: float = 5.0) -> bool:
    """Probe the VM's agent-service through Caddy.

    Hits ``/api/sessions`` which Caddy reverse-proxies to the agent-service
    on port 3000. Without an Authorization header the agent-service auth
    middleware returns **401** immediately, which proves the process is up.
    If the agent-service is down but Caddy is running, Caddy returns **502**.
    If everything is down the connection fails outright.

    This probe is only valid once a VM is already running an assistant
    workload, such as post-assignment liveness checks or legacy already-
    assigned desktops. Idle pool VMs intentionally keep agent-service off
    until assignment, so claim-time pool health must not depend on this.

    Uses ``verify=False`` because Caddy may still be using a temporary
    self-signed certificate while the ACME challenge completes. The
    connection is within GCP's VPC where infrastructure-level encryption
    already applies.
    """
    import warnings

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=requests.packages.urllib3.exceptions.InsecureRequestWarning,
            )
            resp = requests.get(
                f"https://{hostname}/api/sessions",
                timeout=timeout,
                verify=False,
            )
        return resp.status_code < 500
    except requests.RequestException:
        return False


def probe_vm_agent_service_authenticated(
    hostname: str,
    api_key: str,
    timeout: float = 10.0,
) -> bool:
    """Verify the assigned VM agent accepts the expected bearer token.

    This is stricter than plain HTTPS reachability: it proves the agent
    process is up and accepts the key that the session expects.

    Unlike :func:`probe_vm_agent_service`, TLS is verified here: the request
    carries the session's bearer token to a command-execution endpoint, so
    the certificate has to prove the host is the VM we assigned. Pool VMs
    serve the pre-issued ``*.vm.unify.ai`` wildcard pushed through instance
    metadata, so a verification failure means the VM never received that
    cert — which is a VM that is not ready, not a probe to loosen.
    """
    if not api_key:
        return False
    try:
        resp = requests.post(
            f"https://{hostname}/api/exec",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"command": "echo assistant-session-ready", "timeout": 5000},
            timeout=timeout,
        )
        return 200 <= resp.status_code < 300
    except requests.RequestException:
        return False


# =============================================================================
# Secret Manager
# =============================================================================


def get_secret(secret_name: str, project_id: str = None) -> Optional[str]:
    """
    Fetch a secret from Google Cloud Secret Manager.

    Args:
        secret_name: The name of the secret
        project_id: The GCP project ID (defaults to SETTINGS.vm_project_id)

    Returns:
        The secret value as a string, or None if not found.
    """
    if project_id is None:
        project_id = SETTINGS.vm_project_id

    try:
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        logger.warning(f"Failed to fetch secret '{secret_name}': {e}")
        return None


def get_dns_hostname(assistant_id: str) -> str:
    """Generate consistent DNS hostname from assistant ID.

    Format: unity-assistant-{id}{-staging}.vm.unify.ai

    NOTE: Same for both Windows and Ubuntu - only one VM per assistant.
    """
    return f"unity-assistant-{assistant_id}{SETTINGS.env_suffix}.{DOMAIN_SUFFIX}"


# =============================================================================
# Startup Scripts
# =============================================================================


def load_windows_startup_script() -> str:
    """
    Load the Windows init script from file.

    Returns:
        The PowerShell startup script content.
    """
    with open(WINDOWS_INIT_SCRIPT_PATH, "r") as f:
        return f.read()


def load_ubuntu_startup_script() -> str:
    """
    Load the Ubuntu init script from file.

    Returns:
        The bash startup script content.
    """
    with open(UBUNTU_INIT_SCRIPT_PATH, "r") as f:
        return f.read()


def load_windows_pool_watcher() -> str:
    with open(WINDOWS_POOL_WATCHER_PATH, "r") as f:
        return f.read()


def load_ubuntu_pool_watcher() -> str:
    with open(UBUNTU_POOL_WATCHER_PATH, "r") as f:
        return f.read()


def load_ubuntu_supervisord_conf() -> str:
    with open(UBUNTU_SUPERVISORD_CONF_PATH, "r") as f:
        return f.read()


# =============================================================================
# SSH Key Generation for File Sync
# =============================================================================


def generate_ssh_keypair() -> Tuple[str, str]:
    """Generate an Ed25519 SSH keypair for VM file sync.

    Returns:
        Tuple of (private_key_pem, public_key_openssh)
        - private_key_pem: OpenSSH format private key (PEM)
        - public_key_openssh: OpenSSH format public key (single line)
    """
    # Generate Ed25519 private key
    private_key = ed25519.Ed25519PrivateKey.generate()

    # Serialize private key to OpenSSH format
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    # Serialize public key to OpenSSH format
    public_key = private_key.public_key()
    public_key_openssh = public_key.public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("utf-8")

    # Add comment to public key
    public_key_openssh = f"{public_key_openssh} unity-file-sync"

    return private_key_pem, public_key_openssh


def store_ssh_private_key(
    assistant_id: str,
    private_key: str,
) -> bool:
    """Store SSH private key on the assistant profile via the admin PATCH endpoint.

    Args:
        assistant_id: The assistant ID
        private_key: The SSH private key (PEM format)

    Returns:
        True if stored successfully, False otherwise
    """
    admin_key = SETTINGS.orchestra_admin_key
    if not admin_key:
        logger.error("ORCHESTRA_ADMIN_KEY not configured, cannot store SSH key")
        return False

    url = f"{SETTINGS.orchestra_url}/admin/assistant/{assistant_id}"
    try:
        response = requests.patch(
            url,
            json={"desktop_filesync_sshkey": private_key},
            headers={"Authorization": f"Bearer {admin_key}"},
            timeout=30,
        )
        if response.status_code == 200:
            logger.info(f"Stored SSH private key for assistant {assistant_id}")
            return True
        logger.error(f"Failed to store SSH key: {response.status_code} {response.text}")
        return False
    except Exception as e:
        logger.error(f"Error storing SSH private key: {e}")
        return False


def _fetch_existing_ssh_key(assistant_id: str) -> Optional[str]:
    """Fetch the assistant's existing SSH private key from Orchestra.

    Returns the PEM-encoded private key string, or None if not found.
    """
    admin_key = SETTINGS.orchestra_admin_key
    if not admin_key:
        return None

    url = f"{SETTINGS.orchestra_url}/admin/assistant"
    try:
        response = requests.get(
            url,
            params={"agent_id": str(assistant_id)},
            headers={"Authorization": f"Bearer {admin_key}"},
            timeout=30,
        )
        if response.status_code == 200:
            assistants = response.json().get("info", [])
            if assistants:
                return assistants[0].get("desktop_filesync_sshkey")
    except Exception as e:
        logger.warning(f"Failed to fetch existing SSH key for {assistant_id}: {e}")
    return None


def _derive_public_key(private_key_pem: str) -> str:
    """Derive the OpenSSH public key from a PEM-encoded Ed25519 private key."""
    private_key = serialization.load_ssh_private_key(
        private_key_pem.encode("utf-8"),
        password=None,
    )
    public_key_openssh = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode("utf-8")
    )
    return f"{public_key_openssh} unity-file-sync"


# =============================================================================
# VM Pool Management
# =============================================================================


def _pool_vm_config(vm_type: str) -> Dict[str, Any]:
    """Return type-specific configuration for pool VMs.

    Uses dedicated pool image families (unity-pool-ubuntu-vm / unity-pool-windows-vm)
    so pool images don't affect existing legacy VMs.
    """
    if vm_type == "windows":
        return {
            "machine_type": WINDOWS_VM_MACHINE_TYPE,
            "disk_size_gb": WINDOWS_VM_DISK_SIZE_GB,
            "image_family": POOL_WINDOWS_VM_IMAGE_FAMILY,
            "image_project": WINDOWS_VM_IMAGE_PROJECT,
            "tags": WINDOWS_VM_TAGS,
            "startup_script_key": "windows-startup-script-ps1",
            "startup_script_loader": load_windows_startup_script,
            "pool_watcher_loader": load_windows_pool_watcher,
            "enable_display": True,
        }
    return {
        "machine_type": UBUNTU_VM_MACHINE_TYPE,
        "disk_size_gb": UBUNTU_VM_DISK_SIZE_GB,
        "image_family": POOL_UBUNTU_VM_IMAGE_FAMILY,
        "image_project": UBUNTU_VM_IMAGE_PROJECT,
        "tags": UBUNTU_VM_TAGS,
        "startup_script_key": "startup-script",
        "startup_script_loader": load_ubuntu_startup_script,
        "pool_watcher_loader": load_ubuntu_pool_watcher,
        "supervisord_conf_loader": load_ubuntu_supervisord_conf,
        "enable_display": False,
    }


def _pool_vm_name(vm_type: str, n: int) -> str:
    location = _current_vm_placement().location.id
    location_component = "" if location == SETTINGS.vm_region else f"-{location}"
    return (
        f"{pool_vm_name_prefix(vm_type)}-{vm_type}{location_component}-{n}"
        f"{SETTINGS.env_suffix}"
    )


def _pool_ip_name(vm_type: str, n: int) -> str:
    location = _current_vm_placement().location.id
    location_component = "" if location == SETTINGS.vm_region else f"-{location}"
    return (
        f"{pool_vm_name_prefix(vm_type)}-{vm_type}{location_component}-ip-{n}"
        f"{SETTINGS.env_suffix}"
    )


def _pool_hostname(vm_type: str, n: int) -> str:
    location = _current_vm_placement().location.id
    location_component = "" if location == SETTINGS.vm_region else f"-{location}"
    return (
        f"{pool_vm_name_prefix(vm_type)}-{vm_type}{location_component}-{n}"
        f"{SETTINGS.env_suffix}.{DOMAIN_SUFFIX}"
    )


def _pool_name_env_suffixes() -> tuple[str, ...]:
    """Env suffixes this controller may parse for pool VM/IP names.

    Always includes the live suffix. Retired suffixes (e.g. ``-preview``) are
    included so orphan reclaim can clean up network resources from deleted
    environments that no longer have a controller of their own.
    """
    suffixes = [SETTINGS.env_suffix]
    for retired in POOL_RETIRED_ENV_SUFFIXES:
        if retired not in suffixes:
            suffixes.append(retired)
    return tuple(suffixes)


def _current_pool_environment_label() -> str:
    return "staging" if SETTINGS.deploy_env == "staging" else "production"


def _parse_pool_vm_identity(
    vm_name: str,
    vm_type: str | None = None,
) -> Optional[tuple[str, str, int, str]]:
    """Parse ``(prefix, vm_type, number, env_suffix)`` from a pool VM name.

    Recognizes live and historical prefixes plus the live and retired env
    suffixes. Returns ``None`` for names that are not pool VMs, or that belong
    to a still-active foreign env (e.g. staging names when running in prod).
    """
    types = (vm_type,) if vm_type else ("ubuntu", "windows")
    for candidate_type in types:
        for prefix in pool_vm_name_prefixes(candidate_type):
            head = f"{prefix}-{candidate_type}-"
            if not vm_name.startswith(head):
                continue
            remainder = vm_name[len(head) :]
            for env_suffix in _pool_name_env_suffixes():
                number_text = remainder
                if env_suffix:
                    if not number_text.endswith(env_suffix):
                        continue
                    number_text = number_text[: -len(env_suffix)]
                elif any(
                    number_text.endswith(other)
                    for other in ("-staging",) + POOL_RETIRED_ENV_SUFFIXES
                    if other
                ):
                    # Bare production names must not swallow active staging
                    # (or retired) suffixes as part of the numeric id.
                    continue
                location_and_number = number_text.rsplit("-", 1)
                try:
                    number = int(location_and_number[-1])
                except ValueError:
                    continue
                if len(location_and_number) == 2:
                    try:
                        get_pool_location(location_and_number[0])
                    except ValueError:
                        continue
                return prefix, candidate_type, number, env_suffix
    return None


def _is_current_environment_pool_vm(instance: Any, vm_type: str | None = None) -> bool:
    """Return whether a pool VM is safe for this environment to operate on.

    The name is the durable boundary because older VMs may predate the
    ``environment`` label. When the label is present, require it to agree with
    the name and the controller as a second guard against cross-environment
    claims in shared GCP projects and zones.
    """
    parsed = _parse_pool_vm_identity(str(getattr(instance, "name", "")), vm_type)
    if parsed is None or parsed[3] != SETTINGS.env_suffix:
        return False
    labels = dict(getattr(instance, "labels", None) or {})
    environment = str(labels.get("environment", "") or "")
    return not environment or environment == _current_pool_environment_label()


def _pool_vm_number(vm_name: str, vm_type: str) -> Optional[int]:
    parsed = _parse_pool_vm_identity(vm_name, vm_type)
    if parsed is None:
        return None
    return parsed[2]


def _pool_vm_hostname(vm_name: str, vm_type: str) -> str:
    parsed = _parse_pool_vm_identity(vm_name, vm_type)
    if parsed is None:
        return f"{vm_name}.{DOMAIN_SUFFIX}"
    return f"{vm_name}.{DOMAIN_SUFFIX}"


def _pool_vm_type_from_name(vm_name: str) -> Optional[str]:
    parsed = _parse_pool_vm_identity(vm_name)
    if parsed is None:
        return None
    return parsed[1]


def _pool_ip_name_for_vm(vm_name: str, vm_type: str) -> Optional[str]:
    parsed = _parse_pool_vm_identity(vm_name, vm_type)
    if parsed is None:
        return None
    _, _, vm_number, env_suffix = parsed
    stem = vm_name[: -len(env_suffix)] if env_suffix else vm_name
    prefix = stem[: -len(f"-{vm_number}")]
    return f"{prefix}-ip-{vm_number}{env_suffix}"


def _pool_vm_name_from_ip_name(ip_name: str, vm_type: str) -> Optional[str]:
    """Map a pool static-IP name to its VM name, including historical aliases."""
    for prefix in pool_vm_name_prefixes(vm_type):
        head = f"{prefix}-{vm_type}-ip-"
        if not ip_name.startswith(head):
            continue
        vm_name = ip_name.replace("-ip-", "-", 1)
        if _parse_pool_vm_identity(vm_name, vm_type) is None:
            return None
        return vm_name
    return None


def _current_env_pool_vm_name_from_ip_name(ip_name: str, vm_type: str) -> Optional[str]:
    """Backward-compatible alias for :func:`_pool_vm_name_from_ip_name`."""
    return _pool_vm_name_from_ip_name(ip_name, vm_type)


def _assistant_disk_name(assistant_id: str) -> str:
    sanitized = assistant_id.lower().replace("_", "-")
    location = _current_vm_placement().location.id
    location_component = "" if location == SETTINGS.vm_region else f"-{location}"
    return f"unity-disk-{sanitized}{location_component}{SETTINGS.env_suffix}"


def _pool_bootstrap_metadata_updates(vm_name: str, vm_type: str) -> Dict[str, str]:
    cfg = _pool_vm_config(vm_type)
    metadata_updates = {
        cfg["startup_script_key"]: cfg["startup_script_loader"](),
        "pool-watcher-script": cfg["pool_watcher_loader"](),
        "hostname": _pool_vm_hostname(vm_name, vm_type),
        "orchestra-url": SETTINGS.orchestra_url,
        "comms-url": SETTINGS.comms_url,
        "unity-environment": SETTINGS.deploy_env,
        POOL_CONTRACT_GENERATION_LABEL: POOL_VM_CONTRACT_GENERATION,
    }

    supervisord_conf_loader = cfg.get("supervisord_conf_loader")
    if supervisord_conf_loader:
        metadata_updates["supervisord-conf"] = supervisord_conf_loader()

    # No github-token: the repositories a pool VM clones are public, and the
    # startup scripts already fall back to unauthenticated URLs when the key is
    # absent. Instance metadata is readable by anything running on the VM, so a
    # credential placed there is available to every process on it.

    tls_cert = get_secret(VM_WILDCARD_CERT_SECRET) or ""
    tls_key = get_secret(VM_WILDCARD_KEY_SECRET) or ""
    if tls_cert and tls_key:
        metadata_updates["tls-fullchain"] = tls_cert
        metadata_updates["tls-privkey"] = tls_key

    metadata_updates["archive-bucket"] = POOL_ASSISTANT_ARCHIVE_BUCKET

    return metadata_updates


def _wait_for_pool_static_ip(ip_client, ip_name: str) -> str:
    """Return a reserved pool IP once GCE reports a concrete address value."""

    deadline = time.monotonic() + POOL_STATIC_IP_READY_TIMEOUT_SECONDS
    last_status = "unknown"
    while True:
        try:
            ip_result = ip_client.get(
                project=SETTINGS.vm_project_id,
                region=_current_vm_placement().region,
                address=ip_name,
            )
        except NotFound:
            static_ip = ""
            last_status = "not_found"
        else:
            static_ip = str(getattr(ip_result, "address", "") or "")
            last_status = str(getattr(ip_result, "status", "") or "unknown")
            if static_ip:
                return static_ip

        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Static IP {ip_name} did not obtain an address before timeout "
                f"(status={last_status})",
            )
        time.sleep(POOL_STATIC_IP_READY_POLL_INTERVAL_SECONDS)


def _delete_dns_a_record(hostname: str) -> bool:
    """Delete the hostname's A record if it still exists."""

    dns_client = dns.Client(project=SETTINGS.dns_project_id)
    zone = dns_client.zone(DNS_ZONE_NAME)
    fqdn = f"{hostname}."
    for record in zone.list_resource_record_sets():
        if record.name == fqdn and record.record_type == "A":
            changes = zone.changes()
            changes.delete_record_set(record)
            changes.create()
            logger.info("Deleted DNS A record: %s", hostname)
            return True
    return False


def _upsert_dns_a_record(hostname: str, address: str) -> None:
    """Make ``hostname`` resolve to ``address`` in the VM DNS zone."""

    dns_client = dns.Client(project=SETTINGS.dns_project_id)
    zone = dns_client.zone(DNS_ZONE_NAME)
    fqdn = f"{hostname}."
    for record in zone.list_resource_record_sets():
        if record.name == fqdn and record.record_type == "A":
            if list(record.rrdatas or []) == [address]:
                return
            changes = zone.changes()
            changes.delete_record_set(record)
            changes.create()
            break

    changes = zone.changes()
    changes.add_record_set(zone.resource_record_set(fqdn, "A", 300, [address]))
    changes.create()
    logger.info("Upserted DNS A record: %s -> %s", hostname, address)


def _external_access_config(instance) -> tuple[str, str, str]:
    """Return the primary NIC/access-config identity and its public IP."""

    for interface in getattr(instance, "network_interfaces", None) or []:
        for access_config in getattr(interface, "access_configs", None) or []:
            nat_ip = str(getattr(access_config, "nat_i_p", "") or "")
            if nat_ip:
                return (
                    str(getattr(interface, "name", "") or "nic0"),
                    str(getattr(access_config, "name", "") or "External NAT"),
                    nat_ip,
                )
    raise RuntimeError(f"Pool VM {instance.name} has no external NAT access config")


def _replace_vm_external_ip(
    client,
    vm_name: str,
    *,
    network_interface: str,
    access_config_name: str,
    current_ip: str,
    replacement_ip: str,
) -> None:
    """Replace an instance NAT address, restoring the old one if add fails."""

    if current_ip == replacement_ip:
        return

    client.delete_access_config(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        instance=vm_name,
        access_config=access_config_name,
        network_interface=network_interface,
    ).result()
    try:
        client.add_access_config(
            project=SETTINGS.vm_project_id,
            zone=_current_vm_placement().zone,
            instance=vm_name,
            network_interface=network_interface,
            access_config_resource=compute_v1.AccessConfig(
                name=access_config_name,
                type_="ONE_TO_ONE_NAT",
                nat_i_p=replacement_ip,
                network_tier="PREMIUM",
            ),
        ).result()
    except Exception:
        try:
            client.add_access_config(
                project=SETTINGS.vm_project_id,
                zone=_current_vm_placement().zone,
                instance=vm_name,
                network_interface=network_interface,
                access_config_resource=compute_v1.AccessConfig(
                    name=access_config_name,
                    type_="ONE_TO_ONE_NAT",
                    nat_i_p=current_ip,
                    network_tier="PREMIUM",
                ),
            ).result()
        except Exception:
            logger.exception(
                "Failed restoring pool address %s after IP replacement failure on %s",
                current_ip,
                vm_name,
            )
        raise


def attach_assistant_static_ip_to_pool_vm(
    vm_name: str,
    assistant_id: str,
    vm_type: str,
) -> dict[str, str]:
    """Attach an assistant's stable address and publish its stable DNS name.

    Pool VMs are born with a pool-owned address.  A claimed VM swaps that
    address for the assistant-owned address before its assignment metadata can
    start the guest and before desktop readiness can be published.
    """

    reserved = reserve_assistant_static_ip(assistant_id)
    assistant_ip = str(reserved["address"] or "")
    if not assistant_ip:
        raise RuntimeError(f"Assistant static IP for {assistant_id} has no address")

    client = compute_v1.InstancesClient()
    instance = client.get(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        instance=vm_name,
    )
    network_interface, access_config_name, current_ip = _external_access_config(
        instance,
    )
    pool_ip_name = _pool_ip_name_for_vm(vm_name, vm_type)
    if not pool_ip_name:
        raise RuntimeError(f"Cannot determine pool static IP for {vm_name}")
    pool_ip = _wait_for_pool_static_ip(compute_v1.AddressesClient(), pool_ip_name)
    if current_ip not in (pool_ip, assistant_ip):
        raise RuntimeError(
            f"Refusing to replace unexpected external IP on {vm_name}: {current_ip}",
        )
    _replace_vm_external_ip(
        client,
        vm_name,
        network_interface=network_interface,
        access_config_name=access_config_name,
        current_ip=current_ip,
        replacement_ip=assistant_ip,
    )

    hostname = get_dns_hostname(assistant_id)
    _upsert_dns_a_record(hostname, assistant_ip)
    _report_assistant_static_ip_attachment(
        assistant_id=assistant_id,
        address_name=str(
            reserved.get("name") or assistant_static_ip_name(assistant_id),
        ),
        address=assistant_ip,
        hostname=hostname,
    )
    _log_vm_pool_event(
        "assistant_static_ip_attached",
        assistant_id=assistant_id,
        vm_name=vm_name,
        vm_type=vm_type,
        hostname=hostname,
        ip_address=assistant_ip,
    )
    return {"hostname": hostname, "ip_address": assistant_ip}


def _report_assistant_static_ip_attachment(
    *,
    assistant_id: str,
    address_name: str,
    address: str,
    hostname: str,
) -> None:
    """Best-effort sync of an attached regional address to Orchestra."""

    if not SETTINGS.orchestra_url or not SETTINGS.orchestra_admin_key:
        logger.warning(
            "Cannot report attached assistant IP for %s: Orchestra is not configured",
            assistant_id,
        )
        return
    placement = _current_vm_placement()
    try:
        response = requests.post(
            (
                f"{SETTINGS.orchestra_url}/admin/assistant/{assistant_id}"
                "/managed-desktop/network-identity"
            ),
            json={
                "gcp_address_name": address_name,
                "address": address,
                "region": placement.region,
                "pool_location": placement.location.id,
                "hostname": hostname,
            },
            headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
            timeout=10,
        )
        response.raise_for_status()
    except Exception:
        logger.exception(
            "Failed reporting attached assistant IP for %s to Orchestra",
            assistant_id,
        )


def sync_assistant_static_ip_attachment(
    assistant_id: str,
    placement: VmPlacement | None,
) -> bool:
    """Report an existing assistant address without changing its VM binding."""

    with vm_placement_scope(placement):
        allocation = get_assistant_static_ip(assistant_id)
        if not allocation or not allocation.get("address"):
            return False
        _report_assistant_static_ip_attachment(
            assistant_id=assistant_id,
            address_name=str(
                allocation.get("name") or assistant_static_ip_name(assistant_id),
            ),
            address=str(allocation["address"]),
            hostname=str(
                allocation.get("hostname") or get_dns_hostname(assistant_id),
            ),
        )
    return True


def restore_pool_static_ip_on_vm(
    vm_name: str,
    assistant_id: str,
    vm_type: str,
) -> None:
    """Return a releasing VM to its pool-owned address without releasing ours."""

    ip_name = _pool_ip_name_for_vm(vm_name, vm_type)
    if not ip_name:
        raise RuntimeError(f"Cannot determine pool static IP for {vm_name}")
    pool_ip = _wait_for_pool_static_ip(compute_v1.AddressesClient(), ip_name)

    assistant_address = get_assistant_static_ip(assistant_id)
    client = compute_v1.InstancesClient()
    instance = client.get(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        instance=vm_name,
    )
    network_interface, access_config_name, current_ip = _external_access_config(
        instance,
    )
    if current_ip != pool_ip:
        assistant_ip = str((assistant_address or {}).get("address") or "")
        if assistant_ip and current_ip != assistant_ip:
            raise RuntimeError(
                f"Refusing to replace unexpected external IP on {vm_name}: {current_ip}",
            )
        if not assistant_ip:
            # Pool VMs created before assistant-owned addresses existed can
            # retain an untracked legacy address.  There is no managed
            # assistant address to protect in that case, so returning the VM
            # to its deterministic pool address is the safe migration path.
            logger.warning(
                "Restoring legacy external IP %s on %s to pool address %s",
                current_ip,
                vm_name,
                pool_ip,
            )
        _replace_vm_external_ip(
            client,
            vm_name,
            network_interface=network_interface,
            access_config_name=access_config_name,
            current_ip=current_ip,
            replacement_ip=pool_ip,
        )

    _log_vm_pool_event(
        "pool_static_ip_restored",
        assistant_id=assistant_id,
        vm_name=vm_name,
        vm_type=vm_type,
        ip_address=pool_ip,
    )


def _delete_pool_static_ip(ip_name: str) -> bool:
    """Delete a pool VM's reserved static IP, retrying brief detach lag."""

    ip_client = compute_v1.AddressesClient()
    last_error: Exception | None = None
    for attempt in range(POOL_STATIC_IP_DELETE_MAX_ATTEMPTS):
        try:
            ip_client.delete(
                project=SETTINGS.vm_project_id,
                region=_current_vm_placement().region,
                address=ip_name,
            ).result()
            logger.info("Deleted static IP: %s", ip_name)
            return True
        except NotFound:
            return False
        except Exception as exc:
            last_error = exc
            if attempt == POOL_STATIC_IP_DELETE_MAX_ATTEMPTS - 1:
                break
            logger.info(
                "Retrying static IP delete for %s (%s/%s): %s",
                ip_name,
                attempt + 1,
                POOL_STATIC_IP_DELETE_MAX_ATTEMPTS,
                exc,
            )
            time.sleep(POOL_STATIC_IP_DELETE_RETRY_SECONDS)
    raise RuntimeError(f"Failed to delete static IP {ip_name}: {last_error}")


def _cleanup_deleted_pool_vm_network_resources(
    vm_name: str,
    *,
    vm_type: str | None = None,
) -> Dict[str, Any]:
    """Best-effort cleanup of stale DNS and static IP after deleting a pool VM."""

    resolved_vm_type = vm_type or _pool_vm_type_from_name(vm_name)
    if not resolved_vm_type:
        return {
            "vm_name": vm_name,
            "vm_type": None,
            "hostname": None,
            "ip_name": None,
            "dns_deleted": False,
            "ip_deleted": False,
            "errors": [],
        }

    hostname = _pool_vm_hostname(vm_name, resolved_vm_type)
    ip_name = _pool_ip_name_for_vm(vm_name, resolved_vm_type)
    errors: list[dict[str, str]] = []
    dns_deleted = False
    ip_deleted = False

    try:
        dns_deleted = _delete_dns_a_record(hostname)
    except Exception as exc:
        logger.error("Failed to delete stale DNS record for %s: %s", hostname, exc)
        errors.append({"resource": hostname, "error": str(exc)})

    if ip_name:
        try:
            ip_deleted = _delete_pool_static_ip(ip_name)
        except Exception as exc:
            logger.error("Failed to delete stale static IP %s: %s", ip_name, exc)
            errors.append({"resource": ip_name, "error": str(exc)})

    _log_vm_pool_event(
        "deleted_vm_network_cleanup",
        vm_name=vm_name,
        vm_type=resolved_vm_type,
        hostname=hostname,
        ip_name=ip_name,
        dns_deleted=dns_deleted,
        ip_deleted=ip_deleted,
        cleanup_errors=len(errors),
    )
    return {
        "vm_name": vm_name,
        "vm_type": resolved_vm_type,
        "hostname": hostname,
        "ip_name": ip_name,
        "dns_deleted": dns_deleted,
        "ip_deleted": ip_deleted,
        "errors": errors,
    }


def find_vm_with_disk(assistant_id: str) -> Optional[str]:
    """Return the VM name currently attached to the assistant disk, if any."""
    client = compute_v1.DisksClient()
    disk_name = _assistant_disk_name(assistant_id)
    try:
        disk = client.get(
            project=SETTINGS.vm_project_id,
            zone=_current_vm_placement().zone,
            disk=disk_name,
        )
    except NotFound:
        return None
    for user in getattr(disk, "users", None) or []:
        if "/instances/" in user:
            return user.rsplit("/", 1)[-1]
    return None


def _attached_disk_vm_state(vm_name: str) -> Dict[str, Any]:
    """Return the current runtime ownership state for a VM that holds a disk."""

    client = compute_v1.InstancesClient()
    vm = client.get(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        instance=vm_name,
    )
    labels = dict(vm.labels) if vm.labels else {}
    external_ip = None
    if vm.network_interfaces:
        for ni in vm.network_interfaces:
            if ni.access_configs:
                for ac in ni.access_configs:
                    if ac.nat_i_p:
                        external_ip = ac.nat_i_p
                        break
    return {
        "vm_name": vm.name,
        "assistant_id": labels.get(ASSISTANT_ID_LABEL, "") or None,
        "binding_id": labels.get(BINDING_ID_LABEL, "") or None,
        "pool_role": labels.get(POOL_ROLE_LABEL, "") or None,
        "status": vm.status,
        "hostname": _vm_ref_from_instance(vm)["hostname"],
        "ip_address": external_ip,
    }


def _ensure_disk_ready_for_binding(
    assistant_id: str,
    binding_id: str,
) -> Optional[Dict[str, Any]]:
    """Clear a stale releasing disk owner before assigning a new binding.

    Returns the owner state when the disk is already attached to a VM that *this
    same binding* owns, meaning the caller should adopt that VM rather than claim
    a fresh one. Returns ``None`` when the disk is free to use.

    The same-binding case is a torn assignment: a previous attempt claimed the
    VM, attached the disk and labelled both for this binding, then failed (or
    lost a race) before the session persisted the vmRef. Raising here instead
    deadlocks the binding against its own leftover disk on every retry, with no
    reconciler to break the tie — the orphan sweeper keys on "no live Job for
    this binding", which is false while the binding is live.
    """

    attached_vm_name = find_vm_with_disk(assistant_id)
    if not attached_vm_name:
        return None

    owner = _attached_disk_vm_state(attached_vm_name)
    owner_binding_id = str(owner.get("binding_id", "") or "")
    owner_pool_role = str(owner.get("pool_role", "") or "")
    requested_binding = binding_id.lower().replace("_", "-")
    if owner_binding_id == requested_binding and owner_pool_role == "assigned":
        _log_vm_pool_event(
            "disk_handoff_adopt_own_vm",
            assistant_id=assistant_id,
            binding_id=binding_id,
            vm_name=attached_vm_name,
            owner_pool_role=owner_pool_role,
        )
        return owner
    if owner_binding_id != requested_binding and owner_pool_role != "assigned":
        _log_vm_pool_event(
            "disk_handoff_reclaim_stale_owner",
            assistant_id=assistant_id,
            binding_id=binding_id,
            stale_binding_id=owner_binding_id or None,
            vm_name=attached_vm_name,
            stale_pool_role=owner_pool_role or None,
        )
        reclaim_orphaned_assistant_disk(
            assistant_id,
            current_binding_id=binding_id,
        )
        attached_vm_name = find_vm_with_disk(assistant_id)
        if not attached_vm_name:
            return None
        owner = _attached_disk_vm_state(attached_vm_name)
        owner_binding_id = str(owner.get("binding_id", "") or "")
        owner_pool_role = str(owner.get("pool_role", "") or "")

    details = [attached_vm_name]
    if owner_pool_role:
        details.append(f"pool_role={owner_pool_role}")
    if owner_binding_id:
        details.append(f"binding_id={owner_binding_id}")
    raise AssistantDiskInUseError(
        f"Assistant disk {_assistant_disk_name(assistant_id)} is still attached to "
        + " ".join(details),
    )


def list_pool_vms(vm_type: Optional[str] = None) -> list[Dict[str, Any]]:
    """List all pool VMs, optionally filtered by type."""
    client = compute_v1.InstancesClient()

    label_filter = "labels.pool-role:*"
    if vm_type:
        label_filter += f" AND labels.vm-type={vm_type}"

    request = compute_v1.ListInstancesRequest(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        filter=label_filter,
    )
    results = []
    for instance in client.list(request=request):
        if not _is_current_environment_pool_vm(instance, vm_type):
            continue
        labels = dict(instance.labels) if instance.labels else {}
        external_ip = None
        if instance.network_interfaces:
            for ni in instance.network_interfaces:
                if ni.access_configs:
                    for ac in ni.access_configs:
                        if ac.nat_i_p:
                            external_ip = ac.nat_i_p
                            break

        hostname = labels.get("pool-hostname", instance.name + f".{DOMAIN_SUFFIX}")
        entry: Dict[str, Any] = {
            "vm_name": instance.name,
            "pool_role": labels.get("pool-role", "unknown"),
            "assistant_id": labels.get("assistant-id", "") or None,
            "binding_id": labels.get(BINDING_ID_LABEL, "") or None,
            "vm_type": labels.get("vm-type", "unknown"),
            "contract_generation": labels.get(POOL_CONTRACT_GENERATION_LABEL) or None,
            "contract_current": _has_current_pool_contract(instance),
            "ip_address": external_ip,
            "hostname": hostname,
            "status": instance.status,
            "label_fingerprint": instance.label_fingerprint,
        }
        if instance.last_start_timestamp:
            entry["last_start_timestamp"] = instance.last_start_timestamp
        if instance.last_stop_timestamp:
            entry["last_stop_timestamp"] = instance.last_stop_timestamp
        results.append(entry)
    return results


def split_binding_runtime_vms(
    assistant_id: str,
    *,
    binding_id: str | None = None,
) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
    """Split assistant-owned runtime VMs into current-binding and other sets."""

    sanitized_assistant_id = assistant_id.lower().replace("_", "-")
    runtime_vms = [
        vm
        for vm in list_pool_vms()
        if vm.get("assistant_id") == sanitized_assistant_id
        and vm.get("pool_role") in ("assigned", POOL_ROLE_RELEASING)
    ]
    if not binding_id:
        return runtime_vms, []

    binding_label = binding_id.lower().replace("_", "-")
    current_binding_vms = []
    other_binding_vms = []
    for vm in runtime_vms:
        if str(vm.get("binding_id", "") or "") == binding_label:
            current_binding_vms.append(vm)
        else:
            other_binding_vms.append(vm)
    return current_binding_vms, other_binding_vms


def provision_pool_vm(vm_type: str, n: int) -> Dict[str, Any]:
    """Create a new pool VM and return once the GCE create request is accepted.

    The instance boots asynchronously after the insert operation starts. The
    pool already models that in-flight state via the ``provisioning`` label, so
    callers such as ``rebalance_pool()`` should not block an HTTP request on the
    full VM boot lifecycle.
    """
    vm_name = _pool_vm_name(vm_type, n)
    ip_name = _pool_ip_name(vm_type, n)
    hostname = _pool_hostname(vm_type, n)
    cfg = _pool_vm_config(vm_type)

    # Reserve static IP
    ip_client = compute_v1.AddressesClient()
    address = compute_v1.Address(
        name=ip_name,
        address_type="EXTERNAL",
        network_tier="PREMIUM",
        description=f"Static IP for pool VM {vm_name}",
    )
    try:
        op = ip_client.insert(
            project=SETTINGS.vm_project_id,
            region=_current_vm_placement().region,
            address_resource=address,
        )
        op.result()
        logger.info(f"Reserved static IP: {ip_name}")
    except Conflict:
        logger.info(f"Static IP {ip_name} already exists, reusing")

    static_ip = _wait_for_pool_static_ip(ip_client, ip_name)

    # Create DNS A record
    dns_client = dns.Client(project=SETTINGS.dns_project_id)
    zone = dns_client.zone(DNS_ZONE_NAME)
    fqdn = f"{hostname}."
    try:
        for record in zone.list_resource_record_sets():
            if record.name == fqdn and record.record_type == "A":
                changes = zone.changes()
                changes.delete_record_set(record)
                changes.create()
                break
    except Exception as e:
        logger.warning(f"Could not check existing DNS records: {e}")

    record_set = zone.resource_record_set(fqdn, "A", 300, [static_ip])
    changes = zone.changes()
    changes.add_record_set(record_set)
    changes.create()
    logger.info(f"Created DNS A record: {hostname} -> {static_ip}")

    metadata_items = [
        compute_v1.Items(key=key, value=value)
        for key, value in _pool_bootstrap_metadata_updates(vm_name, vm_type).items()
    ]

    labels = {
        POOL_ROLE_LABEL: "provisioning",
        ASSISTANT_ID_LABEL: "",
        BINDING_ID_LABEL: "",
        "vm-type": vm_type,
        "pool-hostname": hostname.replace(".", "-"),
        "pool-location": _current_vm_placement().location.id,
        POOL_CONTRACT_GENERATION_LABEL: POOL_VM_CONTRACT_GENERATION,
        POOL_TRANSITION_EPOCH_LABEL: _pool_transition_epoch_value(),
        POOL_PROGRESS_PHASE_LABEL: "provisioning",
        POOL_PROGRESS_EPOCH_LABEL: _pool_progress_epoch_value(),
        **POOL_GOVERNANCE_LABELS,
        "environment": "staging" if SETTINGS.deploy_env == "staging" else "production",
    }

    instance_kwargs = dict(
        name=vm_name,
        machine_type=f"zones/{_current_vm_placement().zone}/machineTypes/{cfg['machine_type']}",
        description=f"Unity pool VM ({vm_type}) #{n}",
        labels=labels,
        tags=compute_v1.Tags(items=cfg["tags"]),
        disks=[
            compute_v1.AttachedDisk(
                boot=True,
                auto_delete=True,
                initialize_params=compute_v1.AttachedDiskInitializeParams(
                    disk_size_gb=cfg["disk_size_gb"],
                    disk_type=f"zones/{_current_vm_placement().zone}/diskTypes/{VM_DISK_TYPE}",
                    source_image=f"projects/{cfg['image_project']}/global/images/family/{cfg['image_family']}",
                ),
            ),
        ],
        network_interfaces=[
            compute_v1.NetworkInterface(
                network=f"global/networks/{VM_NETWORK}",
                access_configs=[
                    compute_v1.AccessConfig(
                        name="External NAT",
                        type_="ONE_TO_ONE_NAT",
                        nat_i_p=static_ip,
                        network_tier="PREMIUM",
                    ),
                ],
            ),
        ],
        metadata=compute_v1.Metadata(items=metadata_items),
        scheduling=compute_v1.Scheduling(
            on_host_maintenance="MIGRATE",
            automatic_restart=True,
        ),
        service_accounts=[
            compute_v1.ServiceAccount(
                email=f"pool-vm-sa@{SETTINGS.vm_project_id}.iam.gserviceaccount.com",
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            ),
        ],
    )
    if cfg["enable_display"]:
        instance_kwargs["display_device"] = compute_v1.DisplayDevice(
            enable_display=True,
        )

    instance = compute_v1.Instance(**instance_kwargs)
    client = compute_v1.InstancesClient()
    op = client.insert(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        instance_resource=instance,
    )

    logger.info(
        "Provision request submitted for pool VM %s (%s) with IP %s",
        vm_name,
        vm_type,
        static_ip,
    )
    _log_vm_pool_event(
        "provision",
        vm_name=vm_name,
        vm_type=vm_type,
        hostname=hostname,
        ip_address=static_ip,
        operation_name=getattr(op, "name", None),
    )
    return {
        "vm_name": vm_name,
        "ip_address": static_ip,
        "hostname": hostname,
        "vm_type": vm_type,
        "status": "PROVISIONING",
    }


def _set_pool_labels(
    client: compute_v1.InstancesClient,
    vm_name: str,
    label_overrides: Dict[str, str],
    max_retries: int = 3,
    expected_role: Optional[str] = None,
) -> bool:
    """Set labels on a pool VM with retry on fingerprint conflict.

    Re-reads the VM on each attempt so the fingerprint and base labels
    are always fresh, avoiding 412 PRECONDITION_FAILED when concurrent
    operations (e.g. parallel trims) update labels between our read and
    write.

    When ``expected_role`` is set, the current ``pool-role`` label must
    match before the update is applied.  Returns False immediately if
    the role doesn't match (another operation claimed or released the
    VM concurrently).  Returns True on successful update.
    """
    for attempt in range(max_retries):
        fresh = client.get(
            project=SETTINGS.vm_project_id,
            zone=_current_vm_placement().zone,
            instance=vm_name,
        )
        labels = dict(fresh.labels) if fresh.labels else {}
        if expected_role is not None and labels.get(POOL_ROLE_LABEL) != expected_role:
            logger.info(
                f"Skipping label update on {vm_name}: "
                f"expected pool-role={expected_role}, "
                f"got {labels.get(POOL_ROLE_LABEL)}",
            )
            return False
        labels.update(label_overrides)
        if POOL_ROLE_LABEL in label_overrides:
            labels[POOL_TRANSITION_EPOCH_LABEL] = _pool_transition_epoch_value()
            if POOL_PROGRESS_PHASE_LABEL not in label_overrides:
                progress_phase = _progress_phase_for_role(
                    label_overrides[POOL_ROLE_LABEL],
                )
                if progress_phase:
                    labels[POOL_PROGRESS_PHASE_LABEL] = progress_phase
                    labels[POOL_PROGRESS_EPOCH_LABEL] = _pool_progress_epoch_value()
                else:
                    labels.pop(POOL_PROGRESS_PHASE_LABEL, None)
                    labels.pop(POOL_PROGRESS_EPOCH_LABEL, None)
        try:
            client.set_labels(
                project=SETTINGS.vm_project_id,
                zone=_current_vm_placement().zone,
                instance=vm_name,
                instances_set_labels_request_resource=compute_v1.InstancesSetLabelsRequest(
                    labels=labels,
                    label_fingerprint=fresh.label_fingerprint,
                ),
            ).result()
            return True
        except PreconditionFailed:
            if attempt == max_retries - 1:
                raise
            logger.info(
                f"Label fingerprint conflict on {vm_name}, "
                f"retrying ({attempt + 1}/{max_retries})",
            )
    return False


def _regional_reaper_core_api():
    """Return the Core API used to persist regional pool reaper state."""
    from .helpers import setup_kubernetes_client

    _, core_api, _, _ = setup_kubernetes_client()
    if core_api is None:
        raise RuntimeError(
            "Kubernetes CoreV1Api is unavailable for regional pool reaper",
        )
    return core_api


def _read_regional_reaper_state(
    core_api,
    region: str,
) -> tuple[dict[str, Any], str | None]:
    """Read one region's durable reaper state and ConfigMap resource version."""
    try:
        config_map = core_api.read_namespaced_config_map(
            REGIONAL_POOL_REAPER_CONFIG_MAP,
            SETTINGS.default_namespace,
        )
    except ApiException as exc:
        if exc.status != 404:
            raise
        return {}, None
    data = dict(config_map.data or {})
    raw = data.get(region, "")
    try:
        state = json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        logger.warning("Ignoring malformed regional reaper state for %s", region)
        state = {}
    return (
        state if isinstance(state, dict) else {}
    ), config_map.metadata.resource_version


def _write_regional_reaper_state(
    core_api,
    region: str,
    state: dict[str, Any],
    *,
    resource_version: str | None,
) -> None:
    """Persist one region's state with a resource-version CAS."""
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":"))
    if resource_version is None:
        try:
            core_api.create_namespaced_config_map(
                SETTINGS.default_namespace,
                k8s_client.V1ConfigMap(
                    metadata=k8s_client.V1ObjectMeta(
                        name=REGIONAL_POOL_REAPER_CONFIG_MAP,
                    ),
                    data={region: encoded},
                ),
            )
            return
        except ApiException as exc:
            if exc.status != 409:
                raise
            # Another worker created the shared map. Retry using its version.
            _, resource_version = _read_regional_reaper_state(core_api, region)

    config_map = core_api.read_namespaced_config_map(
        REGIONAL_POOL_REAPER_CONFIG_MAP,
        SETTINGS.default_namespace,
    )
    data = dict(config_map.data or {})
    data[region] = encoded
    config_map.data = data
    core_api.replace_namespaced_config_map(
        REGIONAL_POOL_REAPER_CONFIG_MAP,
        SETTINGS.default_namespace,
        config_map,
    )


def _set_regional_reaper_state(
    region: str,
    state: dict[str, Any],
    *,
    core_api=None,
) -> None:
    """Persist a regional reaper fence, retrying ConfigMap update conflicts."""
    core_api = core_api or _regional_reaper_core_api()
    for attempt in range(3):
        _, resource_version = _read_regional_reaper_state(core_api, region)
        try:
            _write_regional_reaper_state(
                core_api,
                region,
                state,
                resource_version=resource_version,
            )
            return
        except ApiException as exc:
            if exc.status != 409 or attempt == 2:
                raise
    raise RuntimeError(f"Unable to persist regional reaper state for {region}")


def _regional_pool_reaper_state(region: str, *, core_api=None) -> dict[str, Any]:
    core_api = core_api or _regional_reaper_core_api()
    state, _ = _read_regional_reaper_state(core_api, region)
    return state


def clear_regional_pool_reaper_fence(
    placement: VmPlacement | None = None,
    *,
    core_api=None,
) -> None:
    """Reopen a nonlegacy location before an assignment can claim capacity."""
    placement = placement or _current_vm_placement()
    if placement.region == SETTINGS.vm_region:
        return
    state = _regional_pool_reaper_state(placement.region, core_api=core_api)
    if not state.get("fenced") and not state.get("emptySince"):
        return
    _set_regional_reaper_state(
        placement.region,
        {"fenced": False},
        core_api=core_api,
    )
    logger.info("Cleared regional pool reaper fence for %s", placement.region)


def _regional_pool_reaper_fenced(placement: VmPlacement | None = None) -> bool:
    """Return whether background capacity maintenance is fenced for a region."""
    placement = placement or _current_vm_placement()
    if placement.region == SETTINGS.vm_region:
        return False
    return bool(_regional_pool_reaper_state(placement.region).get("fenced"))


def _parse_reaper_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def reap_inactive_regional_pools(
    *,
    core_api=None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fence and remove idle capacity in unused nonlegacy regional pools.

    A location must have no assigned or releasing VM for a full hour before it
    is fenced. The fence is persisted in Kubernetes before deletion and is
    rechecked after a fresh GCE read, so an assignment that clears the fence
    cannot race into deleting an owned VM.
    """
    core_api = core_api or _regional_reaper_core_api()
    now = now or datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []
    client = compute_v1.InstancesClient()

    for location in list_pool_locations():
        if location.region == SETTINGS.vm_region:
            continue
        vms_by_zone: dict[str, list[Any]] = {}
        for zone in location.zones:
            placement = VmPlacement(location, zone, "", "regional_reaper")
            with vm_placement_scope(placement):
                vms_by_zone[zone] = list(
                    client.list(
                        request=compute_v1.ListInstancesRequest(
                            project=SETTINGS.vm_project_id,
                            zone=zone,
                            filter="labels.pool-role:*",
                        ),
                    ),
                )
        vms = [vm for zone_vms in vms_by_zone.values() for vm in zone_vms]
        roles = {str((vm.labels or {}).get(POOL_ROLE_LABEL, "")) for vm in vms}
        active = bool({POOL_ROLE_RELEASING, "assigned"} & roles)
        state = _regional_pool_reaper_state(location.region, core_api=core_api)
        if active:
            if state:
                _set_regional_reaper_state(
                    location.region,
                    {"fenced": False},
                    core_api=core_api,
                )
            results.append(
                {"region": location.region, "action": "active", "vm_count": len(vms)},
            )
            continue

        empty_since = _parse_reaper_timestamp(state.get("emptySince"))
        if empty_since is None:
            _set_regional_reaper_state(
                location.region,
                {"emptySince": now.isoformat(), "fenced": False},
                core_api=core_api,
            )
            results.append({"region": location.region, "action": "grace_started"})
            continue
        if (now - empty_since).total_seconds() < REGIONAL_POOL_REAPER_GRACE_SECONDS:
            results.append({"region": location.region, "action": "grace_pending"})
            continue

        fence_state = {"emptySince": empty_since.isoformat(), "fenced": True}
        _set_regional_reaper_state(location.region, fence_state, core_api=core_api)
        # Read after fencing. An assignment clears the fence before claiming,
        # and an owned VM is never an eligible deletion target regardless.
        state = _regional_pool_reaper_state(location.region, core_api=core_api)
        current_vms_by_zone: dict[str, list[Any]] = {}
        for zone in location.zones:
            placement = VmPlacement(location, zone, "", "regional_reaper")
            with vm_placement_scope(placement):
                current_vms_by_zone[zone] = list(
                    client.list(
                        request=compute_v1.ListInstancesRequest(
                            project=SETTINGS.vm_project_id,
                            zone=zone,
                            filter="labels.pool-role:*",
                        ),
                    ),
                )
        current_vms = [
            vm for zone_vms in current_vms_by_zone.values() for vm in zone_vms
        ]
        current_roles = {
            str((vm.labels or {}).get(POOL_ROLE_LABEL, "")) for vm in current_vms
        }
        if not state.get("fenced") or {POOL_ROLE_RELEASING, "assigned"} & current_roles:
            _set_regional_reaper_state(
                location.region,
                {"fenced": False},
                core_api=core_api,
            )
            results.append({"region": location.region, "action": "reopen_race"})
            continue
        deleted = []
        for zone, zone_vms in current_vms_by_zone.items():
            placement = VmPlacement(location, zone, "", "regional_reaper")
            with vm_placement_scope(placement):
                for vm in zone_vms:
                    role = str((vm.labels or {}).get(POOL_ROLE_LABEL, ""))
                    if role not in REGIONAL_POOL_REAPER_REAPABLE_ROLES:
                        continue
                    # Claiming uses the same label-fingerprint CAS. Moving a
                    # candidate out of its claimable role before deletion
                    # makes this race-safe: either the assignment wins and we
                    # skip it, or the reaper wins and the assignment retries.
                    if not _set_pool_labels(
                        client,
                        vm.name,
                        {POOL_ROLE_LABEL: "reaping"},
                        expected_role=role,
                    ):
                        continue
                    _delete_pool_vm_instance(
                        client,
                        vm.name,
                        vm_type=(vm.labels or {}).get("vm-type"),
                    )
                    deleted.append(vm.name)
        results.append(
            {"region": location.region, "action": "reaped", "deleted": deleted},
        )
    return {"regions": results}


def claim_idle_vm(
    assistant_id: str,
    binding_id: str,
    vm_type: str,
    vm_number: int | None = None,
) -> Dict[str, Any]:
    """Atomically claim an idle pool VM using label fingerprint CAS.

    Tracks demand via a process-level counter so that replenish_pool
    provisions enough VMs for all waiting callers.

    Raises ValueError immediately if no idle VMs are available (after
    one replenish attempt).  The caller is expected to publish to the
    pending-VM queue for deferred retry instead of blocking a thread.
    """
    client = compute_v1.InstancesClient()
    label_filter = (
        f"labels.pool-role=idle AND labels.vm-type={vm_type} "
        f"AND labels.environment={_current_pool_environment_label()} "
        f"AND labels.{POOL_CONTRACT_GENERATION_LABEL}={POOL_VM_CONTRACT_GENERATION} "
        "AND status=RUNNING"
    )

    pending_key = _pool_scope_key(vm_type)
    with _pending_lock:
        _pending_claims[pending_key] = _pending_claims.get(pending_key, 0) + 1

    try:
        return _claim_idle_vm_inner(
            client,
            label_filter,
            assistant_id,
            binding_id,
            vm_type,
            vm_number,
        )
    finally:
        with _pending_lock:
            _pending_claims[pending_key] = max(
                0,
                _pending_claims.get(pending_key, 0) - 1,
            )


def _claim_idle_vm_inner(
    client,
    label_filter: str,
    assistant_id: str,
    binding_id: str,
    vm_type: str,
    vm_number: int | None,
) -> Dict[str, Any]:
    while True:
        request = compute_v1.ListInstancesRequest(
            project=SETTINGS.vm_project_id,
            zone=_current_vm_placement().zone,
            filter=label_filter,
        )
        listed_idle_vms = list(client.list(request=request))
        idle_vms = [
            vm for vm in listed_idle_vms if _is_current_environment_pool_vm(vm, vm_type)
        ]
        if len(idle_vms) != len(listed_idle_vms):
            logger.warning(
                "Ignoring foreign-environment idle pool VMs: %s",
                [vm.name for vm in listed_idle_vms if vm not in idle_vms],
            )
        if not idle_vms:
            replenish_pool(vm_type)
            raise ValueError(
                f"No idle {vm_type} pool VMs available — "
                f"request queued for deferred retry",
            )

        if vm_number is not None:
            target_name = _pool_vm_name(vm_type, vm_number)
            matching = [vm for vm in idle_vms if vm.name == target_name]
            if not matching:
                idle_names = [vm.name for vm in idle_vms]
                raise ValueError(
                    f"VM {target_name} is not idle. Idle VMs: {idle_names}",
                )
            candidate_name = matching[0].name
        else:
            candidate_name = random.choice(idle_vms).name

        vm_lock = _get_vm_claim_lock(candidate_name)
        if not vm_lock.acquire(blocking=False):
            logger.info(
                f"VM {candidate_name} being claimed by another thread, retrying",
            )
            continue

        try:
            # Point-read for strongly consistent state and fingerprint.
            # The LIST above is eventually consistent — its fingerprint may
            # be stale, allowing two concurrent setLabels calls to both pass
            # the CAS check.  GET is strongly consistent per GCE docs.
            fresh = client.get(
                project=SETTINGS.vm_project_id,
                zone=_current_vm_placement().zone,
                instance=candidate_name,
            )
            if not _is_current_environment_pool_vm(fresh, vm_type):
                _log_vm_pool_event(
                    "claim_skipped",
                    assistant_id=assistant_id,
                    vm_name=candidate_name,
                    vm_type=vm_type,
                    reason="foreign_environment",
                )
                continue
            if not _has_current_pool_contract(fresh):
                logger.info(
                    "Skipping stale-contract idle VM %s during claim",
                    candidate_name,
                )
                _log_vm_pool_event(
                    "claim_skipped",
                    assistant_id=assistant_id,
                    vm_name=candidate_name,
                    vm_type=vm_type,
                    reason="stale_contract",
                    contract_generation=_pool_contract_generation(fresh) or None,
                    current_contract_generation=POOL_VM_CONTRACT_GENERATION,
                )
                continue
            if fresh.labels.get("pool-role") != "idle":
                logger.info(
                    f"VM {candidate_name} already claimed (pool-role="
                    f"{fresh.labels.get('pool-role')}), retrying",
                )
                continue

            hostname = _read_instance_metadata(fresh, "hostname")
            if not hostname:
                hostname_label = fresh.labels.get("pool-hostname", "")
                hostname = (
                    hostname_label.replace("-", ".")
                    if hostname_label
                    else candidate_name + f".{DOMAIN_SUFFIX}"
                )
            # Idle pool VMs intentionally keep agent-service off until the
            # watcher sees assignment metadata. Claim only binds ownership;
            # strict desktop readiness is enforced later via /infra/vm/ready.
            sanitized = assistant_id.lower().replace("_", "-")
            binding_label = binding_id.lower().replace("_", "-")
            new_labels = dict(fresh.labels) if fresh.labels else {}
            new_labels["pool-role"] = "assigned"
            new_labels["assistant-id"] = sanitized
            new_labels[BINDING_ID_LABEL] = binding_label
            new_labels[POOL_TRANSITION_EPOCH_LABEL] = _pool_transition_epoch_value()
            new_labels[POOL_PROGRESS_PHASE_LABEL] = "assigned"
            new_labels[POOL_PROGRESS_EPOCH_LABEL] = _pool_progress_epoch_value()

            try:
                op = client.set_labels(
                    project=SETTINGS.vm_project_id,
                    zone=_current_vm_placement().zone,
                    instance=candidate_name,
                    instances_set_labels_request_resource=compute_v1.InstancesSetLabelsRequest(
                        labels=new_labels,
                        label_fingerprint=fresh.label_fingerprint,
                    ),
                )
                op.result()
                logger.info(
                    f"Claimed pool VM {candidate_name} for assistant {assistant_id}",
                )

                external_ip = None
                if fresh.network_interfaces:
                    for ni in fresh.network_interfaces:
                        if ni.access_configs:
                            for ac in ni.access_configs:
                                if ac.nat_i_p:
                                    external_ip = ac.nat_i_p
                                    break

                _log_vm_pool_event(
                    "claim",
                    assistant_id=assistant_id,
                    binding_id=binding_id,
                    vm_name=candidate_name,
                    vm_type=vm_type,
                    hostname=hostname,
                )

                return {
                    "vm_name": candidate_name,
                    "assistant_id": assistant_id,
                    "ip_address": external_ip,
                    "hostname": hostname,
                    "desktop_url": f"https://{hostname}",
                    "status": "RUNNING",
                }
            except PreconditionFailed:
                continue
        finally:
            vm_lock.release()


def _read_instance_metadata(instance, key: str) -> Optional[str]:
    """Read a metadata value from a GCE instance object."""
    if instance.metadata and instance.metadata.items:
        for item in instance.metadata.items:
            if item.key == key:
                return item.value
    return None


def create_assistant_disk(assistant_id: str) -> str:
    """Create a persistent disk for an assistant (64 GB standard PD).

    Returns the disk self-link. Idempotent — returns existing disk if present.
    """
    client = compute_v1.DisksClient()
    disk_name = _assistant_disk_name(assistant_id)

    disk = compute_v1.Disk(
        name=disk_name,
        size_gb=POOL_ASSISTANT_DISK_SIZE_GB,
        type_=f"zones/{_current_vm_placement().zone}/diskTypes/{POOL_ASSISTANT_DISK_TYPE}",
        description=f"Persistent storage for assistant {assistant_id}",
    )

    try:
        op = client.insert(
            project=SETTINGS.vm_project_id,
            zone=_current_vm_placement().zone,
            disk_resource=disk,
        )
        op.result()
        logger.info(
            f"Created assistant disk: {disk_name} ({POOL_ASSISTANT_DISK_SIZE_GB} GB)",
        )
        _log_vm_pool_event(
            "create_disk",
            assistant_id=assistant_id,
            disk_name=disk_name,
            disk_size_gb=POOL_ASSISTANT_DISK_SIZE_GB,
        )
    except Conflict:
        logger.info(f"Assistant disk {disk_name} already exists")

    result = client.get(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        disk=disk_name,
    )
    return result.self_link


def attach_assistant_disk(
    vm_name: str,
    assistant_id: str,
    *,
    binding_id: str | None = None,
) -> str:
    """Attach an assistant's persistent disk to a pool VM.

    Returns the device name used for mounting.
    """
    client = compute_v1.InstancesClient()
    disk_name = _assistant_disk_name(assistant_id)
    disk_source = f"projects/{SETTINGS.vm_project_id}/zones/{_current_vm_placement().zone}/disks/{disk_name}"

    attached_disk = compute_v1.AttachedDisk(
        source=disk_source,
        device_name=disk_name,
        auto_delete=False,
        mode="READ_WRITE",
    )

    op = client.attach_disk(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        instance=vm_name,
        attached_disk_resource=attached_disk,
    )
    op.result()

    # The device name defaults to the disk name
    device_name = disk_name
    logger.info(f"Attached disk {disk_name} to {vm_name} (device: {device_name})")
    _log_vm_pool_event(
        "attach_disk",
        assistant_id=assistant_id,
        binding_id=binding_id,
        vm_name=vm_name,
        disk_name=disk_name,
        device_name=device_name,
    )
    return device_name


def detach_assistant_disk(vm_name: str, assistant_id: str) -> bool:
    """Detach an assistant's persistent disk from a pool VM."""
    client = compute_v1.InstancesClient()
    disk_name = _assistant_disk_name(assistant_id)
    disk_suffix = f"/disks/{disk_name}"

    vm = client.get(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        instance=vm_name,
    )
    actual_device_name = None
    if vm.disks:
        for d in vm.disks:
            if d.source and d.source.endswith(disk_suffix):
                actual_device_name = d.device_name
                break

    if not actual_device_name:
        logger.warning(f"Disk {disk_name} not attached to {vm_name}, skipping detach")
        _log_vm_pool_event(
            "detach_disk_skipped",
            assistant_id=assistant_id,
            vm_name=vm_name,
            disk_name=disk_name,
        )
        return False

    op = client.detach_disk(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        instance=vm_name,
        device_name=actual_device_name,
    )
    op.result()
    logger.info(
        f"Detached disk {disk_name} from {vm_name} (device: {actual_device_name})",
    )
    _log_vm_pool_event(
        "detach_disk",
        assistant_id=assistant_id,
        vm_name=vm_name,
        disk_name=disk_name,
        device_name=actual_device_name,
    )
    return True


def delete_assistant_disk(assistant_id: str) -> bool:
    """Delete an assistant's persistent disk."""
    disk_name = _assistant_disk_name(assistant_id)

    attached_vm_name = find_vm_with_disk(assistant_id)
    if attached_vm_name:
        raise AssistantDiskInUseError(
            f"Assistant disk {disk_name} is still attached to {attached_vm_name}",
        )

    client = compute_v1.DisksClient()
    try:
        op = client.delete(
            project=SETTINGS.vm_project_id,
            zone=_current_vm_placement().zone,
            disk=disk_name,
        )
        op.result()
        logger.info(f"Deleted assistant disk: {disk_name}")
        return True
    except NotFound:
        logger.warning(f"Assistant disk {disk_name} not found")
        return False


def _assistant_archive_blob_names(assistant_id: str) -> list[str]:
    """GCS object names for an assistant's Local and desktop-profile archives."""
    return [
        f"{assistant_id}.tar.gz",
        f"{assistant_id}-desktop-profile.tar.gz",
    ]


def delete_assistant_pool_archive(assistant_id: str) -> dict:
    """Delete Local + desktop-profile GCS archives for an assistant.

    Returns a summary of which blobs were deleted. Missing blobs are
    treated as success (idempotent teardown).
    """
    import os
    import json as _json
    from google.cloud import storage
    from google.oauth2.service_account import Credentials

    deleted: list[str] = []
    missing: list[str] = []
    try:
        creds_json = os.getenv("GCP_SA_KEY")
        if creds_json:
            creds = Credentials.from_service_account_info(_json.loads(creds_json))
            client = storage.Client(credentials=creds)
        else:
            client = storage.Client()
        bucket = client.bucket(POOL_ASSISTANT_ARCHIVE_BUCKET)
        for name in _assistant_archive_blob_names(assistant_id):
            blob = bucket.blob(name)
            if blob.exists():
                blob.delete()
                deleted.append(name)
                logger.info(
                    "Deleted assistant archive gs://%s/%s",
                    POOL_ASSISTANT_ARCHIVE_BUCKET,
                    name,
                )
            else:
                missing.append(name)
    except Exception as exc:
        logger.warning(
            "Failed deleting assistant archives for %s: %s",
            assistant_id,
            exc,
        )
        raise

    return {
        "assistant_id": assistant_id,
        "bucket": POOL_ASSISTANT_ARCHIVE_BUCKET,
        "deleted": deleted,
        "missing": missing,
    }


def _detach_attached_assistant_disk(vm_name: str) -> tuple[bool, Optional[str]]:
    """Detach every assistant data disk still attached to this VM.

    A pool VM should hold at most one assistant disk, but a release that idles a
    VM without detaching its disk can leave the disk stranded; a later assignment
    then stacks a second assistant disk onto the same VM. Releasing such a VM
    must clear all of them so no assistant's binding release is left blocked by a
    disk that ``find_vm_with_disk`` keeps reporting as attached.
    """
    client = compute_v1.InstancesClient()
    vm = client.get(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        instance=vm_name,
    )
    attached_disks = [
        disk
        for disk in vm.disks or []
        if not getattr(disk, "boot", False)
        and "/disks/unity-disk-" in (getattr(disk, "source", "") or "")
    ]
    if not attached_disks:
        return False, None

    last_disk_name: Optional[str] = None
    for attached_disk in attached_disks:
        disk_name = (attached_disk.source or "").rsplit("/", 1)[-1]
        client.detach_disk(
            project=SETTINGS.vm_project_id,
            zone=_current_vm_placement().zone,
            instance=vm_name,
            device_name=attached_disk.device_name,
        ).result()
        last_disk_name = disk_name
        logger.info(
            "Detached assistant disk %s from %s during release completion",
            disk_name,
            vm_name,
        )
        _log_vm_pool_event(
            "detach_disk",
            vm_name=vm_name,
            disk_name=disk_name,
            device_name=attached_disk.device_name,
            reason="release_complete",
        )
    return True, last_disk_name


def reclaim_orphaned_assistant_disk(
    assistant_id: str,
    *,
    current_binding_id: str | None = None,
) -> Dict[str, Any]:
    """Detach this assistant's disk from a pool VM that no longer owns it.

    Release completion normally detaches the assistant data disk while the VM is
    still labelled ``releasing``. If a VM is returned to the idle pool (ownership
    labels cleared) before its disk was detached, the disk is stranded:
    ``find_vm_with_disk`` keeps reporting it, so the binding release can never
    complete and the assistant can never be woken again. This reclaims that
    stranded disk by detaching it directly. It never touches a VM that is
    actively ``assigned`` (in use by a live binding); those are handled by the
    normal assignment/release paths.
    """
    vm_name = find_vm_with_disk(assistant_id)
    if not vm_name:
        return {"detached": False, "reason": "no_attached_disk"}

    owner = _attached_disk_vm_state(vm_name)
    owner_role = str(owner.get("pool_role", "") or "")
    if owner_role == "assigned":
        return {"detached": False, "reason": "vm_assigned", "vm_name": vm_name}

    detached = detach_assistant_disk(vm_name, assistant_id)
    _log_vm_pool_event(
        "reclaim_orphaned_disk",
        assistant_id=assistant_id,
        binding_id=current_binding_id,
        vm_name=vm_name,
        pool_role=owner_role or None,
        owner_assistant_id=str(owner.get("assistant_id", "") or "") or None,
        owner_binding_id=str(owner.get("binding_id", "") or "") or None,
        detached=detached,
    )
    return {
        "detached": detached,
        "vm_name": vm_name,
        "pool_role": owner_role or None,
    }


def _update_instance_metadata(
    vm_name: str,
    updates: Dict[str, str],
    max_retries: int = 3,
    *,
    source: str | None = None,
    assistant_id: str | None = None,
    binding_id: str | None = None,
) -> None:
    """Update metadata on a running instance (merge with existing).

    Retries on PreconditionFailed (412) which occurs when the metadata
    fingerprint is stale due to a concurrent update.
    """
    client = compute_v1.InstancesClient()

    for attempt in range(max_retries + 1):
        instance = client.get(
            project=SETTINGS.vm_project_id,
            zone=_current_vm_placement().zone,
            instance=vm_name,
        )

        existing = {}
        if instance.metadata and instance.metadata.items:
            existing = {item.key: item.value for item in instance.metadata.items}

        existing.update(updates)

        items = [compute_v1.Items(key=k, value=v) for k, v in existing.items()]
        metadata = compute_v1.Metadata(
            items=items,
            fingerprint=instance.metadata.fingerprint if instance.metadata else None,
        )
        try:
            op = client.set_metadata(
                project=SETTINGS.vm_project_id,
                zone=_current_vm_placement().zone,
                instance=vm_name,
                metadata_resource=metadata,
            )
            op.result()
            refreshed = client.get(
                project=SETTINGS.vm_project_id,
                zone=_current_vm_placement().zone,
                instance=vm_name,
            )
            tracked_keys = sorted(
                set(
                    updates.keys()
                    | set(RELEASE_TRIGGER_METADATA_KEYS)
                    | {
                        ASSISTANT_ID_LABEL,
                        BINDING_ID_LABEL,
                        "disk-device",
                        RELEASE_GENERATION_METADATA_KEY,
                    },
                ),
            )
            logger.info(f"Updated metadata on {vm_name}: {list(updates.keys())}")
            _log_vm_pool_event(
                "metadata_update",
                vm_name=vm_name,
                assistant_id=assistant_id,
                binding_id=binding_id,
                source=source,
                metadata_keys=sorted(updates.keys()),
                metadata_actions=_metadata_update_actions(updates),
                metadata_presence={
                    key: bool(_read_instance_metadata(refreshed, key))
                    for key in tracked_keys
                },
            )
            return
        except PreconditionFailed:
            if attempt < max_retries:
                logger.info(
                    f"Metadata fingerprint conflict on {vm_name}, "
                    f"retrying ({attempt + 1}/{max_retries})",
                )
                continue
            raise


def _delete_pool_vm_instance(
    client,
    vm_name: str,
    *,
    vm_type: str | None = None,
) -> None:
    client.delete(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        instance=vm_name,
    ).result()
    _cleanup_deleted_pool_vm_network_resources(vm_name, vm_type=vm_type)


def _recycle_pool_vm_instance(client, vm, *, reason: str) -> str:
    labels = dict(vm.labels) if vm.labels else {}
    vm_type = labels.get("vm-type", "ubuntu")
    assistant_id = labels.get(ASSISTANT_ID_LABEL, "") or None
    contract_generation = labels.get(POOL_CONTRACT_GENERATION_LABEL, "") or None

    if assistant_id:
        restore_pool_static_ip_on_vm(vm.name, assistant_id, vm_type)
    _delete_pool_vm_instance(client, vm.name, vm_type=vm_type)
    _log_vm_pool_event(
        "contract_recycled",
        vm_name=vm.name,
        vm_type=vm_type,
        assistant_id=assistant_id,
        pool_role=labels.get(POOL_ROLE_LABEL, ""),
        status=getattr(vm, "status", ""),
        contract_generation=contract_generation,
        current_contract_generation=POOL_VM_CONTRACT_GENERATION,
        reason=reason,
    )
    logger.info("Recycled stale-contract VM %s (%s)", vm.name, reason)
    return f"Recycled stale-contract VM {vm.name}"


def _recycle_stale_pool_vms(vm_type: str) -> list[str]:
    client = compute_v1.InstancesClient()
    request = compute_v1.ListInstancesRequest(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        filter=f"labels.vm-type={vm_type}",
    )

    actions: list[str] = []
    for vm in client.list(request=request):
        labels = dict(vm.labels) if vm.labels else {}
        role = labels.get(POOL_ROLE_LABEL, "")
        if (
            not role
            or role not in RECYCLEABLE_STALE_POOL_ROLES
            or _has_current_pool_contract(vm)
        ):
            continue
        try:
            actions.append(
                _recycle_pool_vm_instance(
                    client,
                    vm,
                    reason="outdated_guest_contract",
                ),
            )
        except Exception as exc:
            logger.error("Failed to recycle stale-contract VM %s: %s", vm.name, exc)
    return actions


def _adopt_assigned_vm(
    *,
    assistant_id: str,
    binding_id: str,
    vm_type: str,
    owner: Dict[str, Any],
    attach_static_ip: bool,
    started_at: float,
) -> Dict[str, Any]:
    """Resume a torn assignment against the VM this binding already owns.

    The VM keeps the labels, attached disk, guest metadata and per-binding
    desktop secret written by the attempt that failed to persist its vmRef, so
    adopting recovers the warm desktop instead of burning a fresh boot cycle.
    The secret is read back from ``vnc-password`` metadata rather than re-minted:
    the guest already configured VNC with it, and rewriting metadata would not
    re-trigger the pool watcher (it wakes on a *changed* ``unify-key``).

    Static IP attachment is re-run because the failed attempt may have died
    before that stage, in which case the VM still answers on its pool hostname.

    An adopted VM whose guest never finished ``do_assign`` has no agent-service
    listening. That is not detected here — the controller's readiness poll and
    the guest-handshake deadline own it, and both outcomes beat deadlocking the
    binding against its own disk forever.
    """

    vm_name = str(owner["vm_name"])
    hostname = str(owner.get("hostname") or "")
    ip_address = owner.get("ip_address")
    if attach_static_ip:
        stable_network = attach_assistant_static_ip_to_pool_vm(
            vm_name,
            assistant_id,
            vm_type,
        )
        hostname = str(stable_network["hostname"])
        ip_address = stable_network["ip_address"]

    instance = compute_v1.InstancesClient().get(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        instance=vm_name,
    )
    desktop_secret = _read_instance_metadata(instance, "vnc-password") or ""
    if not hostname:
        hostname = _vm_ref_from_instance(instance)["hostname"]

    placement = _current_vm_placement()
    _log_vm_pool_event(
        "assign_adopted",
        assistant_id=assistant_id,
        binding_id=binding_id,
        vm_name=vm_name,
        vm_type=vm_type,
        hostname=hostname,
        desktop_secret_recovered=bool(desktop_secret),
        duration_ms=int((time.monotonic() - started_at) * 1000),
    )
    return {
        "vm_name": vm_name,
        "assistant_id": assistant_id,
        "binding_id": binding_id,
        "ip_address": ip_address,
        "hostname": hostname,
        "desktop_url": f"https://{hostname}",
        "desktop_secret": desktop_secret,
        "status": "RUNNING",
        "ssh_username": POOL_SSH_USERNAME,
        "ssh_port": SSH_SYNC_PORT,
        "pool_location": placement.location.id,
        "region": placement.region,
        "zone": placement.zone,
    }


def _assign_pool_vm(
    assistant_id: str,
    binding_id: str,
    unify_apikey: str,
    vm_type: str = "ubuntu",
    vm_number: int | None = None,
    attach_static_ip: bool = True,
) -> Dict[str, Any]:
    """Claim and configure exactly one VM for a specific binding."""
    started_at = time.monotonic()
    vm_name: str | None = None
    hostname: str | None = None
    current_stage = "acquire_assignment_lease"
    coord_api, namespace, lease_holder_id = _acquire_binding_vm_lease(
        binding_id,
        holder_prefix="vm-assign",
    )
    if coord_api is None:
        raise RuntimeError(
            f"Another VM assignment is already in progress for binding {binding_id}",
        )

    _log_vm_pool_event(
        "assign_started",
        assistant_id=assistant_id,
        binding_id=binding_id,
        vm_type=vm_type,
        vm_number=vm_number,
    )

    try:
        current_stage = "ensure_disk_ready"
        adoptable_owner = _run_vm_pool_stage(
            operation="assign",
            stage=current_stage,
            fn=lambda: _ensure_disk_ready_for_binding(assistant_id, binding_id),
            assistant_id=assistant_id,
            binding_id=binding_id,
            vm_type=vm_type,
        )
        if adoptable_owner is not None:
            current_stage = "adopt_assigned_vm"
            return _run_vm_pool_stage(
                operation="assign",
                stage=current_stage,
                fn=lambda: _adopt_assigned_vm(
                    assistant_id=assistant_id,
                    binding_id=binding_id,
                    vm_type=vm_type,
                    owner=adoptable_owner,
                    attach_static_ip=attach_static_ip,
                    started_at=started_at,
                ),
                assistant_id=assistant_id,
                binding_id=binding_id,
                vm_name=str(adoptable_owner.get("vm_name") or ""),
                vm_type=vm_type,
            )
        if attach_static_ip:
            current_stage = "reclaim_stale_assistant_ip_owner"
            _run_vm_pool_stage(
                operation="assign",
                stage=current_stage,
                fn=lambda: reclaim_stale_assistant_ip_owners(assistant_id),
                assistant_id=assistant_id,
                binding_id=binding_id,
                vm_type=vm_type,
            )
        current_stage = "claim_idle_vm"
        claimed = _run_vm_pool_stage(
            operation="assign",
            stage=current_stage,
            fn=lambda: claim_idle_vm(
                assistant_id,
                binding_id,
                vm_type,
                vm_number=vm_number,
            ),
            assistant_id=assistant_id,
            binding_id=binding_id,
            vm_type=vm_type,
            vm_number=vm_number,
        )
        vm_name = claimed["vm_name"]
        hostname = claimed["hostname"]
        if attach_static_ip:
            current_stage = "attach_assistant_static_ip"
            stable_network = _run_vm_pool_stage(
                operation="assign",
                stage=current_stage,
                fn=lambda: attach_assistant_static_ip_to_pool_vm(
                    vm_name,
                    assistant_id,
                    vm_type,
                ),
                assistant_id=assistant_id,
                binding_id=binding_id,
                vm_name=vm_name,
                vm_type=vm_type,
            )
            hostname = stable_network["hostname"]
            claimed["hostname"] = hostname
            claimed["ip_address"] = stable_network["ip_address"]
            claimed["desktop_url"] = f"https://{hostname}"
        current_stage = "create_assistant_disk"
        _run_vm_pool_stage(
            operation="assign",
            stage=current_stage,
            fn=lambda: create_assistant_disk(assistant_id),
            assistant_id=assistant_id,
            binding_id=binding_id,
            vm_name=vm_name,
            vm_type=vm_type,
        )
        current_stage = "attach_assistant_disk"
        device_name = _run_vm_pool_stage(
            operation="assign",
            stage=current_stage,
            fn=lambda: attach_assistant_disk(
                vm_name,
                assistant_id,
                binding_id=binding_id,
            ),
            assistant_id=assistant_id,
            binding_id=binding_id,
            vm_name=vm_name,
            vm_type=vm_type,
        )
        current_stage = "load_ssh_key"
        existing_key = _run_vm_pool_stage(
            operation="assign",
            stage=current_stage,
            fn=lambda: _fetch_existing_ssh_key(assistant_id),
            assistant_id=assistant_id,
            binding_id=binding_id,
            vm_name=vm_name,
            vm_type=vm_type,
        )
        if existing_key:
            private_key = existing_key
            public_key = _derive_public_key(existing_key)
        else:
            current_stage = "generate_ssh_keypair"
            private_key, public_key = _run_vm_pool_stage(
                operation="assign",
                stage=current_stage,
                fn=generate_ssh_keypair,
                assistant_id=assistant_id,
                binding_id=binding_id,
                vm_name=vm_name,
                vm_type=vm_type,
            )
            current_stage = "store_ssh_private_key"
            _run_vm_pool_stage(
                operation="assign",
                stage=current_stage,
                fn=lambda: store_ssh_private_key(assistant_id, private_key),
                assistant_id=assistant_id,
                binding_id=binding_id,
                vm_name=vm_name,
                vm_type=vm_type,
            )

        # VncAuth (DES-based) only compares the first 8 bytes of the password,
        # but mint a longer secret anyway since it also feeds HMAC/signed uses.
        desktop_secret = secrets.token_urlsafe(16)
        metadata = {
            "unify-key": unify_apikey,
            "vnc-password": desktop_secret,
            "ssh-public-key": public_key,
            "disk-device": device_name,
            "assistant-id": assistant_id,
            "binding-id": binding_id,
            "hostname": hostname,
            RELEASE_GENERATION_METADATA_KEY: "",
        }
        if vm_type == "windows" and MAK_KEY:
            metadata["office-mak-key"] = MAK_KEY
        current_stage = "update_instance_metadata"
        _run_vm_pool_stage(
            operation="assign",
            stage=current_stage,
            fn=lambda: _update_instance_metadata(
                vm_name,
                metadata,
                source="assign_pool_vm.assignment",
                assistant_id=assistant_id,
                binding_id=binding_id,
            ),
            assistant_id=assistant_id,
            binding_id=binding_id,
            vm_name=vm_name,
            vm_type=vm_type,
        )

        logger.info(
            f"Pool assignment complete: {vm_name} -> assistant {assistant_id}",
        )
        _log_vm_pool_event(
            "assign_complete",
            assistant_id=assistant_id,
            binding_id=binding_id,
            vm_name=vm_name,
            vm_type=vm_type,
            hostname=claimed["hostname"],
            disk_device=device_name,
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        return {
            "vm_name": vm_name,
            "assistant_id": assistant_id,
            "binding_id": binding_id,
            "ip_address": claimed["ip_address"],
            "hostname": claimed["hostname"],
            "desktop_url": claimed["desktop_url"],
            "desktop_secret": desktop_secret,
            "status": "RUNNING",
            "ssh_username": POOL_SSH_USERNAME,
            "ssh_port": SSH_SYNC_PORT,
            "pool_location": _current_vm_placement().location.id,
            "region": _current_vm_placement().region,
            "zone": _current_vm_placement().zone,
        }
    except Exception as exc:
        if vm_name and current_stage in {
            "attach_assistant_static_ip",
            "reclaim_stale_assistant_ip_owner",
        }:
            _set_pool_labels(
                compute_v1.InstancesClient(),
                vm_name,
                {
                    POOL_ROLE_LABEL: "idle",
                    ASSISTANT_ID_LABEL: "",
                    BINDING_ID_LABEL: "",
                },
                expected_role="assigned",
            )
        _log_vm_pool_event(
            "assign_failed",
            assistant_id=assistant_id,
            binding_id=binding_id,
            vm_name=vm_name,
            vm_hostname=hostname,
            vm_type=vm_type,
            vm_number=vm_number,
            failed_stage=current_stage,
            error_type=type(exc).__name__,
            error=str(exc),
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        raise
    finally:
        _release_binding_vm_lease(
            coord_api,
            binding_id,
            namespace,
            lease_holder_id,
        )


def assign_pool_vm(
    assistant_id: str,
    binding_id: str,
    unify_apikey: str,
    vm_type: str = "ubuntu",
    vm_number: int | None = None,
    *,
    placement: VmPlacement | None = None,
    attach_static_ip: bool = True,
) -> Dict[str, Any]:
    """Claim a VM within one explicit pool location."""
    # A demand-driven assignment is authoritative evidence that this location
    # is needed again. Clear the durable reaper fence before it can replenish
    # or claim capacity.
    clear_regional_pool_reaper_fence(placement)
    with vm_placement_scope(placement):
        return _assign_pool_vm(
            assistant_id=assistant_id,
            binding_id=binding_id,
            unify_apikey=unify_apikey,
            vm_type=vm_type,
            vm_number=vm_number,
            attach_static_ip=attach_static_ip,
        )


def has_assigned_vm(assistant_id: str) -> bool:
    """Check whether an assigned VM exists for this assistant.

    Lightweight read-only check — does not modify any state.
    """
    return get_assigned_vm_ref(assistant_id) is not None


def get_assigned_vm_ref(assistant_id: str) -> Optional[Dict[str, Any]]:
    """Return the currently assigned VM identity for an assistant, if any.

    Uses a label-filtered LIST which is eventually consistent. For
    correctness-critical decisions where the session already knows its
    vmRef, prefer ``verify_vm_assignment`` (strongly consistent GET).
    """
    client = compute_v1.InstancesClient()
    sanitized = assistant_id.lower().replace("_", "-")
    request = compute_v1.ListInstancesRequest(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        filter=f"labels.pool-role=assigned AND labels.assistant-id={sanitized}",
    )
    assigned = list(client.list(request=request))
    if not assigned:
        return None
    if len(assigned) > 1:
        logger.warning(
            "Multiple assigned VMs found for assistant %s: %s",
            assistant_id,
            [vm.name for vm in assigned],
        )
        _log_vm_pool_event(
            "multiple_assigned_vms",
            assistant_id=assistant_id,
            vm_names=[vm.name for vm in assigned],
        )
    vm = assigned[0]
    return _vm_ref_from_instance(vm)


def _verify_vm_assignment(
    vm_name: str,
    binding_id: str,
    assistant_id: str | None = None,
) -> Optional[Dict[str, Any]]:
    """Strongly consistent check: GET the named VM and verify binding ownership."""
    client = compute_v1.InstancesClient()
    binding_label = binding_id.lower().replace("_", "-")
    assistant_label = (
        assistant_id.lower().replace("_", "-") if assistant_id is not None else None
    )
    try:
        vm = client.get(
            project=SETTINGS.vm_project_id,
            zone=_current_vm_placement().zone,
            instance=vm_name,
        )
    except Exception:
        return None
    labels = dict(vm.labels) if vm.labels else {}
    if labels.get("pool-role") != "assigned":
        return None
    if labels.get(BINDING_ID_LABEL) != binding_label:
        return None
    if assistant_label is not None and labels.get("assistant-id") != assistant_label:
        return None
    return _vm_ref_from_instance(vm)


def verify_vm_assignment(
    vm_name: str,
    binding_id: str,
    assistant_id: str | None = None,
    *,
    placement: VmPlacement | None = None,
) -> Optional[Dict[str, Any]]:
    """Strongly verify one assignment in its persisted VM location."""
    with vm_placement_scope(placement):
        return _verify_vm_assignment(vm_name, binding_id, assistant_id)


def _vm_ref_from_instance(vm) -> Dict[str, Any]:
    labels = dict(vm.labels) if vm.labels else {}
    hostname = _read_instance_metadata(vm, "hostname")
    if not hostname:
        hostname_label = labels.get("pool-hostname", "")
        hostname = (
            hostname_label.replace("-", ".")
            if hostname_label
            else vm.name + f".{DOMAIN_SUFFIX}"
        )
    return {
        "name": vm.name,
        "hostname": hostname,
        "vmType": labels.get("vm-type", "ubuntu"),
        # This is routing metadata, not identity. Legacy bindings omit it and
        # remain valid while the configured Iowa pool is the only pool.
        "poolLocation": _current_vm_placement().region,
        "region": _current_vm_placement().region,
        "zone": _current_vm_placement().zone,
    }


def _is_job_non_terminal(job) -> bool:
    """Return True if the Job is still alive (non-terminal, not being deleted).

    Mirrors the controller's ``_bound_job_for_session`` semantics: a pod
    in restart-backoff temporarily has ``active == 0`` without any
    terminal condition.  Treating that as "no live job" would cause the
    orphan sweep to prematurely release the VM while the controller
    still considers the Job bound.
    """
    if job.metadata.deletion_timestamp:
        return False
    labels = job.metadata.labels or {}
    if labels.get("unity-status") == "done":
        return False
    for condition in job.status.conditions or []:
        if condition.type == "Failed" and condition.status == "True":
            return False
        if condition.type == "Complete" and condition.status == "True":
            return False
    return True


def reconcile_orphaned_vms(
    batch_api,
    vm_type: str = "ubuntu",
    *,
    all_regions: bool = False,
) -> Dict[str, Any]:
    """Release VMs assigned to assistants that no longer have live K8s Jobs.

    When a K8s pod crashes or is force-deleted, release_pool_vm is never
    called, leaving the VM stuck in pool-role=assigned. This reconciler
    detects such orphans by cross-referencing the K8s Job list and releases
    them back to the pool. A live job for a *different* binding does not keep
    an older binding alive: this matters when a session is re-created while
    its prior VM still owns the persistent disk.

    Uses the same non-terminal job semantics as the AssistantSession
    controller so that pods in restart-backoff are not mistaken for
    dead jobs.

    Idempotent and safe to call on a cron schedule.
    """
    if all_regions:
        by_location: dict[str, Dict[str, Any]] = {}
        for location_id, zone in SETTINGS.vm_provisioned_locations.items():
            placement = VmPlacement(
                location=get_pool_location(location_id),
                zone=zone,
                source_timezone="",
                resolution="orphan-reconcile",
            )
            with vm_placement_scope(placement):
                by_location[location_id] = reconcile_orphaned_vms(
                    batch_api,
                    vm_type,
                )
        return {
            "by_location": by_location,
            "checked": sum(item["checked"] for item in by_location.values()),
            "released": [
                row for item in by_location.values() for row in item["released"]
            ],
            "kept": [row for item in by_location.values() for row in item["kept"]],
        }

    client = compute_v1.InstancesClient()
    assigned_request = compute_v1.ListInstancesRequest(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        filter=f"labels.pool-role=assigned AND labels.vm-type={vm_type}",
    )
    releasing_request = compute_v1.ListInstancesRequest(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        filter=f"labels.pool-role={POOL_ROLE_RELEASING} AND labels.vm-type={vm_type}",
    )
    assigned_vms = list(client.list(request=assigned_request))
    releasing_vms = list(client.list(request=releasing_request))

    released = []
    kept = []
    recovered_releasing = []
    kept_releasing = []
    releasing_errors = []
    for vm in assigned_vms:
        aid = vm.labels.get("assistant-id", "")
        binding_id = vm.labels.get(BINDING_ID_LABEL, "")
        if not aid:
            continue

        try:
            jobs = batch_api.list_namespaced_job(
                namespace=SETTINGS.default_namespace,
                label_selector=f"app=unity,assistant-id={aid}",
            )
            live_jobs = [j for j in jobs.items if _is_job_non_terminal(j)]
        except Exception as e:
            logger.warning(
                "reconcile_orphaned_vms: failed to check jobs for %s: %s",
                aid,
                e,
            )
            continue

        normalized_binding_id = str(binding_id).lower().replace("_", "-")
        live_binding_ids = {
            str((job.metadata.labels or {}).get(BINDING_ID_LABEL, ""))
            .lower()
            .replace("_", "-")
            for job in live_jobs
            if (job.metadata.labels or {}).get(BINDING_ID_LABEL)
        }
        has_legacy_live_job = any(
            not (job.metadata.labels or {}).get(BINDING_ID_LABEL) for job in live_jobs
        )
        binding_is_live = has_legacy_live_job or (
            bool(normalized_binding_id) and normalized_binding_id in live_binding_ids
        )

        if not binding_is_live:
            logger.info(
                "Orphaned VM %s assigned to %s (binding %s has no live K8s Job) — releasing",
                vm.name,
                aid,
                binding_id,
            )
            try:
                if not binding_id:
                    logger.warning(
                        "Skipping orphan release for %s because binding-id is missing",
                        vm.name,
                    )
                    continue
                result = release_pool_vm(aid, binding_id, vm_name=vm.name)
                _replenish_after_retired_release(result)
                released.append(
                    {
                        "vm_name": vm.name,
                        "assistant_id": aid,
                        "binding_id": binding_id,
                        "pool_role": result.get("pool_role"),
                        "retired": bool(result.get("retired")),
                    },
                )
            except Exception as e:
                logger.error(
                    "Failed to release orphaned VM %s: %s",
                    vm.name,
                    e,
                )
        else:
            kept.append(
                {"vm_name": vm.name, "assistant_id": aid, "binding_id": binding_id},
            )

    now = datetime.now(timezone.utc)
    for vm in releasing_vms:
        try:
            refreshed = client.get(
                project=SETTINGS.vm_project_id,
                zone=_current_vm_placement().zone,
                instance=vm.name,
            )
            refreshed = _refresh_inflight_progress_phase(client, refreshed)
            if not _is_stale_inflight_vm(
                refreshed,
                now=now,
                timeout_seconds=POOL_RELEASE_TIMEOUT_SECONDS,
            ):
                kept_releasing.append({"vm_name": vm.name})
                continue

            labels = dict(refreshed.labels) if refreshed.labels else {}
            aid = labels.get(ASSISTANT_ID_LABEL, "")
            binding_id = labels.get(BINDING_ID_LABEL, "")
            if not aid or not binding_id:
                logger.warning(
                    "Skipping aged releasing VM %s because ownership labels are missing",
                    vm.name,
                )
                releasing_errors.append(
                    {
                        "vm_name": vm.name,
                        "reason": "missing_ownership_labels",
                    },
                )
                continue

            current_release_generation = _read_instance_release_generation(refreshed)
            result = recover_stuck_pool_vm_release(
                aid,
                binding_id,
                vm_name=vm.name,
                current_release_generation=current_release_generation,
                allow_rearm=(current_release_generation or 0) < MAX_RELEASE_GENERATION,
                retire_reason="aged_releasing_vm",
            )
            recovered_releasing.append(
                {
                    "vm_name": vm.name,
                    "assistant_id": aid,
                    "binding_id": binding_id,
                    "action": result.get("action"),
                    "release_generation": result.get("release_generation"),
                    "retired": bool(result.get("retired")),
                },
            )
        except Exception as e:
            logger.error("Failed to recover aged releasing VM %s: %s", vm.name, e)
            releasing_errors.append({"vm_name": vm.name, "error": str(e)})

    return {
        "checked": len(assigned_vms),
        "released": released,
        "kept": kept,
        "releasing_checked": len(releasing_vms),
        "releasing_recovered": recovered_releasing,
        "releasing_kept": kept_releasing,
        "releasing_errors": releasing_errors,
    }


def purge_quarantined_vms(vm_type: str = "ubuntu") -> Dict[str, Any]:
    """Delete quarantined VMs that are consuming resources without serving traffic.

    Quarantined VMs are already stopped (by _quarantine_pool_vm) and
    excluded from all pool operations. Without this purge they accumulate
    indefinitely, wasting GCE instance quota and boot-disk storage.

    Deleting a quarantined VM auto-deletes its boot disk. Any attached
    persistent disk (auto_delete=false) survives as an unattached disk and
    will be re-attached when the assistant's next session claims a fresh VM.

    Idempotent and safe to call on a cron schedule.
    """
    client = compute_v1.InstancesClient()
    request = compute_v1.ListInstancesRequest(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        filter=f"labels.pool-role=quarantined AND labels.vm-type={vm_type}",
    )
    quarantined = list(client.list(request=request))
    if not quarantined:
        return {"found": 0, "deleted": [], "errors": []}

    deleted: list[str] = []
    errors: list[dict] = []

    def _delete_one(vm) -> Optional[str]:
        try:
            _delete_pool_vm_instance(client, vm.name, vm_type=vm_type)
            _log_vm_pool_event(
                "quarantined_purged",
                vm_name=vm.name,
                vm_type=vm_type,
                status=vm.status,
            )
            logger.info("Purged quarantined VM %s", vm.name)
            return vm.name
        except Exception as exc:
            logger.error("Failed to delete quarantined VM %s: %s", vm.name, exc)
            errors.append({"vm_name": vm.name, "error": str(exc)})
            return None

    with ThreadPoolExecutor(
        max_workers=min(len(quarantined), 5),
        thread_name_prefix="quarantine-purge",
    ) as pool:
        for result in pool.map(_delete_one, quarantined):
            if result:
                deleted.append(result)

    return {"found": len(quarantined), "deleted": deleted, "errors": errors}


def cleanup_orphaned_pool_network_resources(vm_type: str) -> Dict[str, Any]:
    """Delete old reserved pool IPs and stale DNS for missing pool VMs.

    Reclaims network resources for the live env suffix and retired suffixes
    (e.g. ``-preview``), including historical name prefixes such as
    ``droid-pool-*``. Active foreign-env names (staging vs production) are
    left for that env's controller.
    """

    instance_client = compute_v1.InstancesClient()
    address_client = compute_v1.AddressesClient()
    request = compute_v1.ListInstancesRequest(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
    )
    existing_names = {vm.name for vm in instance_client.list(request=request)}
    cutoff = (
        datetime.now(timezone.utc).timestamp()
        - POOL_ORPHANED_NETWORK_RESOURCE_GRACE_SECONDS
    )

    candidates: list[dict[str, str]] = []
    for address in address_client.list(
        project=SETTINGS.vm_project_id,
        region=_current_vm_placement().region,
    ):
        ip_name = str(getattr(address, "name", "") or "")
        vm_name = _pool_vm_name_from_ip_name(ip_name, vm_type)
        if not vm_name:
            continue
        if str(getattr(address, "status", "") or "") != "RESERVED":
            continue
        if getattr(address, "users", None):
            continue
        if vm_name in existing_names:
            continue
        created_at = _parse_gce_timestamp(getattr(address, "creation_timestamp", None))
        if created_at and created_at.timestamp() >= cutoff:
            continue
        candidates.append(
            {
                "ip_name": ip_name,
                "vm_name": vm_name,
                "hostname": _pool_vm_hostname(vm_name, vm_type),
            },
        )

    deleted_addresses: list[str] = []
    deleted_dns: list[str] = []
    errors: list[dict[str, str]] = []
    actions: list[str] = []
    for candidate in candidates:
        hostname = candidate["hostname"]
        ip_name = candidate["ip_name"]
        vm_name = candidate["vm_name"]
        try:
            if _delete_dns_a_record(hostname):
                deleted_dns.append(hostname)
                actions.append(f"Deleted stale pool DNS {hostname}")
        except Exception as exc:
            logger.error("Failed to delete stale pool DNS %s: %s", hostname, exc)
            errors.append({"resource": hostname, "error": str(exc)})

        try:
            if _delete_pool_static_ip(ip_name):
                deleted_addresses.append(ip_name)
                actions.append(f"Deleted orphaned pool static IP {ip_name}")
        except Exception as exc:
            logger.error(
                "Failed to delete orphaned pool static IP %s: %s",
                ip_name,
                exc,
            )
            errors.append({"resource": ip_name, "error": str(exc)})

        _log_vm_pool_event(
            "orphaned_network_resource_reconciled",
            vm_name=vm_name,
            vm_type=vm_type,
            hostname=hostname,
            ip_name=ip_name,
            dns_deleted=hostname in deleted_dns,
            ip_deleted=ip_name in deleted_addresses,
        )

    return {
        "vm_type": vm_type,
        "found": len(candidates),
        "deleted_addresses": deleted_addresses,
        "deleted_dns": deleted_dns,
        "errors": errors,
        "actions": actions,
    }


def _assistant_ids_owning_addresses() -> set[str]:
    """Return every assistant ID that appears to own a regional address.

    Read from an aggregated list because assistant addresses are regional and
    the fleet spans regions, and by label *and* name because neither signal is
    complete on its own: rotation parks an assistant on an operation-scoped
    name that only labels tie back to it, while addresses reserved before label
    repair landed carry no labels and only their name identifies them.

    Every imprecision here is deliberately in the same direction. The caller
    only deletes a record whose assistant is *absent* from this set, so an
    over-broad membership — a rotation name parsing to something wider than the
    assistant, an ID colliding across environments — keeps a record that could
    have been reaped. It can never drop a record that is still in service.
    """

    client = compute_v1.AddressesClient()
    owned: set[str] = set()
    for _scope, scoped_list in client.aggregated_list(project=SETTINGS.vm_project_id):
        for address in getattr(scoped_list, "addresses", None) or []:
            labels = dict(getattr(address, "labels", None) or {})
            assistant = labels.get(ASSISTANT_STATIC_IP_ASSISTANT_LABEL, "")
            if (
                assistant
                and labels.get(ASSISTANT_STATIC_IP_MANAGED_BY_LABEL)
                == ASSISTANT_STATIC_IP_MANAGED_BY_VALUE
                and labels.get(ASSISTANT_STATIC_IP_OWNER_LABEL)
                == ASSISTANT_STATIC_IP_OWNER_VALUE
            ):
                owned.add(assistant)
            from_name = _assistant_id_from_address_name(
                str(getattr(address, "name", "") or ""),
            )
            if from_name:
                owned.add(from_name)
    return owned


def _assistant_id_from_address_name(address_name: str) -> Optional[str]:
    """Return the assistant component of an assistant-owned address name."""

    prefix = "unity-assistant-ip-"
    if not address_name.startswith(prefix):
        return None
    component = address_name[len(prefix) :]
    for suffix in ASSISTANT_RESOURCE_ENV_SUFFIXES:
        if component.endswith(suffix):
            component = component[: -len(suffix)]
            break
    return component or None


def _assistant_id_from_dns_record(fqdn: str) -> Optional[str]:
    """Return the owning assistant ID for one of this env's assistant records.

    Records belonging to another deploy environment — live or retired — resolve
    to ``None`` so they are left to that environment's controller.
    """

    match = ASSISTANT_DNS_RECORD_PATTERN.match(fqdn)
    if match is None:
        return None
    if (match.group("suffix") or "") != SETTINGS.env_suffix:
        return None
    return _assistant_static_ip_id_component(match.group("id"), max_length=63)


def cleanup_orphaned_assistant_dns_records(
    *,
    apply: bool = False,
    max_deletions: int = ORPHANED_ASSISTANT_DNS_MAX_DELETIONS,
) -> Dict[str, Any]:
    """Delete assistant A records whose owning address no longer exists.

    An assistant's record is created only after its address is reserved, so an
    address-less record cannot be an assignment in flight — it is the residue of
    a release that predates DNS teardown, or of one that died between its two
    deletes. ``apply`` defaults to reporting candidates without touching the
    zone, and ``max_deletions`` bounds one pass.
    """

    owned = _assistant_ids_owning_addresses()
    zone = dns.Client(project=SETTINGS.dns_project_id).zone(DNS_ZONE_NAME)
    candidates = []
    for record in zone.list_resource_record_sets():
        if record.record_type != "A":
            continue
        assistant_id = _assistant_id_from_dns_record(record.name)
        if assistant_id is None or assistant_id in owned:
            continue
        candidates.append(record)

    deletable = candidates[:max_deletions]
    hostnames = [record.name.rstrip(".") for record in candidates]
    deleted: list[str] = []
    errors: list[dict[str, str]] = []

    if apply:
        for start in range(0, len(deletable), ORPHANED_ASSISTANT_DNS_DELETE_CHUNK):
            chunk = deletable[start : start + ORPHANED_ASSISTANT_DNS_DELETE_CHUNK]
            changes = zone.changes()
            for record in chunk:
                changes.delete_record_set(record)
            chunk_hostnames = [record.name.rstrip(".") for record in chunk]
            try:
                changes.create()
            except Exception as exc:
                logger.error(
                    "Failed to delete orphaned assistant DNS records %s: %s",
                    ", ".join(chunk_hostnames),
                    exc,
                )
                errors.extend(
                    {"resource": hostname, "error": str(exc)}
                    for hostname in chunk_hostnames
                )
                continue
            deleted.extend(chunk_hostnames)
            logger.info(
                "Deleted orphaned assistant DNS records: %s",
                ", ".join(chunk_hostnames),
            )

    truncated = len(candidates) - len(deletable)
    if truncated:
        logger.warning(
            "Capped orphaned assistant DNS cleanup at %s of %s candidates",
            len(deletable),
            len(candidates),
        )
    _log_vm_pool_event(
        "orphaned_assistant_dns_reconciled",
        applied=apply,
        found=len(candidates),
        deleted=len(deleted),
        truncated=truncated,
        cleanup_errors=len(errors),
    )
    return {
        "applied": apply,
        "found": len(candidates),
        "candidates": hostnames,
        "deleted": deleted,
        "truncated": truncated,
        "errors": errors,
    }


def _release_pool_vm(
    assistant_id: str,
    binding_id: str,
    *,
    vm_name: str | None = None,
    release_generation: int | None = None,
) -> Dict[str, Any]:
    """Transition an assigned VM into guest-side release cleanup.

    Release is now asynchronous: the pool role moves from ``assigned`` to
    ``releasing`` immediately so the VM is no longer claimable, then the
    pool watcher finishes guest cleanup and calls back into Comms to detach
    the disk and mark the VM idle. Product callers should pass ``vm_name`` so
    stale cleanup paths cannot retarget a newer VM for the same assistant.
    ``release_generation`` makes repeated release requests observable even when
    the assignment keys are already cleared. VMs running an outdated guest
    contract are retired instead of being returned to service.
    """
    sanitized = assistant_id.lower().replace("_", "-")
    binding_label = binding_id.lower().replace("_", "-")
    requested_release_generation = _normalize_release_generation(release_generation)
    vm = None
    client = None
    started_at = time.monotonic()
    current_stage = "lookup_vm"
    coord_api = None
    lease_namespace = SETTINGS.default_namespace
    lease_holder_id = None

    _log_vm_pool_event(
        "release_started",
        assistant_id=assistant_id,
        binding_id=binding_id,
        vm_name=vm_name,
    )

    try:
        current_stage = "acquire_binding_lease"
        coord_api, lease_namespace, lease_holder_id = _acquire_binding_vm_lease(
            binding_id,
            holder_prefix="vm-release",
            wait_timeout_seconds=VM_BINDING_RELEASE_LEASE_WAIT_SECONDS,
        )
        if coord_api is None:
            _log_vm_pool_event(
                "release_skipped",
                assistant_id=assistant_id,
                binding_id=binding_id,
                vm_name=vm_name,
                reason="binding_operation_busy",
            )
            return {
                "released": False,
                "assistant_id": assistant_id,
                "binding_id": binding_id,
                "vm_name": vm_name,
                "reason": "binding_operation_busy",
                "message": "Another binding VM operation is still in progress",
            }
        client = compute_v1.InstancesClient()
        if vm_name:
            current_stage = "lookup_vm_by_name"
            try:
                candidate = client.get(
                    project=SETTINGS.vm_project_id,
                    zone=_current_vm_placement().zone,
                    instance=vm_name,
                )
            except NotFound:
                _log_vm_pool_event(
                    "release_skipped",
                    assistant_id=assistant_id,
                    vm_name=vm_name,
                    reason="vm_not_found",
                )
                return {
                    "released": False,
                    "assistant_id": assistant_id,
                    "vm_name": vm_name,
                    "message": "VM not found",
                }

            candidate_labels = dict(candidate.labels) if candidate.labels else {}
            candidate_role = candidate_labels.get(POOL_ROLE_LABEL, "")
            if (
                candidate_labels.get(ASSISTANT_ID_LABEL) != sanitized
                or candidate_labels.get(BINDING_ID_LABEL) != binding_label
                or candidate_role not in ("assigned", POOL_ROLE_RELEASING)
            ):
                _log_vm_pool_event(
                    "release_skipped",
                    assistant_id=assistant_id,
                    binding_id=binding_id,
                    vm_name=vm_name,
                    current_role=candidate_role or None,
                    current_assistant_id=candidate_labels.get(ASSISTANT_ID_LABEL)
                    or None,
                    current_binding_id=candidate_labels.get(BINDING_ID_LABEL) or None,
                    reason="vm_not_owned",
                )
                return {
                    "released": False,
                    "assistant_id": assistant_id,
                    "binding_id": binding_id,
                    "vm_name": vm_name,
                    "message": "VM is not currently owned by assistant",
                }
            vm = candidate
        else:
            current_stage = "lookup_vm_by_binding"
            label_filter = f"labels.{BINDING_ID_LABEL}={binding_label}"
            request = compute_v1.ListInstancesRequest(
                project=SETTINGS.vm_project_id,
                zone=_current_vm_placement().zone,
                filter=label_filter,
            )
            candidates = [
                candidate
                for candidate in client.list(request=request)
                if (dict(candidate.labels) if candidate.labels else {}).get(
                    POOL_ROLE_LABEL,
                )
                in ("assigned", POOL_ROLE_RELEASING)
            ]
            if not candidates:
                logger.info(
                    f"No pool VM assigned to assistant {assistant_id} — nothing to release",
                )
                _log_vm_pool_event(
                    "release_skipped",
                    assistant_id=assistant_id,
                    binding_id=binding_id,
                    reason="no_assigned_vm",
                )
                return {
                    "released": False,
                    "assistant_id": assistant_id,
                    "binding_id": binding_id,
                    "message": "No VM assigned",
                }

            vm = next(
                (
                    candidate
                    for candidate in candidates
                    if (dict(candidate.labels) if candidate.labels else {}).get(
                        POOL_ROLE_LABEL,
                    )
                    == "assigned"
                ),
                candidates[0],
            )

        while True:
            vm_name = vm.name
            labels = dict(vm.labels) if vm.labels else {}
            current_role = labels.get(POOL_ROLE_LABEL, "")
            vm_type = labels.get("vm-type", "ubuntu")
            current_release_generation = _read_instance_release_generation(vm)
            target_release_generation = (
                requested_release_generation
                or (
                    current_release_generation
                    if current_role == POOL_ROLE_RELEASING
                    else None
                )
                or 1
            )

            if not _has_current_pool_contract(vm):
                current_stage = "retire_stale_contract_vm"
                _recycle_pool_vm_instance(
                    client,
                    vm,
                    reason="assistant_release_with_stale_contract",
                )
                return {
                    "released": True,
                    "assistant_id": assistant_id,
                    "binding_id": binding_id,
                    "vm_name": vm_name,
                    "vm_type": vm_type,
                    "pool_role": "retired",
                    "retired": True,
                    "release_generation": target_release_generation,
                    "message": "Retired stale-contract VM",
                }

            if current_role == POOL_ROLE_RELEASING:
                resumed = False
                generation_changed = (
                    current_release_generation != target_release_generation
                )
                release_metadata_present = _release_metadata_still_present(vm)
                _log_vm_pool_event(
                    "release_resume_check",
                    assistant_id=assistant_id,
                    binding_id=binding_id,
                    vm_name=vm_name,
                    current_release_generation=current_release_generation,
                    target_release_generation=target_release_generation,
                    release_metadata_present=release_metadata_present,
                )
                if generation_changed or release_metadata_present:
                    current_stage = "refresh_releasing_epoch"
                    _refresh_releasing_progress_epoch(client, vm_name)
                    current_stage = "resume_release_metadata"
                    _update_instance_metadata(
                        vm_name,
                        _release_metadata_updates(
                            clear_assignment=False,
                            release_generation=target_release_generation,
                        ),
                        source=(
                            "release_pool_vm.rearm"
                            if generation_changed
                            else "release_pool_vm.resume"
                        ),
                        assistant_id=assistant_id,
                        binding_id=binding_id,
                    )
                    resumed = True
                    _log_vm_pool_event(
                        "release_rearmed" if generation_changed else "release_resumed",
                        assistant_id=assistant_id,
                        binding_id=binding_id,
                        vm_name=vm_name,
                        previous_release_generation=current_release_generation,
                        release_generation=target_release_generation,
                    )
                logger.info("Release already in progress for pool VM %s", vm_name)
                return {
                    "released": resumed,
                    "assistant_id": assistant_id,
                    "binding_id": binding_id,
                    "vm_name": vm_name,
                    "vm_type": vm_type,
                    "pool_role": POOL_ROLE_RELEASING,
                    "release_generation": target_release_generation,
                    "rearmed": generation_changed and resumed,
                    "message": (
                        "Release re-armed"
                        if generation_changed and resumed
                        else "Release already in progress"
                    ),
                }

            current_stage = "mark_releasing"
            updated = _set_pool_labels(
                client,
                vm_name,
                {POOL_ROLE_LABEL: POOL_ROLE_RELEASING},
                expected_role="assigned",
            )
            if not updated:
                refreshed = client.get(
                    project=SETTINGS.vm_project_id,
                    zone=_current_vm_placement().zone,
                    instance=vm_name,
                )
                refreshed_labels = dict(refreshed.labels) if refreshed.labels else {}
                refreshed_role = refreshed_labels.get(POOL_ROLE_LABEL, "")
                if (
                    refreshed_role == POOL_ROLE_RELEASING
                    and refreshed_labels.get(ASSISTANT_ID_LABEL) == sanitized
                    and refreshed_labels.get(BINDING_ID_LABEL) == binding_label
                ):
                    vm = refreshed
                    continue
                return {
                    "released": False,
                    "assistant_id": assistant_id,
                    "binding_id": binding_id,
                    "vm_name": vm_name,
                    "vm_type": vm_type,
                    "message": "VM role changed before release could start",
                }

            current_stage = "update_release_metadata"
            _update_instance_metadata(
                vm_name,
                _release_metadata_updates(
                    clear_assignment=False,
                    release_generation=target_release_generation,
                ),
                source="release_pool_vm.request",
                assistant_id=assistant_id,
                binding_id=binding_id,
            )

            logger.info(
                "Release requested for pool VM %s from assistant %s",
                vm_name,
                assistant_id,
            )
            _log_vm_pool_event(
                "release_requested",
                assistant_id=assistant_id,
                binding_id=binding_id,
                vm_name=vm_name,
                release_generation=target_release_generation,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )

            return {
                "released": True,
                "assistant_id": assistant_id,
                "binding_id": binding_id,
                "vm_name": vm_name,
                "vm_type": vm_type,
                "pool_role": POOL_ROLE_RELEASING,
                "release_generation": target_release_generation,
            }
    except Exception as exc:
        _log_vm_pool_event(
            "release_failed",
            assistant_id=assistant_id,
            binding_id=binding_id,
            vm_name=vm_name,
            failed_stage=current_stage,
            error_type=type(exc).__name__,
            error=str(exc),
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        raise
    finally:
        _release_binding_vm_lease(
            coord_api,
            binding_id,
            lease_namespace,
            lease_holder_id,
        )


def release_pool_vm(
    assistant_id: str,
    binding_id: str,
    *,
    vm_name: str | None = None,
    release_generation: int | None = None,
    placement: VmPlacement | None = None,
) -> Dict[str, Any]:
    """Release a VM in the location recorded by its binding."""
    with vm_placement_scope(placement):
        return _release_pool_vm(
            assistant_id,
            binding_id,
            vm_name=vm_name,
            release_generation=release_generation,
        )


def _replenish_after_retired_release(result: Dict[str, Any]) -> bool:
    """Backfill pool capacity after a release path retires a VM."""

    if not result.get("retired"):
        return False
    replenish_pool(str(result.get("vm_type", "ubuntu") or "ubuntu"))
    result["replenished"] = True
    return True


def retire_pool_vm_release(
    assistant_id: str,
    binding_id: str,
    *,
    vm_name: str,
    reason: str,
) -> Dict[str, Any]:
    """Retire a VM that is stuck during release instead of returning it to idle."""

    sanitized = assistant_id.lower().replace("_", "-")
    binding_label = binding_id.lower().replace("_", "-")
    client = None
    coord_api = None
    lease_namespace = SETTINGS.default_namespace
    lease_holder_id = None
    started_at = time.monotonic()
    current_stage = "acquire_binding_lease"

    _log_vm_pool_event(
        "release_retire_started",
        assistant_id=assistant_id,
        binding_id=binding_id,
        vm_name=vm_name,
        reason=reason,
    )

    try:
        coord_api, lease_namespace, lease_holder_id = _acquire_binding_vm_lease(
            binding_id,
            holder_prefix="vm-release-retire",
            wait_timeout_seconds=VM_BINDING_RELEASE_LEASE_WAIT_SECONDS,
        )
        if coord_api is None:
            return {
                "released": False,
                "assistant_id": assistant_id,
                "binding_id": binding_id,
                "vm_name": vm_name,
                "reason": "binding_operation_busy",
                "message": "Another binding VM operation is still in progress",
            }

        current_stage = "load_vm"
        client = compute_v1.InstancesClient()
        try:
            vm = client.get(
                project=SETTINGS.vm_project_id,
                zone=_current_vm_placement().zone,
                instance=vm_name,
            )
        except NotFound:
            return {
                "released": False,
                "assistant_id": assistant_id,
                "binding_id": binding_id,
                "vm_name": vm_name,
                "skipped": True,
                "reason": "vm_not_found",
            }

        labels = dict(vm.labels) if vm.labels else {}
        current_role = labels.get(POOL_ROLE_LABEL, "")
        current_assistant_id = labels.get(ASSISTANT_ID_LABEL, "")
        current_binding_id = labels.get(BINDING_ID_LABEL, "")
        vm_type = labels.get("vm-type", "ubuntu")

        if (
            current_binding_id != binding_label
            or (current_assistant_id and current_assistant_id != sanitized)
            or current_role not in RETIRABLE_RELEASE_ROLES
        ):
            _log_vm_pool_event(
                "release_retire_skipped",
                assistant_id=assistant_id,
                binding_id=binding_id,
                vm_name=vm_name,
                vm_type=vm_type,
                current_role=current_role or None,
                current_assistant_id=current_assistant_id or None,
                current_binding_id=current_binding_id or None,
                reason="vm_not_owned",
            )
            return {
                "released": False,
                "assistant_id": assistant_id,
                "binding_id": binding_id,
                "vm_name": vm_name,
                "vm_type": vm_type,
                "pool_role": current_role,
                "current_assistant_id": current_assistant_id or None,
                "current_binding_id": current_binding_id or None,
                "skipped": True,
                "reason": "vm_not_owned",
            }

        current_stage = "retire_vm"
        _recycle_pool_vm_instance(client, vm, reason=reason)
        result = {
            "released": True,
            "assistant_id": assistant_id,
            "binding_id": binding_id,
            "vm_name": vm_name,
            "vm_type": vm_type,
            "pool_role": "retired",
            "retired": True,
            "release_generation": _read_instance_release_generation(vm),
            "message": "Retired stuck release VM",
            "retire_reason": reason,
        }
        _log_vm_pool_event(
            "release_retired",
            assistant_id=assistant_id,
            binding_id=binding_id,
            vm_name=vm_name,
            vm_type=vm_type,
            previous_pool_role=current_role or None,
            reason=reason,
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        _replenish_after_retired_release(result)
        return result
    except Exception as exc:
        _log_vm_pool_event(
            "release_retire_failed",
            assistant_id=assistant_id,
            binding_id=binding_id,
            vm_name=vm_name,
            failed_stage=current_stage,
            error_type=type(exc).__name__,
            error=str(exc),
            reason=reason,
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        raise
    finally:
        _release_binding_vm_lease(
            coord_api,
            binding_id,
            lease_namespace,
            lease_holder_id,
        )


def recover_stuck_pool_vm_release(
    assistant_id: str,
    binding_id: str,
    *,
    vm_name: str,
    current_release_generation: int | None,
    allow_rearm: bool,
    retire_reason: str,
) -> Dict[str, Any]:
    """Recover a timed-out release by re-arming once, then retiring the VM."""

    if allow_rearm:
        next_release_generation = (
            _normalize_release_generation(current_release_generation) or 0
        ) + 1
        result = release_pool_vm(
            assistant_id,
            binding_id,
            vm_name=vm_name,
            release_generation=next_release_generation,
        )
        _replenish_after_retired_release(result)
        result["action"] = "retired" if result.get("retired") else "rearmed"
        return result

    result = retire_pool_vm_release(
        assistant_id,
        binding_id,
        vm_name=vm_name,
        reason=retire_reason,
    )
    result["action"] = "retired" if result.get("retired") else "skipped"
    return result


def _complete_pool_vm_release(vm_name: str, binding_id: str) -> Dict[str, Any]:
    """Finalize release by idling current VMs or retiring stale-contract ones."""
    client = compute_v1.InstancesClient()
    started_at = time.monotonic()
    current_stage = "load_vm"
    _log_vm_pool_event(
        "release_complete_started",
        vm_name=vm_name,
        binding_id=binding_id,
    )
    try:
        vm = client.get(
            project=SETTINGS.vm_project_id,
            zone=_current_vm_placement().zone,
            instance=vm_name,
        )
        labels = dict(vm.labels) if vm.labels else {}
        current_role = labels.get(POOL_ROLE_LABEL, "")
        assistant_id = labels.get(ASSISTANT_ID_LABEL, "")
        current_binding_id = labels.get(BINDING_ID_LABEL, "")
        binding_label = binding_id.lower().replace("_", "-")
        vm_type = labels.get("vm-type", "ubuntu")

        if vm.status != "RUNNING" and current_role == POOL_ROLE_RELEASING:
            current_stage = "detach_assistant_disk_from_stopped_vm"
            detached, disk_name = _detach_attached_assistant_disk(vm_name)
            current_stage = "retire_stopped_releasing_vm"
            _delete_pool_vm_instance(client, vm_name, vm_type=vm_type)
            _log_vm_pool_event(
                "release_complete_stopped_vm",
                assistant_id=assistant_id or None,
                binding_id=current_binding_id or None,
                vm_name=vm_name,
                disk_name=disk_name,
                detached=detached,
            )
            return {
                "vm_name": vm_name,
                "vm_type": vm_type,
                "pool_role": "retired",
                "assistant_id": assistant_id or None,
                "binding_id": current_binding_id or None,
                "disk_name": disk_name,
                "detached": detached,
                "retired": True,
            }
        if vm.status != "RUNNING":
            return {
                "vm_name": vm_name,
                "status": vm.status,
                "pool_role": current_role,
                "skipped": True,
                "reason": "vm_not_running",
            }
        if current_role != POOL_ROLE_RELEASING:
            if current_role == "idle" and not assistant_id and not current_binding_id:
                return {
                    "vm_name": vm_name,
                    "vm_type": vm_type,
                    "pool_role": current_role,
                    "assistant_id": None,
                    "binding_id": None,
                    "already_released": True,
                }
            return {
                "vm_name": vm_name,
                "pool_role": current_role,
                "assistant_id": assistant_id or None,
                "binding_id": current_binding_id or None,
                "skipped": True,
                "reason": "not_releasing",
            }
        if current_binding_id != binding_label:
            return {
                "vm_name": vm_name,
                "pool_role": current_role,
                "binding_id": current_binding_id or None,
                "skipped": True,
                "reason": "binding_changed",
            }

        current_stage = "detach_assistant_disk"
        detached, disk_name = _detach_attached_assistant_disk(vm_name)
        current_stage = "restore_pool_static_ip"
        restore_pool_static_ip_on_vm(vm_name, assistant_id, vm_type)
        current_stage = "clear_assignment_metadata"
        release_metadata = _release_metadata_updates(clear_assignment=True)
        release_metadata["hostname"] = _pool_vm_hostname(vm_name, vm_type)
        _update_instance_metadata(
            vm_name,
            release_metadata,
            source="complete_pool_vm_release.clear_assignment",
            assistant_id=assistant_id or None,
            binding_id=current_binding_id or None,
        )

        if not _has_current_pool_contract(vm):
            current_stage = "retire_stale_contract_vm"
            _recycle_pool_vm_instance(
                client,
                vm,
                reason="release_complete_with_stale_contract",
            )
            return {
                "vm_name": vm_name,
                "vm_type": vm_type,
                "pool_role": "retired",
                "assistant_id": assistant_id or None,
                "binding_id": current_binding_id or None,
                "disk_name": disk_name,
                "detached": detached,
                "retired": True,
            }

        current_stage = "mark_idle"
        updated = _set_pool_labels(
            client,
            vm_name,
            {
                POOL_ROLE_LABEL: "idle",
                ASSISTANT_ID_LABEL: "",
                BINDING_ID_LABEL: "",
            },
            expected_role=POOL_ROLE_RELEASING,
        )
        if not updated:
            refreshed = client.get(
                project=SETTINGS.vm_project_id,
                zone=_current_vm_placement().zone,
                instance=vm_name,
            )
            return {
                "vm_name": vm_name,
                "pool_role": (dict(refreshed.labels) if refreshed.labels else {}).get(
                    POOL_ROLE_LABEL,
                    "",
                ),
                "skipped": True,
                "reason": "role_changed",
            }

        _log_vm_pool_event(
            "release_complete",
            assistant_id=assistant_id or None,
            binding_id=current_binding_id or None,
            vm_name=vm_name,
            disk_name=disk_name,
            detached=detached,
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        return {
            "vm_name": vm_name,
            "vm_type": vm_type,
            "pool_role": "idle",
            "assistant_id": assistant_id or None,
            "binding_id": current_binding_id or None,
            "disk_name": disk_name,
            "detached": detached,
        }
    except Exception as exc:
        _log_vm_pool_event(
            "release_complete_failed",
            vm_name=vm_name,
            binding_id=binding_id,
            failed_stage=current_stage,
            error_type=type(exc).__name__,
            error=str(exc),
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
        raise


def complete_pool_vm_release(
    vm_name: str,
    binding_id: str,
    *,
    placement: VmPlacement | None = None,
) -> Dict[str, Any]:
    """Finalize release in a persisted or discovered pool location."""
    resolved = placement or _placement_for_vm_name(vm_name)
    with vm_placement_scope(resolved):
        return _complete_pool_vm_release(vm_name, binding_id)


def _list_pool_state(vm_type: str):
    """Snapshot current pool state for a VM type.

    Returns (client, pool_vms, idle_vms, stopped_vms, in_flight_vms, existing_names).
    in_flight_vms are VMs that are transitioning back toward service but not yet
    claimable (provisioning, starting, or releasing).
    """
    client = compute_v1.InstancesClient()
    type_filter = (
        f"labels.vm-type={vm_type} "
        f"AND labels.environment={_current_pool_environment_label()}"
    )
    request = compute_v1.ListInstancesRequest(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        filter=type_filter,
    )
    all_vms = list(client.list(request=request))
    all_pool_vms = [
        vm
        for vm in all_vms
        if vm.labels
        and vm.labels.get("pool-role")
        and _is_current_environment_pool_vm(vm, vm_type)
    ]
    pool_vms = [vm for vm in all_pool_vms if _has_current_pool_contract(vm)]
    idle_vms = [
        vm
        for vm in pool_vms
        if vm.labels.get("pool-role") == "idle" and vm.status == "RUNNING"
    ]
    stopped_vms = [
        vm
        for vm in pool_vms
        if vm.labels.get("pool-role") == "stopped" and vm.status == "TERMINATED"
    ]
    in_flight_vms = [
        vm
        for vm in pool_vms
        if vm.status in ("STAGING", "RUNNING")
        and vm.labels.get("pool-role")
        in ("provisioning", "starting", POOL_ROLE_RELEASING)
    ]
    existing_names = {vm.name for vm in all_pool_vms}
    return client, pool_vms, idle_vms, stopped_vms, in_flight_vms, existing_names


def _start_one_stopped_vm(client, vm) -> bool:
    """Start a single stopped VM.

    Sets pool-role=starting BEFORE issuing client.start() so the scrub
    function (which targets pool-role=stopped + RUNNING) cannot kill a
    VM that is legitimately booting. The startup script transitions
    starting → idle via mark-idle once boot completes.

    Refreshes the boot metadata before starting, since the previous boot's
    startup script wipes the sensitive entries once it no longer needs them.
    """
    vm_type = (dict(vm.labels) if vm.labels else {}).get("vm-type", "ubuntu")
    try:
        if not _has_current_pool_contract(vm):
            _recycle_pool_vm_instance(
                client,
                vm,
                reason="start_requested_with_stale_contract",
            )
            return False

        ok = _set_pool_labels(
            client,
            vm.name,
            {"pool-role": "starting"},
            expected_role="stopped",
        )
        if not ok:
            logger.info(f"Replenish: {vm.name} label CAS failed (already claimed?)")
            _log_vm_pool_event(
                "replenish_start_skipped",
                vm_name=vm.name,
                vm_type=(dict(vm.labels) if vm.labels else {}).get("vm-type", "ubuntu"),
                reason="label_cas_failed",
            )
            return False

        _update_instance_metadata(
            vm.name,
            _pool_bootstrap_metadata_updates(vm.name, vm_type),
        )

        op = client.start(
            project=SETTINGS.vm_project_id,
            zone=_current_vm_placement().zone,
            instance=vm.name,
        )
        logger.info(
            "Replenish: start request submitted for stopped VM %s (pool-role=starting)",
            vm.name,
        )
        _log_vm_pool_event(
            "replenish_start",
            vm_name=vm.name,
            vm_type=vm_type,
            operation_name=getattr(op, "name", None),
        )
        return True
    except Exception as e:
        logger.error(f"Replenish: failed to start {vm.name}: {e}")
        _set_pool_labels(client, vm.name, {"pool-role": "stopped"})
        _log_vm_pool_event(
            "replenish_start_failed",
            vm_name=vm.name,
            vm_type=vm_type,
            error=str(e),
        )
        return False


def start_pool_vm(vm_type: str, vm_number: int) -> Dict[str, Any]:
    """Start a specific stopped pool VM by type and number.

    Uses the same stopped → starting label transition as
    _start_one_stopped_vm so the VM is protected from scrub during
    boot and can legitimately transition to idle via mark-idle. If the
    stopped VM is on an outdated guest contract, it is replaced with a
    freshly provisioned VM instead of being started.
    """
    vm_name = _pool_vm_name(vm_type, vm_number)
    client = compute_v1.InstancesClient()

    try:
        vm = client.get(
            project=SETTINGS.vm_project_id,
            zone=_current_vm_placement().zone,
            instance=vm_name,
        )
    except NotFound:
        raise ValueError(f"VM {vm_name} not found")

    if vm.status != "TERMINATED":
        raise Conflict(f"VM {vm_name} is {vm.status}, expected TERMINATED")

    if not _has_current_pool_contract(vm):
        _recycle_pool_vm_instance(
            client,
            vm,
            reason="manual_start_with_stale_contract",
        )
        provision_pool_vm(vm_type, vm_number)
        return {"vm_name": vm_name, "status": "provisioning", "recycled": True}

    ok = _set_pool_labels(
        client,
        vm_name,
        {"pool-role": "starting"},
        expected_role="stopped",
    )
    if not ok:
        raise Conflict(
            f"VM {vm_name} label CAS failed (pool-role is not stopped)",
        )

    _update_instance_metadata(
        vm_name,
        _pool_bootstrap_metadata_updates(vm_name, vm_type),
    )

    try:
        op = client.start(
            project=SETTINGS.vm_project_id,
            zone=_current_vm_placement().zone,
            instance=vm_name,
        )
        op.result()
    except Exception:
        _set_pool_labels(client, vm_name, {"pool-role": "stopped"})
        raise

    logger.info(f"Manual start: started VM {vm_name} (pool-role=starting)")
    _log_vm_pool_event(
        "manual_start",
        vm_name=vm_name,
        vm_type=vm_type,
    )
    return {"vm_name": vm_name, "status": "starting"}


def replenish_pool(vm_type: str, extra_demand: int = 0) -> Dict[str, Any]:
    """Start or provision VMs to meet current demand.

    Demand-aware: computes deficit from the number of threads currently
    waiting in claim_idle_vm, not just POOL_TARGET_IDLE.  Subtracts VMs
    already booting (in-flight) to avoid runaway over-provisioning across
    sequential replenish cycles.

    extra_demand compensates for VMs just claimed whose label change may
    not yet be reflected in the eventually-consistent GCE instances.list.

    Uses a non-blocking per-vm_type lock so concurrent callers (fire-and-
    forget from assign_pool_endpoint, poll-driven from claim_idle_vm) don't
    duplicate work.  Starts and provisions are parallelised via a thread pool.
    """
    if _regional_pool_reaper_fenced():
        logger.info(
            "Skipping replenish for fenced regional pool %s",
            _current_vm_placement().region,
        )
        return {
            "vm_type": vm_type,
            "actions": [],
            "skipped": True,
            "fenced": True,
        }

    lock = _get_replenish_lock(vm_type)
    if not lock.acquire(blocking=False):
        return {"vm_type": vm_type, "actions": [], "skipped": True}

    try:
        return _replenish_pool_inner(vm_type, extra_demand)
    finally:
        lock.release()


def _probe_and_quarantine_unhealthy_idle_vms(vm_type: str) -> list:
    """Probe idle VMs through Caddy to the agent-service and quarantine failures.

    Hits ``/api/sessions`` via the Caddy reverse proxy — a 401 from the
    agent-service auth middleware proves the process is alive; a 502 from
    Caddy or a connection error means the VM is broken.  Runs alongside
    quarantine/scrub in the replenish cycle so that broken idle VMs are
    removed from the claimable pool before any session can pick them up.
    """
    client = compute_v1.InstancesClient()
    request = compute_v1.ListInstancesRequest(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        filter=(
            f"labels.pool-role=idle AND labels.vm-type={vm_type} "
            f"AND labels.environment={_current_pool_environment_label()} "
            f"AND labels.{POOL_CONTRACT_GENERATION_LABEL}={POOL_VM_CONTRACT_GENERATION} "
            "AND status=RUNNING"
        ),
    )
    idle_vms = [
        vm
        for vm in client.list(request=request)
        if _is_current_environment_pool_vm(vm, vm_type)
    ]
    if not idle_vms:
        return []

    actions: list[str] = []

    def _check_one(vm) -> Optional[str]:
        ref = _vm_ref_from_instance(vm)
        hostname = ref.get("hostname", "")
        if not hostname:
            return None
        if probe_vm_agent_service(hostname, timeout=2.0):
            return None
        _log_vm_pool_event(
            "idle_probe_failed",
            vm_name=vm.name,
            vm_type=vm_type,
            hostname=hostname,
        )
        return _quarantine_pool_vm(
            client,
            vm,
            reason=f"idle health probe failed ({hostname})",
        )

    with ThreadPoolExecutor(
        max_workers=min(len(idle_vms), 5),
        thread_name_prefix="idle-probe",
    ) as pool:
        for result in pool.map(_check_one, idle_vms):
            if result:
                actions.append(result)

    return actions


def _replenish_pool_inner(vm_type: str, extra_demand: int = 0) -> Dict[str, Any]:
    placement = _current_vm_placement()
    actions = _recycle_stale_pool_vms(vm_type)
    actions.extend(_quarantine_stale_inflight_vms(vm_type))
    actions.extend(_scrub_inconsistent_vms(vm_type))

    # Keep hot-path replenish focused on restoring capacity. Bulk health sweeps
    # over the entire idle pool can quarantine every candidate at once and
    # temporarily collapse availability faster than replacements can boot.
    # Claim-time probing still rejects unhealthy VMs before assignment.

    client, _, idle_vms, stopped_vms, in_flight_vms, existing_names = _list_pool_state(
        vm_type,
    )

    with _pending_lock:
        pending = _pending_claims.get(_pool_scope_key(vm_type), 0)

    target = max(POOL_TARGET_IDLE, pending)
    deficit = target - len(idle_vms) - len(in_flight_vms) + extra_demand

    logger.info(
        f"Replenish {vm_type}: target={target} idle={len(idle_vms)} "
        f"in_flight={len(in_flight_vms)} stopped={len(stopped_vms)} "
        f"extra_demand={extra_demand} deficit={deficit}",
    )
    _log_vm_pool_event(
        "replenish_decision",
        vm_type=vm_type,
        target=target,
        idle=len(idle_vms),
        in_flight=len(in_flight_vms),
        stopped=len(stopped_vms),
        extra_demand=extra_demand,
        deficit=deficit,
    )

    if deficit <= 0:
        return {"vm_type": vm_type, "idle_count": len(idle_vms), "actions": actions}

    # Decide what to start vs provision
    vms_to_start = stopped_vms[:deficit]
    remaining_deficit = deficit - len(vms_to_start)

    numbers_to_provision: list[int] = []
    n = 1
    for _ in range(remaining_deficit):
        while _pool_vm_name(vm_type, n) in existing_names:
            n += 1
        numbers_to_provision.append(n)
        existing_names.add(_pool_vm_name(vm_type, n))
        n += 1

    # Run starts and provisions in parallel
    with ThreadPoolExecutor(
        max_workers=max(len(vms_to_start) + len(numbers_to_provision), 1),
        thread_name_prefix="replenish",
    ) as pool:
        futures = {}
        for vm in vms_to_start:
            f = pool.submit(
                _run_in_vm_placement,
                placement,
                _start_one_stopped_vm,
                client,
                vm,
            )
            futures[f] = f"Started stopped VM {vm.name}"
        for num in numbers_to_provision:
            f = pool.submit(
                _run_in_vm_placement,
                placement,
                provision_pool_vm,
                vm_type,
                num,
            )
            futures[f] = f"Provisioned new pool VM #{num}"

        started_count = 0
        for f in as_completed(futures):
            desc = futures[f]
            try:
                result = f.result()
                if result is not False:
                    actions.append(desc)
                    if desc.startswith("Started"):
                        started_count += 1
            except Conflict:
                logger.info(f"Replenish: conflict — {desc}, skipping")
            except Exception as e:
                logger.error(f"Replenish: failed — {desc}: {e}")

    # Replenish the stopped reserve if we consumed any
    if started_count > 0:
        _, _, _, stopped_vms_now, _, existing_names_now = _list_pool_state(vm_type)
        reserve_deficit = POOL_TARGET_STOPPED - len(stopped_vms_now)
        if reserve_deficit > 0:
            with ThreadPoolExecutor(
                max_workers=reserve_deficit,
                thread_name_prefix="replenish-reserve",
            ) as pool:
                reserve_futures = {}
                nr = 1
                for _ in range(reserve_deficit):
                    while _pool_vm_name(vm_type, nr) in existing_names_now:
                        nr += 1
                    rf = pool.submit(
                        _run_in_vm_placement,
                        placement,
                        provision_pool_vm,
                        vm_type,
                        nr,
                    )
                    reserve_futures[rf] = nr
                    existing_names_now.add(_pool_vm_name(vm_type, nr))
                    nr += 1

                for rf in as_completed(reserve_futures):
                    num = reserve_futures[rf]
                    try:
                        rf.result()
                        actions.append(
                            f"Provisioned new pool VM #{num} (stopped reserve)",
                        )
                        _log_vm_pool_event(
                            "replenish_provision_reserve",
                            vm_type=vm_type,
                            vm_name=_pool_vm_name(vm_type, num),
                        )
                    except Exception as e:
                        logger.error(
                            f"Replenish: failed to provision reserve VM #{num}: {e}",
                        )

    return {"vm_type": vm_type, "idle_count": len(idle_vms), "actions": actions}


def trim_pool(vm_type: str, extra_demand: int = 0) -> Dict[str, Any]:
    """Stop excess idle VMs to maintain POOL_TARGET_IDLE.

    ``extra_demand`` carries demand this process cannot see in its own
    ``_pending_claims`` counter — sessions waiting on capacity elsewhere. Without
    it, a trim in one process undoes a replenish another process just made for a
    queued session, and the pool flaps a VM between stopped and started.

    Uses a non-blocking per-vm_type lock so concurrent callers (fire-and-
    forget from release_pool_endpoint) don't duplicate work.
    """
    lock = _get_trim_lock(vm_type)
    if not lock.acquire(blocking=False):
        return {"vm_type": vm_type, "actions": [], "skipped": True}
    try:
        return _trim_pool_inner(vm_type, extra_demand=extra_demand)
    finally:
        lock.release()


def _idle_vm_age_seconds(vm, *, now: Optional[datetime] = None) -> Optional[float]:
    """Return how long a VM has been idle, or None when unknown."""

    reference = now or datetime.now(timezone.utc)
    progress_at = _pool_progress_reference_time(vm)
    if progress_at is not None and _pool_progress_phase(vm) == "idle":
        return max(0.0, (reference - progress_at).total_seconds())

    last_start = getattr(vm, "last_start_timestamp", None) or getattr(
        vm,
        "lastStartTimestamp",
        None,
    )
    if last_start:
        try:
            started_at = datetime.fromisoformat(str(last_start).replace("Z", "+00:00"))
            return max(0.0, (reference - started_at).total_seconds())
        except (TypeError, ValueError, OSError):
            logger.warning("Invalid last_start_timestamp on %s", vm.name)
    return None


def _trim_pool_inner(vm_type: str, extra_demand: int = 0) -> Dict[str, Any]:
    """Uses label-first ordering: CAS-sets pool-role from idle to stopped
    before issuing the stop, so a concurrent claim that already flipped
    the label to assigned will cause the CAS to fail cleanly.

    Re-verifies idle count on each iteration so that concurrent claims
    reducing the pool below target cause the loop to break early.

    Demand-aware and grace-aware: keeps at least
    ``max(POOL_TARGET_IDLE, pending_claims, extra_demand)`` idle VMs, and never
    stops an idle VM younger than ``POOL_IDLE_TRIM_GRACE_SECONDS`` so cold-start
    replenish in one process cannot be undone by trim in another.
    """
    client = compute_v1.InstancesClient()
    actions: list[str] = []
    max_iterations = 10

    for _ in range(max_iterations):
        try:
            _, _, idle_vms, _, _, _ = _list_pool_state(vm_type)
            with _pending_lock:
                pending = _pending_claims.get(_pool_scope_key(vm_type), 0)
            target = max(POOL_TARGET_IDLE, pending, extra_demand)
            if len(idle_vms) <= target:
                break

            now = datetime.now(timezone.utc)
            trimmable = []
            for vm in idle_vms:
                age = _idle_vm_age_seconds(vm, now=now)
                if age is None or age < POOL_IDLE_TRIM_GRACE_SECONDS:
                    continue
                trimmable.append(vm)
            if not trimmable:
                break
            # Keep enough idle capacity for the target; prefer trimming the
            # newest excess VMs once they are past the claim grace window.
            excess = len(idle_vms) - target
            if excess <= 0:
                break
            candidates = sorted(trimmable, key=lambda v: v.name, reverse=True)
            candidate = candidates[0]

            if not _set_pool_labels(
                client,
                candidate.name,
                {"pool-role": "stopped"},
                expected_role="idle",
            ):
                continue

            try:
                client.stop(
                    project=SETTINGS.vm_project_id,
                    zone=_current_vm_placement().zone,
                    instance=candidate.name,
                ).result()
            except Exception as e:
                logger.error(
                    f"Trim: stop failed for {candidate.name}, reverting label: {e}",
                )
                _set_pool_labels(client, candidate.name, {"pool-role": "idle"})
                continue

            fresh = client.get(
                project=SETTINGS.vm_project_id,
                zone=_current_vm_placement().zone,
                instance=candidate.name,
            )
            if fresh.status == "RUNNING":
                logger.warning(
                    f"Trim: {candidate.name} still RUNNING after stop, retrying",
                )
                try:
                    client.stop(
                        project=SETTINGS.vm_project_id,
                        zone=_current_vm_placement().zone,
                        instance=candidate.name,
                    ).result()
                except Exception as e:
                    logger.error(f"Trim: retry stop failed for {candidate.name}: {e}")

            actions.append(f"Stopped excess VM {candidate.name}")
            logger.info(f"Trim: stopped excess VM {candidate.name}")
            _log_vm_pool_event(
                "trim_stop",
                vm_name=candidate.name,
                vm_type=vm_type,
                pending=pending,
                target=target,
                idle_before=len(idle_vms),
            )
        except Exception as e:
            # Per-VM errors (e.g. CAS exhausting retries, or a failed
            # label revert after a failed stop) must not abort the loop
            # — remaining VMs still need processing.
            logger.error(f"Trim: failed to process VM: {e}")

    _, _, final_idle, _, _, _ = _list_pool_state(vm_type)
    return {"vm_type": vm_type, "idle_count": len(final_idle), "actions": actions}


def trim_stopped_pool_reserve(vm_type: str) -> Dict[str, Any]:
    """Delete excess stopped reserve VMs to maintain ``POOL_TARGET_STOPPED``.

    Uses the replenish lock because this cleanup mutates the same stopped
    reserve that ``replenish_pool()`` consumes to satisfy fresh demand.
    """

    lock = _get_replenish_lock(vm_type)
    if not lock.acquire(blocking=False):
        return {
            "vm_type": vm_type,
            "found": 0,
            "kept": [],
            "deleted": [],
            "errors": [],
            "actions": [],
            "skipped": True,
        }
    try:
        return _trim_stopped_pool_reserve_inner(vm_type)
    finally:
        lock.release()


def _trim_stopped_pool_reserve_inner(vm_type: str) -> Dict[str, Any]:
    """Delete older stopped reserve VMs beyond ``POOL_TARGET_STOPPED``."""

    client, _, _, stopped_vms, _, _ = _list_pool_state(vm_type)
    ranked = sorted(
        stopped_vms,
        key=lambda vm: (
            _stopped_pool_reference_time(vm)
            or datetime.min.replace(tzinfo=timezone.utc),
            vm.name,
        ),
        reverse=True,
    )
    kept = [vm.name for vm in ranked[:POOL_TARGET_STOPPED]]
    to_delete = ranked[POOL_TARGET_STOPPED:]
    if not to_delete:
        return {
            "vm_type": vm_type,
            "found": len(ranked),
            "kept": kept,
            "deleted": [],
            "errors": [],
            "actions": [],
        }

    deleted: list[str] = []
    errors: list[dict[str, str]] = []

    def _delete_one(vm) -> str | None:
        reference_time = _stopped_pool_reference_time(vm)
        try:
            _delete_pool_vm_instance(client, vm.name, vm_type=vm_type)
            _log_vm_pool_event(
                "stopped_reserve_pruned",
                vm_name=vm.name,
                vm_type=vm_type,
                retained_target=POOL_TARGET_STOPPED,
                stopped_reference_time=(
                    reference_time.isoformat() if reference_time else None
                ),
            )
            logger.info("Pruned excess stopped reserve VM %s", vm.name)
            return vm.name
        except Exception as exc:
            logger.error("Failed to prune stopped reserve VM %s: %s", vm.name, exc)
            errors.append({"vm_name": vm.name, "error": str(exc)})
            return None

    with ThreadPoolExecutor(
        max_workers=min(len(to_delete), 5),
        thread_name_prefix="reserve-prune",
    ) as pool:
        for result in pool.map(_delete_one, to_delete):
            if result:
                deleted.append(result)

    return {
        "vm_type": vm_type,
        "found": len(ranked),
        "kept": kept,
        "deleted": deleted,
        "errors": errors,
        "actions": [
            f"Deleted excess stopped reserve VM {vm_name}" for vm_name in deleted
        ],
    }


def _scrub_inconsistent_vms(vm_type: str) -> list[str]:
    """Detect and fix label/status mismatches across all pool VMs.

    Fetches every pool VM of *vm_type* in a single GCE list call, then
    classifies each into one of seven anomaly categories:

      1. stopped  + RUNNING      → stop   (ghost from failed trim)
      2. idle     + TERMINATED   → relabel stopped  (late mark-idle race)
      3. idle     + SUSPENDED    → relabel stopped   (GCE auto-suspend)
      4. starting + TERMINATED   → relabel stopped   (boot failed)
      5. provisioning + TERMINATED → relabel stopped (setup died)
      6. assigned + TERMINATED   → quarantine         (session's VM died)
      7. quarantined + RUNNING   → stop               (should not run)

    VMs that don't match any anomaly pattern are left untouched.
    """
    client = compute_v1.InstancesClient()
    request = compute_v1.ListInstancesRequest(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        filter=f"labels.vm-type={vm_type}",
    )
    all_vms = list(client.list(request=request))

    _STOP_ANOMALIES = {
        ("stopped", "RUNNING"),
        ("quarantined", "RUNNING"),
    }
    _RELABEL_STOPPED_ANOMALIES = {
        ("idle", "TERMINATED"),
        ("idle", "SUSPENDED"),
        ("starting", "TERMINATED"),
        ("provisioning", "TERMINATED"),
    }
    _QUARANTINE_ANOMALIES = {
        ("assigned", "TERMINATED"),
    }

    actions: list[str] = []

    for vm in all_vms:
        labels = dict(vm.labels) if vm.labels else {}
        role = labels.get("pool-role", "")
        if not role:
            continue

        key = (role, vm.status)

        if key in _STOP_ANOMALIES:
            anomaly = f"{role}_but_{vm.status.lower()}"
        elif key in _RELABEL_STOPPED_ANOMALIES:
            anomaly = f"{role}_but_{vm.status.lower()}"
        elif key in _QUARANTINE_ANOMALIES:
            anomaly = f"{role}_but_{vm.status.lower()}"
        else:
            continue

        logger.info(
            "Scrub %s: %s anomaly=%s (pool-role=%s, status=%s)",
            vm_type,
            vm.name,
            anomaly,
            role,
            vm.status,
        )
        _log_vm_pool_event(
            "scrub_anomaly",
            vm_type=vm_type,
            vm_name=vm.name,
            anomaly=anomaly,
            pool_role=role,
            gce_status=vm.status,
        )

        try:
            if key in _STOP_ANOMALIES:
                _request_vm_stop(
                    client,
                    vm.name,
                    vm_type=vm_type,
                    reason=f"scrub: {anomaly}",
                )
                msg = f"Scrub: stop requested for {vm.name} ({anomaly})"
                actions.append(msg)
                logger.info(msg)
                _log_vm_pool_event(
                    "scrub_stop",
                    vm_name=vm.name,
                    vm_type=vm_type,
                    anomaly=anomaly,
                )

            elif key in _RELABEL_STOPPED_ANOMALIES:
                _set_pool_labels(client, vm.name, {"pool-role": "stopped"})
                msg = f"Scrub: relabeled {vm.name} → stopped ({anomaly})"
                actions.append(msg)
                logger.info(msg)
                _log_vm_pool_event(
                    "scrub_relabel",
                    vm_name=vm.name,
                    vm_type=vm_type,
                    anomaly=anomaly,
                )

            elif key in _QUARANTINE_ANOMALIES:
                action = _quarantine_pool_vm(
                    client,
                    vm,
                    reason=f"scrub: {anomaly}",
                )
                if action:
                    actions.append(action)

        except Exception as e:
            logger.error("Scrub: failed to fix %s (%s): %s", vm.name, anomaly, e)

    return actions


def rebalance_pool(vm_type: str) -> Dict[str, Any]:
    """Full rebalance with orphan cleanup, scrub, replenish, trim, and reserve prune."""

    orphan_cleanup_result = cleanup_orphaned_pool_network_resources(vm_type)
    scrub_actions = _scrub_inconsistent_vms(vm_type)
    replenish_result = replenish_pool(vm_type)
    trim_result = trim_pool(vm_type)
    reserve_trim_result = trim_stopped_pool_reserve(vm_type)
    return {
        "vm_type": vm_type,
        "actions": (
            orphan_cleanup_result["actions"]
            + scrub_actions
            + replenish_result["actions"]
            + trim_result["actions"]
            + reserve_trim_result["actions"]
        ),
        "orphaned_static_ips_deleted": orphan_cleanup_result["deleted_addresses"],
        "orphaned_dns_deleted": orphan_cleanup_result["deleted_dns"],
        "orphaned_network_errors": orphan_cleanup_result["errors"],
        "stopped_reserve_deleted": reserve_trim_result["deleted"],
        "stopped_reserve_kept": reserve_trim_result["kept"],
    }


def reconcile_orphaned_disks(
    max_age_hours: int = 12,
    idle_hours: int = POOL_ASSISTANT_DISK_IDLE_HOURS,
    hard_cap_hours: int = POOL_ASSISTANT_DISK_HARD_CAP_HOURS,
) -> Dict[str, Any]:
    """Garbage-collect unattached ``unity-disk-*`` pd-standard disks.

    Workspace files are archived to GCS on session release, so the PD is
    no longer the sole durable copy. Three deletion branches cover the
    realistic cost-leak patterns:

    - **A — orphan:** assistant no longer exists in Orchestra and the
      disk has been detached at least *max_age_hours* (default 72 h).
    - **B — idle:** assistant still exists, the disk has been detached
      at least *idle_hours* (default 30 d), **and** a GCS archive exists
      whose ``updated`` timestamp is at least as recent as the disk's
      ``last_detach_timestamp`` (modulo a small skew). A fresh PD is
      recreated transparently on the next assignment and the guest
      restore path repopulates ``/Unity/Local`` from GCS.
    - **C — hard cap (opt-in, default off):** covers the above cases
      when the archive is missing or stale but the disk has been
      detached longer than *hard_cap_hours*. Accepts data loss; emits a
      WARN log per deletion. Set *hard_cap_hours* to ``0`` to disable.

    Returns a per-reason breakdown plus the aggregate ``deleted`` /
    ``skipped`` counts kept for contract compatibility. Idempotent and
    safe to call on a cron schedule (e.g. hourly).
    """
    from datetime import datetime, timezone, timedelta

    client = compute_v1.DisksClient()
    request = compute_v1.ListDisksRequest(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
    )

    now = datetime.now(timezone.utc)
    orphan_cutoff = now - timedelta(hours=max_age_hours)
    idle_cutoff = now - timedelta(hours=idle_hours)
    hard_cap_enabled = hard_cap_hours > 0
    hard_cap_cutoff = (
        now - timedelta(hours=hard_cap_hours) if hard_cap_enabled else None
    )

    deleted_orphan: list[str] = []
    deleted_idle: list[str] = []
    deleted_hard_cap: list[str] = []
    skipped_active: list[str] = []
    skipped_fresh: list[str] = []
    skipped_no_archive: list[str] = []
    skipped_stale_archive: list[str] = []
    errors: list[Dict[str, str]] = []
    deleted_details: list[Dict[str, str]] = []

    env_suffix = SETTINGS.env_suffix  # e.g. "" / "-staging"

    def _delete(disk_name: str, reason: str, bucket: list[str]) -> None:
        try:
            op = client.delete(
                project=SETTINGS.vm_project_id,
                zone=_current_vm_placement().zone,
                disk=disk_name,
            )
            op.result()
            bucket.append(disk_name)
            deleted_details.append({"disk": disk_name, "reason": reason})
            log_fn = logger.warning if reason == "hard_cap" else logger.info
            log_fn(f"Deleted assistant disk ({reason}): {disk_name}")
        except NotFound:
            logger.info(f"Disk already deleted during reconcile: {disk_name}")
        except Exception as e:
            logger.error(f"Failed to delete disk {disk_name} ({reason}): {e}")
            errors.append({"disk": disk_name, "reason": reason, "error": str(e)})

    for disk in client.list(request=request):
        if not disk.name.startswith("unity-disk-"):
            continue
        if disk.type_ and "pd-standard" not in disk.type_:
            continue
        if disk.users:
            continue

        # Extract assistant_id from disk name:
        #   unity-disk-{sanitized_id}{env_suffix}
        raw = disk.name[len("unity-disk-") :]
        if env_suffix and raw.endswith(env_suffix):
            raw = raw[: -len(env_suffix)]
        assistant_id = raw

        detach_ts = _parse_disk_timestamp(
            disk.last_detach_timestamp or disk.creation_timestamp,
        )

        assistant_exists = _assistant_exists(assistant_id)

        # Branch A — orphaned assistant.
        if not assistant_exists:
            if detach_ts is not None and detach_ts > orphan_cutoff:
                skipped_fresh.append(disk.name)
                continue
            _delete(disk.name, "orphan", deleted_orphan)
            continue

        # Branch B — assistant exists but has been idle long enough and
        # GCS holds a fresh archive.
        if detach_ts is None or detach_ts > idle_cutoff:
            skipped_active.append(disk.name)
            continue

        archive_exists, archive_updated = _assistant_archive_info(assistant_id)
        if not archive_exists:
            if hard_cap_enabled and detach_ts <= hard_cap_cutoff:
                logger.warning(
                    "Deleting disk %s under hard_cap=%dh with no GCS archive; "
                    "accepting data loss for assistant_id=%s",
                    disk.name,
                    hard_cap_hours,
                    assistant_id,
                )
                _delete(disk.name, "hard_cap", deleted_hard_cap)
            else:
                skipped_no_archive.append(disk.name)
            continue

        skew = timedelta(seconds=POOL_ASSISTANT_DISK_ARCHIVE_FRESHNESS_SKEW_SECONDS)
        if archive_updated is None or archive_updated + skew < detach_ts:
            if hard_cap_enabled and detach_ts <= hard_cap_cutoff:
                logger.warning(
                    "Deleting disk %s under hard_cap=%dh with stale archive "
                    "(archive_updated=%s, last_detach=%s); accepting data loss "
                    "for assistant_id=%s",
                    disk.name,
                    hard_cap_hours,
                    archive_updated,
                    detach_ts,
                    assistant_id,
                )
                _delete(disk.name, "hard_cap", deleted_hard_cap)
            else:
                skipped_stale_archive.append(disk.name)
            continue

        _delete(disk.name, "idle", deleted_idle)

    total_deleted = len(deleted_orphan) + len(deleted_idle) + len(deleted_hard_cap)
    total_skipped = (
        len(skipped_active)
        + len(skipped_fresh)
        + len(skipped_no_archive)
        + len(skipped_stale_archive)
    )

    if skipped_stale_archive:
        logger.warning(
            "Orphaned disk reconcile: %d disks kept due to stale GCS archive "
            "(points to missed release-time uploads): %s",
            len(skipped_stale_archive),
            skipped_stale_archive,
        )

    logger.info(
        "Orphaned disk reconciliation complete: "
        "deleted=%d (orphan=%d idle=%d hard_cap=%d) "
        "skipped=%d (active=%d fresh=%d no_archive=%d stale_archive=%d) "
        "errors=%d",
        total_deleted,
        len(deleted_orphan),
        len(deleted_idle),
        len(deleted_hard_cap),
        total_skipped,
        len(skipped_active),
        len(skipped_fresh),
        len(skipped_no_archive),
        len(skipped_stale_archive),
        len(errors),
    )

    return {
        "deleted": total_deleted,
        "skipped": total_skipped,
        "errors": errors,
        "deleted_orphan": len(deleted_orphan),
        "deleted_idle": len(deleted_idle),
        "deleted_hard_cap": len(deleted_hard_cap),
        "skipped_active_assistant": len(skipped_active),
        "skipped_fresh": len(skipped_fresh),
        "skipped_no_archive": len(skipped_no_archive),
        "skipped_stale_archive": len(skipped_stale_archive),
        "deleted_disks": deleted_orphan + deleted_idle + deleted_hard_cap,
        "deleted_details": deleted_details,
    }


def _parse_disk_timestamp(raw: Any):
    """Normalise a GCE disk timestamp into an aware ``datetime`` (UTC)."""
    from datetime import datetime, timezone

    if not raw:
        return None
    try:
        if isinstance(raw, str):
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            ts = raw
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except (ValueError, TypeError):
        return None


def _assistant_exists(assistant_id: str) -> bool:
    """Check whether an assistant still exists in Orchestra.

    Returns True if the assistant is found (disk must be kept), False if
    the assistant does not exist (disk can be garbage-collected), and
    True on any network/auth error (fail-safe: keep the disk).
    """
    admin_key = SETTINGS.orchestra_admin_key
    if not admin_key:
        return True  # cannot verify — assume it exists

    url = f"{SETTINGS.orchestra_url}/admin/assistant"
    try:
        resp = requests.get(
            url,
            params={"agent_id": str(assistant_id)},
            headers={"Authorization": f"Bearer {admin_key}"},
            timeout=10,
        )
        if resp.status_code == 200:
            assistants = resp.json().get("info", [])
            return len(assistants) > 0
        # Non-200 likely means auth issue or server error — keep disk.
        return True
    except Exception:
        return True  # network error — keep disk


def _assistant_archive_info(assistant_id: str):
    """Return ``(exists, updated_at)`` for the assistant's Local GCS archive.

    Looks up ``gs://{POOL_ASSISTANT_ARCHIVE_BUCKET}/{assistant_id}.tar.gz``.
    The companion desktop-profile blob
    (``{assistant_id}-desktop-profile.tar.gz``) is best-effort session
    state and is not required for orphan-disk GC — missing profile only
    means a cold browser login on next assign.

    Any exception is swallowed and reported as ``(False, None)`` so
    callers fail safe (keep the PD) on transient GCS issues.
    """
    import os
    import json as _json
    from google.cloud import storage
    from google.oauth2.service_account import Credentials

    try:
        creds_json = os.getenv("GCP_SA_KEY")
        if creds_json:
            creds = Credentials.from_service_account_info(_json.loads(creds_json))
            client = storage.Client(credentials=creds)
        else:
            client = storage.Client()
        bucket = client.bucket(POOL_ASSISTANT_ARCHIVE_BUCKET)
        blob = bucket.get_blob(f"{assistant_id}.tar.gz")
        if blob is None:
            return False, None
        return True, blob.updated
    except Exception as exc:
        logger.warning(
            "GCS archive probe failed for assistant_id=%s: %s",
            assistant_id,
            exc,
        )
        return False, None


def _assistant_desktop_profile_archive_exists(assistant_id: str) -> bool:
    """Return whether the companion desktop-profile archive blob exists."""
    import os
    import json as _json
    from google.cloud import storage
    from google.oauth2.service_account import Credentials

    try:
        creds_json = os.getenv("GCP_SA_KEY")
        if creds_json:
            creds = Credentials.from_service_account_info(_json.loads(creds_json))
            client = storage.Client(credentials=creds)
        else:
            client = storage.Client()
        bucket = client.bucket(POOL_ASSISTANT_ARCHIVE_BUCKET)
        blob = bucket.get_blob(f"{assistant_id}-desktop-profile.tar.gz")
        return blob is not None
    except Exception as exc:
        logger.warning(
            "GCS desktop-profile probe failed for assistant_id=%s: %s",
            assistant_id,
            exc,
        )
        return False


# =============================================================================
# TLS Certificate Push to Running Pool VMs
# =============================================================================


def push_cert_to_pool_vms() -> Dict[str, Any]:
    """Push the latest wildcard TLS cert to all pool VMs via metadata.

    Updates ``tls-fullchain`` / ``tls-privkey`` on every pool VM in the
    current environment (zone is staging- or prod-scoped).  Includes
    stopped VMs so they boot with the fresh cert when replenish starts
    them.  Running VMs pick up the change via the watcher's long-poll.
    """
    tls_cert = get_secret(VM_WILDCARD_CERT_SECRET)
    tls_key = get_secret(VM_WILDCARD_KEY_SECRET)
    if not tls_cert or not tls_key:
        logger.warning("push_cert_to_pool_vms: cert/key not found in Secret Manager")
        return {"pushed": False, "reason": "cert_not_found", "vms": []}

    client = compute_v1.InstancesClient()
    request = compute_v1.ListInstancesRequest(
        project=SETTINGS.vm_project_id,
        zone=_current_vm_placement().zone,
        filter="labels.pool-role:*",
    )
    pool_vms = list(client.list(request=request))

    results: list[str] = []
    for vm in pool_vms:
        try:
            _update_instance_metadata(
                vm.name,
                {
                    "tls-fullchain": tls_cert,
                    "tls-privkey": tls_key,
                },
            )
            results.append(vm.name)
            logger.info(f"Pushed cert to {vm.name}")
        except Exception as e:
            logger.error(f"Failed to push cert to {vm.name}: {e}")

    logger.info(f"Cert push complete: {len(results)}/{len(pool_vms)} VMs updated")
    return {"pushed": True, "vms": results, "total": len(pool_vms)}
