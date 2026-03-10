"""
VM Lifecycle Management Helpers

This module provides functions for managing VMs on GCP (Windows and Ubuntu), including:
- Static IP reservation and release
- DNS A record management
- VM creation, start, stop, and deletion
- Full provisioning and deprovisioning orchestration
- SSH key generation for file sync
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple

import requests
from google.cloud import compute_v1
from google.cloud import dns
from google.cloud import secretmanager
from google.api_core.exceptions import NotFound, Conflict
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

# Environment detection
STAGING = os.environ.get("STAGING", "false").lower() == "true"

_default_orchestra_url = (
    "https://api.unify.ai/v0"
    if not STAGING
    else "https://internal.example.com/v0"
)
ORCHESTRA_URL = os.environ.get("ORCHESTRA_URL", _default_orchestra_url)

_default_comms_url = (
    "https://unity-comms-app-000000000000.us-central1.run.app"
    if not STAGING
    else "https://unity-comms-app-staging-000000000000.us-central1.run.app"
)
COMMS_URL = os.environ.get("UNITY_COMMS_URL", _default_comms_url)

from .vm_config import (
    VM_PROJECT_ID,
    DNS_PROJECT_ID,
    REGION,
    ZONE,
    DNS_ZONE_NAME,
    DOMAIN_SUFFIX,
    VM_DISK_TYPE,
    VM_NETWORK,
    ENV_SUFFIX,
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
    POOL_SSH_USERNAME,
    POOL_TARGET_IDLE,
    POOL_TARGET_STOPPED,
    POOL_ASSISTANT_DISK_SIZE_GB,
    POOL_ASSISTANT_DISK_TYPE,
    POOL_VM_NAME_PREFIX,
    POOL_UBUNTU_VM_IMAGE_FAMILY,
    POOL_WINDOWS_VM_IMAGE_FAMILY,
    POOL_ASSIGN_TIMEOUT,
    POOL_ASSIGN_POLL_INTERVAL,
)

logger = logging.getLogger(__name__)


def _probe_vm_https(hostname: str, timeout: float = 5.0) -> bool:
    """Probe whether Caddy is listening on port 443.

    Uses ``verify=False`` because Caddy may still be using a temporary
    self-signed certificate while the ACME challenge completes.  The goal is
    to confirm that Caddy is *up and accepting connections*, not that the
    certificate chain is valid.  Downstream callers (Unity) also skip TLS
    verification for the same reason — the connection is within GCP's VPC
    where infrastructure-level encryption already applies.
    """
    import warnings

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=requests.packages.urllib3.exceptions.InsecureRequestWarning,
            )
            resp = requests.head(
                f"https://{hostname}/",
                timeout=timeout,
                verify=False,
            )
        return resp.status_code < 500
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
        project_id: The GCP project ID (defaults to VM_PROJECT_ID)

    Returns:
        The secret value as a string, or None if not found.
    """
    if project_id is None:
        project_id = VM_PROJECT_ID

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
    return f"unity-assistant-{assistant_id}{ENV_SUFFIX}.{DOMAIN_SUFFIX}"


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
    api_key: str,
) -> bool:
    """Store SSH private key as an assistant secret via Orchestra API.

    Uses the assistant secrets API to store the private key so it can
    be retrieved by the Unity assistant for file sync.

    Args:
        assistant_id: The assistant ID
        private_key: The SSH private key (PEM format)
        api_key: Unify API key for authentication

    Returns:
        True if stored successfully, False otherwise
    """
    secret_name = "vm_ssh_private_key"
    url = f"{ORCHESTRA_URL}/assistant/{assistant_id}/secret"

    try:
        response = requests.post(
            url,
            json={
                "secret_name": secret_name,
                "secret_value": private_key,
                "description": "SSH private key for VM file sync (Ed25519)",
            },
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )

        if response.status_code in (200, 201):
            logger.info(f"Stored SSH private key for assistant {assistant_id}")
            return True
        elif response.status_code == 409:
            # Secret already exists, update it
            update_url = f"{url}/{secret_name}"
            update_response = requests.put(
                update_url,
                json={"secret_value": private_key},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            if update_response.status_code == 200:
                logger.info(f"Updated SSH private key for assistant {assistant_id}")
                return True
            else:
                logger.error(
                    f"Failed to update SSH key: {update_response.status_code} "
                    f"{update_response.text}",
                )
                return False
        else:
            logger.error(
                f"Failed to store SSH key: {response.status_code} {response.text}",
            )
            return False
    except Exception as e:
        logger.error(f"Error storing SSH private key: {e}")
        return False


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
        "enable_display": False,
    }


def _pool_vm_name(vm_type: str, n: int) -> str:
    return f"{POOL_VM_NAME_PREFIX}-{vm_type}-{n}{ENV_SUFFIX}"


def _pool_ip_name(vm_type: str, n: int) -> str:
    return f"{POOL_VM_NAME_PREFIX}-{vm_type}-ip-{n}{ENV_SUFFIX}"


def _pool_hostname(vm_type: str, n: int) -> str:
    return f"{POOL_VM_NAME_PREFIX}-{vm_type}-{n}{ENV_SUFFIX}.{DOMAIN_SUFFIX}"


def _assistant_disk_name(assistant_id: str) -> str:
    sanitized = assistant_id.lower().replace("_", "-")
    return f"unity-disk-{sanitized}{ENV_SUFFIX}"


def list_pool_vms(vm_type: Optional[str] = None) -> list[Dict[str, Any]]:
    """List all pool VMs, optionally filtered by type."""
    client = compute_v1.InstancesClient()

    label_filter = f"labels.pool-role:*"
    if vm_type:
        label_filter += f" AND labels.vm-type={vm_type}"

    request = compute_v1.ListInstancesRequest(
        project=VM_PROJECT_ID,
        zone=ZONE,
        filter=label_filter,
    )
    results = []
    for instance in client.list(request=request):
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
        results.append(
            {
                "vm_name": instance.name,
                "pool_role": labels.get("pool-role", "unknown"),
                "assistant_id": labels.get("assistant-id", "") or None,
                "vm_type": labels.get("vm-type", "unknown"),
                "ip_address": external_ip,
                "hostname": hostname,
                "status": instance.status,
                "label_fingerprint": instance.label_fingerprint,
            }
        )
    return results


def provision_pool_vm(vm_type: str, n: int) -> Dict[str, Any]:
    """Create a new pool VM with generic name, IP, DNS, and idle labels."""
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
            project=VM_PROJECT_ID, region=REGION, address_resource=address
        )
        op.result()
        logger.info(f"Reserved static IP: {ip_name}")
    except Conflict:
        logger.info(f"Static IP {ip_name} already exists, reusing")

    ip_result = ip_client.get(project=VM_PROJECT_ID, region=REGION, address=ip_name)
    static_ip = ip_result.address

    # Create DNS A record
    dns_client = dns.Client(project=DNS_PROJECT_ID)
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

    # Fetch secrets
    github_token = get_secret("DEVBOT_GITHUB_TOKEN")
    tls_cert = get_secret(VM_WILDCARD_CERT_SECRET)
    tls_key = get_secret(VM_WILDCARD_KEY_SECRET)

    startup_script = cfg["startup_script_loader"]()
    pool_watcher_script = cfg["pool_watcher_loader"]()

    metadata_items = [
        compute_v1.Items(key=cfg["startup_script_key"], value=startup_script),
        compute_v1.Items(key="pool-watcher-script", value=pool_watcher_script),
        compute_v1.Items(key="hostname", value=hostname),
        compute_v1.Items(key="orchestra-url", value=ORCHESTRA_URL),
        compute_v1.Items(key="comms-url", value=COMMS_URL),
    ]
    if github_token:
        metadata_items.append(compute_v1.Items(key="github-token", value=github_token))
    if tls_cert and tls_key:
        metadata_items.append(compute_v1.Items(key="tls-fullchain", value=tls_cert))
        metadata_items.append(compute_v1.Items(key="tls-privkey", value=tls_key))
    if STAGING:
        metadata_items.append(compute_v1.Items(key="staging", value="true"))

    labels = {
        "pool-role": "provisioning",
        "assistant-id": "",
        "vm-type": vm_type,
        "pool-hostname": hostname.replace(".", "-"),
    }

    instance_kwargs = dict(
        name=vm_name,
        machine_type=f"zones/{ZONE}/machineTypes/{cfg['machine_type']}",
        description=f"Unity pool VM ({vm_type}) #{n}",
        labels=labels,
        tags=compute_v1.Tags(items=cfg["tags"]),
        disks=[
            compute_v1.AttachedDisk(
                boot=True,
                auto_delete=True,
                initialize_params=compute_v1.AttachedDiskInitializeParams(
                    disk_size_gb=cfg["disk_size_gb"],
                    disk_type=f"zones/{ZONE}/diskTypes/{VM_DISK_TYPE}",
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
                email=f"pool-vm-sa@{VM_PROJECT_ID}.iam.gserviceaccount.com",
                scopes=["https://www.googleapis.com/auth/compute"],
            ),
        ],
    )
    if cfg["enable_display"]:
        instance_kwargs["display_device"] = compute_v1.DisplayDevice(
            enable_display=True
        )

    instance = compute_v1.Instance(**instance_kwargs)
    client = compute_v1.InstancesClient()
    op = client.insert(project=VM_PROJECT_ID, zone=ZONE, instance_resource=instance)
    op.result()

    logger.info(f"Provisioned pool VM: {vm_name} ({vm_type}) with IP {static_ip}")
    return {
        "vm_name": vm_name,
        "ip_address": static_ip,
        "hostname": hostname,
        "vm_type": vm_type,
        "status": "RUNNING",
    }


def claim_idle_vm(
    assistant_id: str, vm_type: str, vm_number: int | None = None
) -> Dict[str, Any]:
    """Atomically claim an idle pool VM using label fingerprint CAS.

    Retries on Conflict (another request claimed the same VM).
    Waits up to 300s if VMs are provisioning but none idle yet.
    Raises ValueError if no idle VMs are available.

    If vm_number is provided, targets the specific VM (e.g. vm_number=3
    targets unity-pool-{vm_type}-3) instead of auto-selecting.
    """
    client = compute_v1.InstancesClient()
    label_filter = (
        f"labels.pool-role=idle AND labels.vm-type={vm_type} AND status=RUNNING"
    )
    elapsed = 0

    while True:
        request = compute_v1.ListInstancesRequest(
            project=VM_PROJECT_ID,
            zone=ZONE,
            filter=label_filter,
        )
        idle_vms = list(client.list(request=request))
        if not idle_vms:
            if elapsed >= POOL_ASSIGN_TIMEOUT:
                raise ValueError(
                    f"No idle {vm_type} pool VMs available "
                    f"after waiting {elapsed}s"
                )
            logger.info(
                f"No idle {vm_type} VMs, waiting "
                f"({elapsed}s/{POOL_ASSIGN_TIMEOUT}s)..."
            )
            time.sleep(POOL_ASSIGN_POLL_INTERVAL)
            elapsed += POOL_ASSIGN_POLL_INTERVAL
            continue

        if vm_number is not None:
            target_name = _pool_vm_name(vm_type, vm_number)
            matching = [vm for vm in idle_vms if vm.name == target_name]
            if not matching:
                idle_names = [vm.name for vm in idle_vms]
                raise ValueError(
                    f"VM {target_name} is not idle. " f"Idle VMs: {idle_names}"
                )
            candidate = matching[0]
        else:
            candidate = idle_vms[0]
        new_labels = dict(candidate.labels) if candidate.labels else {}
        new_labels["pool-role"] = "assigned"
        new_labels["assistant-id"] = assistant_id.lower().replace("_", "-")

        try:
            op = client.set_labels(
                project=VM_PROJECT_ID,
                zone=ZONE,
                instance=candidate.name,
                instances_set_labels_request_resource=compute_v1.InstancesSetLabelsRequest(
                    labels=new_labels,
                    label_fingerprint=candidate.label_fingerprint,
                ),
            )
            op.result()
            logger.info(
                f"Claimed pool VM {candidate.name} for assistant {assistant_id}"
            )

            external_ip = None
            if candidate.network_interfaces:
                for ni in candidate.network_interfaces:
                    if ni.access_configs:
                        for ac in ni.access_configs:
                            if ac.nat_i_p:
                                external_ip = ac.nat_i_p
                                break

            hostname_label = new_labels.get("pool-hostname", "")
            hostname = (
                hostname_label.replace("-", ".")
                if hostname_label
                else candidate.name + f".{DOMAIN_SUFFIX}"
            )
            # Fix the hostname reconstruction: pool-hostname stores dots as dashes,
            # but we need to be careful about which dashes are literal.
            # The label stores e.g. "unity-pool-ubuntu-1--vm--unify--ai" or similar.
            # Simpler: just read hostname from instance metadata.
            hostname = _read_instance_metadata(candidate, "hostname") or hostname

            return {
                "vm_name": candidate.name,
                "assistant_id": assistant_id,
                "ip_address": external_ip,
                "hostname": hostname,
                "desktop_url": f"https://{hostname}",
                "status": "RUNNING",
            }
        except Conflict:
            logger.info(f"CAS conflict claiming {candidate.name}, retrying")
            continue


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
        type_=f"zones/{ZONE}/diskTypes/{POOL_ASSISTANT_DISK_TYPE}",
        description=f"Persistent storage for assistant {assistant_id}",
    )

    try:
        op = client.insert(project=VM_PROJECT_ID, zone=ZONE, disk_resource=disk)
        op.result()
        logger.info(
            f"Created assistant disk: {disk_name} ({POOL_ASSISTANT_DISK_SIZE_GB} GB)"
        )
    except Conflict:
        logger.info(f"Assistant disk {disk_name} already exists")

    result = client.get(project=VM_PROJECT_ID, zone=ZONE, disk=disk_name)
    return result.self_link


def attach_assistant_disk(vm_name: str, assistant_id: str) -> str:
    """Attach an assistant's persistent disk to a pool VM.

    Returns the device name used for mounting.
    """
    client = compute_v1.InstancesClient()
    disk_name = _assistant_disk_name(assistant_id)
    disk_source = f"projects/{VM_PROJECT_ID}/zones/{ZONE}/disks/{disk_name}"

    attached_disk = compute_v1.AttachedDisk(
        source=disk_source,
        device_name=disk_name,
        auto_delete=False,
        mode="READ_WRITE",
    )

    op = client.attach_disk(
        project=VM_PROJECT_ID,
        zone=ZONE,
        instance=vm_name,
        attached_disk_resource=attached_disk,
    )
    op.result()

    # The device name defaults to the disk name
    device_name = disk_name
    logger.info(f"Attached disk {disk_name} to {vm_name} (device: {device_name})")
    return device_name


def detach_assistant_disk(vm_name: str, assistant_id: str) -> bool:
    """Detach an assistant's persistent disk from a pool VM."""
    client = compute_v1.InstancesClient()
    disk_name = _assistant_disk_name(assistant_id)
    disk_suffix = f"/disks/{disk_name}"

    vm = client.get(project=VM_PROJECT_ID, zone=ZONE, instance=vm_name)
    actual_device_name = None
    if vm.disks:
        for d in vm.disks:
            if d.source and d.source.endswith(disk_suffix):
                actual_device_name = d.device_name
                break

    if not actual_device_name:
        logger.warning(f"Disk {disk_name} not attached to {vm_name}, skipping detach")
        return False

    op = client.detach_disk(
        project=VM_PROJECT_ID,
        zone=ZONE,
        instance=vm_name,
        device_name=actual_device_name,
    )
    op.result()
    logger.info(
        f"Detached disk {disk_name} from {vm_name} (device: {actual_device_name})"
    )
    return True


def delete_assistant_disk(assistant_id: str) -> bool:
    """Delete an assistant's persistent disk."""
    client = compute_v1.DisksClient()
    disk_name = _assistant_disk_name(assistant_id)

    try:
        op = client.delete(project=VM_PROJECT_ID, zone=ZONE, disk=disk_name)
        op.result()
        logger.info(f"Deleted assistant disk: {disk_name}")
        return True
    except NotFound:
        logger.warning(f"Assistant disk {disk_name} not found")
        return False


def _update_instance_metadata(vm_name: str, updates: Dict[str, str]) -> None:
    """Update metadata on a running instance (merge with existing)."""
    client = compute_v1.InstancesClient()
    instance = client.get(project=VM_PROJECT_ID, zone=ZONE, instance=vm_name)

    existing = {}
    if instance.metadata and instance.metadata.items:
        existing = {item.key: item.value for item in instance.metadata.items}

    existing.update(updates)

    items = [compute_v1.Items(key=k, value=v) for k, v in existing.items()]
    metadata = compute_v1.Metadata(
        items=items,
        fingerprint=instance.metadata.fingerprint if instance.metadata else None,
    )
    op = client.set_metadata(
        project=VM_PROJECT_ID,
        zone=ZONE,
        instance=vm_name,
        metadata_resource=metadata,
    )
    op.result()
    logger.info(f"Updated metadata on {vm_name}: {list(updates.keys())}")


def assign_pool_vm(
    assistant_id: str,
    unify_apikey: str,
    vm_type: str = "ubuntu",
    vm_number: int | None = None,
) -> Dict[str, Any]:
    """Full pool assignment: claim VM, create/attach disk, set metadata."""
    claimed = claim_idle_vm(assistant_id, vm_type, vm_number=vm_number)
    vm_name = claimed["vm_name"]

    # Create disk if it doesn't exist, then attach
    create_assistant_disk(assistant_id)
    device_name = attach_assistant_disk(vm_name, assistant_id)

    # Generate SSH keypair and store private key
    private_key, public_key = generate_ssh_keypair()
    store_ssh_private_key(assistant_id, private_key, unify_apikey)

    # Update metadata to trigger watcher reconfiguration
    metadata = {
        "unify-key": unify_apikey,
        "vnc-password": unify_apikey,
        "ssh-public-key": public_key,
        "disk-device": device_name,
        "assistant-id": assistant_id,
    }
    if vm_type == "windows" and MAK_KEY:
        metadata["office-mak-key"] = MAK_KEY
    _update_instance_metadata(vm_name, metadata)

    logger.info(f"Pool assignment complete: {vm_name} -> assistant {assistant_id}")
    return {
        "vm_name": vm_name,
        "assistant_id": assistant_id,
        "ip_address": claimed["ip_address"],
        "hostname": claimed["hostname"],
        "desktop_url": claimed["desktop_url"],
        "status": "RUNNING",
        "ssh_username": POOL_SSH_USERNAME,
        "ssh_port": SSH_SYNC_PORT,
    }


def release_pool_vm(assistant_id: str) -> Dict[str, Any]:
    """Release a pool VM: clear metadata, detach disk, reset labels.

    Idempotent — returns success if no VM is currently assigned.
    """
    client = compute_v1.InstancesClient()
    sanitized = assistant_id.lower().replace("_", "-")
    label_filter = f"labels.pool-role=assigned AND labels.assistant-id={sanitized}"

    request = compute_v1.ListInstancesRequest(
        project=VM_PROJECT_ID,
        zone=ZONE,
        filter=label_filter,
    )
    vms = list(client.list(request=request))
    if not vms:
        logger.info(
            f"No pool VM assigned to assistant {assistant_id} — nothing to release"
        )
        return {
            "released": False,
            "assistant_id": assistant_id,
            "message": "No VM assigned",
        }

    vm = vms[0]
    vm_name = vm.name

    # Clear assignment metadata (triggers watcher cleanup)
    _update_instance_metadata(
        vm_name,
        {
            "unify-key": "",
            "vnc-password": "",
            "ssh-public-key": "",
            "disk-device": "",
            "assistant-id": "",
        },
    )

    # Detach persistent disk
    detach_assistant_disk(vm_name, assistant_id)

    # Reset labels to idle
    labels = dict(vm.labels) if vm.labels else {}
    labels["pool-role"] = "idle"
    labels["assistant-id"] = ""

    # Re-read to get fresh fingerprint after metadata update
    vm = client.get(project=VM_PROJECT_ID, zone=ZONE, instance=vm_name)
    op = client.set_labels(
        project=VM_PROJECT_ID,
        zone=ZONE,
        instance=vm_name,
        instances_set_labels_request_resource=compute_v1.InstancesSetLabelsRequest(
            labels=labels,
            label_fingerprint=vm.label_fingerprint,
        ),
    )
    op.result()

    logger.info(f"Released pool VM {vm_name} from assistant {assistant_id}")

    return {
        "released": True,
        "assistant_id": assistant_id,
        "vm_name": vm_name,
        "vm_type": labels.get("vm-type", "ubuntu"),
    }


def rebalance_pool(vm_type: str) -> Dict[str, Any]:
    """Ensure exactly POOL_TARGET_IDLE idle VMs for the given type.

    Scale up: start a stopped VM or provision a new one.
    Scale down: stop excess idle VMs.
    """
    client = compute_v1.InstancesClient()
    type_filter = f"labels.vm-type={vm_type}"
    request = compute_v1.ListInstancesRequest(
        project=VM_PROJECT_ID,
        zone=ZONE,
        filter=type_filter,
    )
    all_vms = list(client.list(request=request))

    pool_vms = [vm for vm in all_vms if vm.labels and vm.labels.get("pool-role")]
    idle_vms = [
        vm
        for vm in pool_vms
        if vm.labels.get("pool-role") == "idle" and vm.status == "RUNNING"
    ]
    stopped_vms = [
        vm
        for vm in pool_vms
        if vm.labels.get("pool-role") == "stopped" or vm.status == "TERMINATED"
    ]

    actions = {"vm_type": vm_type, "idle_count": len(idle_vms), "actions": []}

    existing_names = {vm.name for vm in pool_vms}
    started_one = False

    # Rule 1: ensure idle VMs are available
    if len(idle_vms) <= POOL_TARGET_IDLE:
        if stopped_vms:
            vm = stopped_vms[0]
            try:
                op = client.start(project=VM_PROJECT_ID, zone=ZONE, instance=vm.name)
                op.result()
                labels = dict(vm.labels) if vm.labels else {}
                labels["pool-role"] = "provisioning"
                vm_fresh = client.get(
                    project=VM_PROJECT_ID, zone=ZONE, instance=vm.name
                )
                client.set_labels(
                    project=VM_PROJECT_ID,
                    zone=ZONE,
                    instance=vm.name,
                    instances_set_labels_request_resource=compute_v1.InstancesSetLabelsRequest(
                        labels=labels,
                        label_fingerprint=vm_fresh.label_fingerprint,
                    ),
                ).result()
                actions["actions"].append(f"Started stopped VM {vm.name}")
                logger.info(f"Rebalance: started stopped VM {vm.name}")
                started_one = True
            except Exception as e:
                logger.error(f"Rebalance: failed to start {vm.name}: {e}")
        else:
            n = 1
            while _pool_vm_name(vm_type, n) in existing_names:
                n += 1
            try:
                provision_pool_vm(vm_type, n)
                existing_names.add(_pool_vm_name(vm_type, n))
                actions["actions"].append(f"Provisioned new pool VM #{n}")
                logger.info(f"Rebalance: provisioned new {vm_type} pool VM #{n}")
            except Exception as e:
                logger.error(f"Rebalance: failed to provision new VM: {e}")
    # Scale down: too many idle VMs
    elif len(idle_vms) > POOL_TARGET_IDLE:
        excess = len(idle_vms) - POOL_TARGET_IDLE
        # Stop the highest-numbered idle VMs
        to_stop = sorted(idle_vms, key=lambda vm: vm.name, reverse=True)[:excess]
        for vm in to_stop:
            try:
                op = client.stop(project=VM_PROJECT_ID, zone=ZONE, instance=vm.name)
                op.result()
                labels = dict(vm.labels) if vm.labels else {}
                labels["pool-role"] = "stopped"
                vm_fresh = client.get(
                    project=VM_PROJECT_ID, zone=ZONE, instance=vm.name
                )
                client.set_labels(
                    project=VM_PROJECT_ID,
                    zone=ZONE,
                    instance=vm.name,
                    instances_set_labels_request_resource=compute_v1.InstancesSetLabelsRequest(
                        labels=labels,
                        label_fingerprint=vm_fresh.label_fingerprint,
                    ),
                ).result()
                actions["actions"].append(f"Stopped excess VM {vm.name}")
                logger.info(f"Rebalance: stopped excess VM {vm.name}")
            except Exception as e:
                logger.error(f"Rebalance: failed to stop {vm.name}: {e}")

    # Rule 2: ensure stopped reserve
    effective_stopped = len(stopped_vms) - (1 if started_one else 0)
    if effective_stopped <= POOL_TARGET_STOPPED:
        n = 1
        while _pool_vm_name(vm_type, n) in existing_names:
            n += 1
        try:
            provision_pool_vm(vm_type, n)
            existing_names.add(_pool_vm_name(vm_type, n))
            actions["actions"].append(f"Provisioned new pool VM #{n} (stopped reserve)")
            logger.info(
                f"Rebalance: provisioned new {vm_type} pool VM #{n} (stopped reserve)"
            )
        except Exception as e:
            logger.error(f"Rebalance: failed to provision new VM: {e}")

    return actions


# =============================================================================
# TLS Certificate Push to Running Pool VMs
# =============================================================================


def push_cert_to_pool_vms() -> Dict[str, Any]:
    """Push the latest wildcard TLS cert to all running pool VMs via metadata.

    After a cert renewal, running VMs still hold the old cert in their
    metadata. This function fetches the fresh cert from Secret Manager and
    updates ``tls-fullchain`` / ``tls-privkey`` on every running pool VM.
    The watchers on each VM detect the metadata change and reload Caddy.
    """
    tls_cert = get_secret(VM_WILDCARD_CERT_SECRET)
    tls_key = get_secret(VM_WILDCARD_KEY_SECRET)
    if not tls_cert or not tls_key:
        logger.warning("push_cert_to_pool_vms: cert/key not found in Secret Manager")
        return {"pushed": False, "reason": "cert_not_found", "vms": []}

    client = compute_v1.InstancesClient()
    request = compute_v1.ListInstancesRequest(
        project=VM_PROJECT_ID,
        zone=ZONE,
        filter="labels.pool-role:*",
    )
    all_vms = list(client.list(request=request))
    running_vms = [vm for vm in all_vms if vm.status == "RUNNING"]

    results: list[str] = []
    for vm in running_vms:
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

    logger.info(f"Cert push complete: {len(results)}/{len(running_vms)} VMs updated")
    return {"pushed": True, "vms": results, "total_running": len(running_vms)}
