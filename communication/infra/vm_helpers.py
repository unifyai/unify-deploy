"""
Windows VM Lifecycle Management Helpers

This module provides functions for managing Windows VMs on GCP, including:
- Static IP reservation and release
- DNS A record management
- VM creation, start, stop, and deletion
- Full provisioning and deprovisioning orchestration
"""

import logging
import os
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from google.cloud import compute_v1
from google.cloud import dns
from google.cloud import secretmanager
from google.api_core.exceptions import NotFound, Conflict

# Environment detection
STAGING = os.environ.get("STAGING", "false").lower() == "true"
UNIFY_BASE_URL = (
    "https://api.unify.ai/v0"
    if not STAGING
    else "https://service.a.run.app/v0"
)

from .vm_config import (
    VM_PROJECT_ID,
    DNS_PROJECT_ID,
    REGION,
    ZONE,
    DNS_ZONE_NAME,
    DOMAIN_SUFFIX,
    VM_MACHINE_TYPE,
    VM_DISK_SIZE_GB,
    VM_DISK_TYPE,
    VM_IMAGE_FAMILY,
    VM_IMAGE_PROJECT,
    VM_NETWORK,
    VM_TAGS,
    ENV_SUFFIX,
    INIT_SCRIPT_PATH,
    MAK_KEY,
)

logger = logging.getLogger(__name__)


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


# =============================================================================
# Naming Conventions
# =============================================================================


def get_vm_name(assistant_id: str) -> str:
    """Generate consistent VM name from assistant ID."""
    # Sanitize assistant_id for GCP naming (lowercase, alphanumeric, hyphens)
    sanitized = assistant_id.lower().replace("_", "-")
    return f"unity-win-{sanitized}{ENV_SUFFIX}"


def get_static_ip_name(assistant_id: str) -> str:
    """Generate consistent static IP name from assistant ID."""
    sanitized = assistant_id.lower().replace("_", "-")
    return f"unity-win-ip-{sanitized}{ENV_SUFFIX}"


def get_dns_hostname(assistant_id: str) -> str:
    """Generate consistent DNS hostname from assistant ID.

    Format: unity-assistant-{id}.vm.unify.ai
    """
    return f"unity-assistant-{assistant_id}{ENV_SUFFIX}.{DOMAIN_SUFFIX}"


# =============================================================================
# Static IP Management
# =============================================================================


def reserve_static_ip(assistant_id: str) -> str:
    """
    Reserve a static external IP address for the VM.

    Returns:
        The reserved IP address string.
    """
    client = compute_v1.AddressesClient()
    ip_name = get_static_ip_name(assistant_id)

    address = compute_v1.Address(
        name=ip_name,
        address_type="EXTERNAL",
        network_tier="PREMIUM",
        description=f"Static IP for Unity Windows VM - Assistant {assistant_id}",
    )

    try:
        operation = client.insert(
            project=VM_PROJECT_ID,
            region=REGION,
            address_resource=address,
        )
        operation.result()  # Wait for completion
        logger.info(f"Reserved static IP: {ip_name}")
    except Conflict:
        logger.info(f"Static IP {ip_name} already exists, reusing")

    # Get the reserved IP address
    result = client.get(project=VM_PROJECT_ID, region=REGION, address=ip_name)
    return result.address


def release_static_ip(assistant_id: str) -> bool:
    """
    Release the static IP address.

    Returns:
        True if released, False if not found.
    """
    client = compute_v1.AddressesClient()
    ip_name = get_static_ip_name(assistant_id)

    try:
        operation = client.delete(
            project=VM_PROJECT_ID,
            region=REGION,
            address=ip_name,
        )
        operation.result()
        logger.info(f"Released static IP: {ip_name}")
        return True
    except NotFound:
        logger.warning(f"Static IP {ip_name} not found")
        return False


def get_static_ip(assistant_id: str) -> Optional[str]:
    """Get the static IP address if it exists."""
    client = compute_v1.AddressesClient()
    ip_name = get_static_ip_name(assistant_id)

    try:
        result = client.get(project=VM_PROJECT_ID, region=REGION, address=ip_name)
        return result.address
    except NotFound:
        return None


# =============================================================================
# DNS Management
# =============================================================================


def create_dns_record(assistant_id: str, ip_address: str) -> bool:
    """
    Create a DNS A record pointing to the VM's static IP.

    Args:
        assistant_id: The assistant ID
        ip_address: The static IP address to point to

    Returns:
        True if created successfully.
    """
    client = dns.Client(project=DNS_PROJECT_ID)
    zone = client.zone(DNS_ZONE_NAME)

    hostname = get_dns_hostname(assistant_id)
    fqdn = f"{hostname}."  # DNS records need trailing dot

    # Check if record already exists and delete it first
    try:
        existing_records = list(zone.list_resource_record_sets())
        for record in existing_records:
            if record.name == fqdn and record.record_type == "A":
                changes = zone.changes()
                changes.delete_record_set(record)
                changes.create()
                logger.info(f"Deleted existing DNS record: {fqdn}")
                break
    except Exception as e:
        logger.warning(f"Could not check existing DNS records: {e}")

    # Create new A record
    record_set = zone.resource_record_set(fqdn, "A", 300, [ip_address])
    changes = zone.changes()
    changes.add_record_set(record_set)
    changes.create()

    logger.info(f"Created DNS A record: {hostname} -> {ip_address}")
    return True


def delete_dns_record(assistant_id: str) -> bool:
    """
    Delete the DNS A record for the VM.

    Returns:
        True if deleted, False if not found.
    """
    client = dns.Client(project=DNS_PROJECT_ID)
    zone = client.zone(DNS_ZONE_NAME)

    hostname = get_dns_hostname(assistant_id)
    fqdn = f"{hostname}."

    try:
        existing_records = list(zone.list_resource_record_sets())
        for record in existing_records:
            if record.name == fqdn and record.record_type == "A":
                changes = zone.changes()
                changes.delete_record_set(record)
                changes.create()
                logger.info(f"Deleted DNS record: {fqdn}")
                return True
        logger.warning(f"DNS record {fqdn} not found")
        return False
    except Exception as e:
        logger.error(f"Error deleting DNS record: {e}")
        return False


# =============================================================================
# Startup Script
# =============================================================================


def generate_vnc_password(length: int = 12) -> str:
    """Generate a random VNC password."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def load_startup_script() -> str:
    """
    Load the Windows init script from file.

    The script reads configuration from GCP instance metadata keys:
    - windows-username: Windows user to create
    - windows-password: Windows user password
    - vnc-password: VNC/TightVNC password
    - hostname: DNS hostname for Caddy HTTPS
    - office-mak-key: Office MAK activation key (optional)

    Returns:
        The PowerShell startup script content.
    """
    with open(INIT_SCRIPT_PATH, "r") as f:
        return f.read()


# =============================================================================
# VM Lifecycle Management
# =============================================================================


def create_windows_vm(
    assistant_id: str,
    static_ip: str,
    hostname: str,
    unify_apikey: str,
    assistant_name: str,
) -> Dict[str, Any]:
    """
    Create a new Windows VM with the specified configuration.

    The startup script reads configuration from GCP instance metadata keys,
    matching the format expected by init2024.ps1:
    - windows-username, windows-password: Windows user credentials
    - vnc-password: VNC password for TightVNC
    - hostname: DNS hostname for Caddy HTTPS
    - office-mak-key: Office MAK activation key
    - github-token: GitHub PAT for cloning private repos
    - anthropic-api-key: Anthropic API key for agent service
    - unify-key: Unify API key for agent service
    - unify-base-url: Unify API base URL

    Args:
        assistant_id: The assistant ID (numeric string)
        static_ip: The static IP to assign
        hostname: The DNS hostname (unity-assistant-{id}.vm.unify.ai)
        unify_apikey: Unify API key (used for VNC password and Windows password)
        assistant_name: Assistant name (used for Windows username)

    Returns:
        Dict with VM details including name, ip, hostname, status.
    """
    client = compute_v1.InstancesClient()
    vm_name = get_vm_name(assistant_id)

    # Use unify_apikey for both VNC and Windows password
    # Use assistant_name for Windows username
    windows_username = assistant_name
    windows_password = unify_apikey
    vnc_password = unify_apikey

    # Fetch secrets from Secret Manager
    github_token = get_secret("DEVBOT_GITHUB_TOKEN")
    anthropic_api_key = get_secret("ANTHROPIC_API_KEY")

    # Load the startup script from file (reads config from metadata)
    startup_script = load_startup_script()

    # Build metadata items - script reads these at runtime
    metadata_items = [
        # The PowerShell startup script
        compute_v1.Items(
            key="windows-startup-script-ps1",
            value=startup_script,
        ),
        # Configuration metadata keys (read by the script via GCP metadata API)
        compute_v1.Items(key="windows-username", value=windows_username),
        compute_v1.Items(key="windows-password", value=windows_password),
        compute_v1.Items(key="vnc-password", value=vnc_password),
        compute_v1.Items(key="hostname", value=hostname),
        # Unify base URL (derived from STAGING flag)
        compute_v1.Items(key="unify-base-url", value=UNIFY_BASE_URL),
    ]

    # Add MAK key if configured
    if MAK_KEY:
        metadata_items.append(compute_v1.Items(key="office-mak-key", value=MAK_KEY))

    # Add staging flag only when STAGING is true
    if STAGING:
        metadata_items.append(compute_v1.Items(key="staging", value="true"))
        logger.info("Added staging=true to VM metadata")

    # Add secrets from Secret Manager (if available)
    if github_token:
        metadata_items.append(compute_v1.Items(key="github-token", value=github_token))
        logger.info("Added GitHub token to VM metadata")

    if anthropic_api_key:
        metadata_items.append(
            compute_v1.Items(key="anthropic-api-key", value=anthropic_api_key)
        )
        logger.info("Added Anthropic API key to VM metadata")

    # Use the passed unify_apikey directly (same key used for VNC/Windows auth)
    metadata_items.append(compute_v1.Items(key="unify-key", value=unify_apikey))
    logger.info("Added Unify key to VM metadata")

    # Configure the VM
    instance = compute_v1.Instance(
        name=vm_name,
        machine_type=f"zones/{ZONE}/machineTypes/{VM_MACHINE_TYPE}",
        description=f"Unity Windows VM for Assistant {assistant_id}",
        labels={
            "unity-assistant": assistant_id.lower().replace("_", "-"),
            "unity-type": "windows-vm",
        },
        tags=compute_v1.Tags(items=VM_TAGS),
        disks=[
            compute_v1.AttachedDisk(
                boot=True,
                auto_delete=True,
                initialize_params=compute_v1.AttachedDiskInitializeParams(
                    disk_size_gb=VM_DISK_SIZE_GB,
                    disk_type=f"zones/{ZONE}/diskTypes/{VM_DISK_TYPE}",
                    source_image=f"projects/{VM_IMAGE_PROJECT}/global/images/family/{VM_IMAGE_FAMILY}",
                ),
            )
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
                    )
                ],
            )
        ],
        metadata=compute_v1.Metadata(items=metadata_items),
        # Enable virtual display for VNC/noVNC to capture
        display_device=compute_v1.DisplayDevice(enable_display=True),
        scheduling=compute_v1.Scheduling(
            on_host_maintenance="MIGRATE",
            automatic_restart=True,
        ),
    )

    operation = client.insert(
        project=VM_PROJECT_ID,
        zone=ZONE,
        instance_resource=instance,
    )
    operation.result()  # Wait for completion

    logger.info(f"Created Windows VM: {vm_name} with IP {static_ip}")

    return {
        "vm_name": vm_name,
        "assistant_id": assistant_id,
        "ip_address": static_ip,
        "hostname": hostname,
        "desktop_url": f"https://{hostname}/desktop/custom.html",
        "status": "RUNNING",
    }


def start_windows_vm(assistant_id: str) -> Dict[str, Any]:
    """
    Start a stopped Windows VM.

    Returns:
        Dict with VM status.
    """
    client = compute_v1.InstancesClient()
    vm_name = get_vm_name(assistant_id)

    try:
        operation = client.start(
            project=VM_PROJECT_ID,
            zone=ZONE,
            instance=vm_name,
        )
        operation.result()
        logger.info(f"Started Windows VM: {vm_name}")

        return {
            "vm_name": vm_name,
            "assistant_id": assistant_id,
            "status": "RUNNING",
            "message": "VM started successfully",
        }
    except NotFound:
        logger.error(f"VM not found: {vm_name}")
        raise ValueError(f"VM not found for assistant {assistant_id}")


def stop_windows_vm(assistant_id: str) -> Dict[str, Any]:
    """
    Stop a running Windows VM (preserves disk and data).

    Returns:
        Dict with VM status.
    """
    client = compute_v1.InstancesClient()
    vm_name = get_vm_name(assistant_id)

    try:
        operation = client.stop(
            project=VM_PROJECT_ID,
            zone=ZONE,
            instance=vm_name,
        )
        operation.result()
        logger.info(f"Stopped Windows VM: {vm_name}")

        return {
            "vm_name": vm_name,
            "assistant_id": assistant_id,
            "status": "TERMINATED",
            "message": "VM stopped successfully",
        }
    except NotFound:
        logger.error(f"VM not found: {vm_name}")
        raise ValueError(f"VM not found for assistant {assistant_id}")


def delete_windows_vm(assistant_id: str) -> bool:
    """
    Delete a Windows VM.

    Returns:
        True if deleted, False if not found.
    """
    client = compute_v1.InstancesClient()
    vm_name = get_vm_name(assistant_id)

    try:
        operation = client.delete(
            project=VM_PROJECT_ID,
            zone=ZONE,
            instance=vm_name,
        )
        operation.result()
        logger.info(f"Deleted Windows VM: {vm_name}")
        return True
    except NotFound:
        logger.warning(f"VM not found: {vm_name}")
        return False


def get_windows_vm_status(assistant_id: str) -> Optional[Dict[str, Any]]:
    """
    Get the current status of a Windows VM.

    Returns:
        Dict with VM details and status, or None if not found.
    """
    client = compute_v1.InstancesClient()
    vm_name = get_vm_name(assistant_id)

    try:
        instance = client.get(
            project=VM_PROJECT_ID,
            zone=ZONE,
            instance=vm_name,
        )

        # Get external IP if assigned
        external_ip = None
        if instance.network_interfaces:
            for ni in instance.network_interfaces:
                if ni.access_configs:
                    for ac in ni.access_configs:
                        if ac.nat_i_p:
                            external_ip = ac.nat_i_p
                            break

        hostname = get_dns_hostname(assistant_id)

        # Extract timestamps from instance
        creation_ts = instance.creation_timestamp  # RFC 3339 string
        last_start_ts = instance.last_start_timestamp  # RFC 3339 string or empty

        # Calculate vm_ready_at: max(creation+15min, last_start+2min)
        vm_ready_at = None
        vm_ready = False

        if creation_ts:
            # Parse creation timestamp
            creation_dt = datetime.fromisoformat(creation_ts.replace("Z", "+00:00"))
            creation_ready = creation_dt + timedelta(minutes=8)

            # Check if there's a last_start_timestamp
            if last_start_ts:
                last_start_dt = datetime.fromisoformat(
                    last_start_ts.replace("Z", "+00:00")
                )
                start_ready = last_start_dt + timedelta(minutes=2)
                # Take the max (whichever requires longer wait)
                ready_at_dt = max(creation_ready, start_ready)
            else:
                ready_at_dt = creation_ready

            vm_ready_at = ready_at_dt.isoformat()
            now = datetime.now(timezone.utc)
            vm_ready = now >= ready_at_dt

        return {
            "vm_name": vm_name,
            "assistant_id": assistant_id,
            "status": instance.status,
            "ip_address": external_ip,
            "hostname": hostname,
            "desktop_url": (
                f"https://{hostname}/desktop/custom.html" if external_ip else None
            ),
            "machine_type": instance.machine_type.split("/")[-1],
            "zone": ZONE,
            "creation_timestamp": creation_ts or None,
            "last_start_timestamp": last_start_ts or None,
            "vm_ready_at": vm_ready_at,
            "vm_ready": vm_ready,
        }
    except NotFound:
        return None


# =============================================================================
# Orchestration Functions
# =============================================================================


def provision_windows_vm_full(
    assistant_id: str,
    unify_apikey: str,
    assistant_name: str,
) -> Dict[str, Any]:
    """
    Full provisioning of a Windows VM:
    1. Reserve static IP
    2. Create DNS A record
    3. Create and start VM

    Args:
        assistant_id: The assistant ID (numeric string)
        unify_apikey: Unify API key (used for VNC and Windows password)
        assistant_name: Assistant name (used for Windows username)

    Returns:
        Dict with full VM details.
    """
    logger.info(f"Starting full provisioning for assistant: {assistant_id}")

    # Step 1: Reserve static IP
    static_ip = reserve_static_ip(assistant_id)
    logger.info(f"Reserved static IP: {static_ip}")

    # Step 2: Create DNS record
    hostname = get_dns_hostname(assistant_id)
    create_dns_record(assistant_id, static_ip)
    logger.info(f"Created DNS record: {hostname} -> {static_ip}")

    # Step 3: Create VM
    result = create_windows_vm(
        assistant_id=assistant_id,
        static_ip=static_ip,
        hostname=hostname,
        unify_apikey=unify_apikey,
        assistant_name=assistant_name,
    )

    logger.info(f"Full provisioning complete for assistant: {assistant_id}")
    return result


def deprovision_windows_vm_full(assistant_id: str) -> Dict[str, Any]:
    """
    Full deprovisioning of a Windows VM:
    1. Delete VM
    2. Delete DNS record
    3. Release static IP

    Returns:
        Dict with deprovisioning status.
    """
    logger.info(f"Starting full deprovisioning for assistant: {assistant_id}")

    results = {
        "assistant_id": assistant_id,
        "vm_deleted": False,
        "dns_deleted": False,
        "ip_released": False,
    }

    # Step 1: Delete VM
    results["vm_deleted"] = delete_windows_vm(assistant_id)

    # Step 2: Delete DNS record
    results["dns_deleted"] = delete_dns_record(assistant_id)

    # Step 3: Release static IP
    results["ip_released"] = release_static_ip(assistant_id)

    logger.info(f"Full deprovisioning complete for assistant: {assistant_id}")
    return results
