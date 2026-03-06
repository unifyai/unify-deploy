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
    # Windows VM config
    WINDOWS_VM_MACHINE_TYPE,
    WINDOWS_VM_DISK_SIZE_GB,
    WINDOWS_VM_IMAGE_FAMILY,
    WINDOWS_VM_IMAGE_PROJECT,
    WINDOWS_VM_TAGS,
    WINDOWS_INIT_SCRIPT_PATH,
    # Ubuntu VM config
    UBUNTU_VM_MACHINE_TYPE,
    UBUNTU_VM_DISK_SIZE_GB,
    UBUNTU_VM_IMAGE_FAMILY,
    UBUNTU_VM_IMAGE_PROJECT,
    UBUNTU_VM_TAGS,
    UBUNTU_INIT_SCRIPT_PATH,
    # Pool config
    POOL_SSH_USERNAME,
    POOL_TARGET_IDLE,
    POOL_ASSISTANT_DISK_SIZE_GB,
    POOL_ASSISTANT_DISK_TYPE,
    POOL_VM_NAME_PREFIX,
    POOL_UBUNTU_VM_IMAGE_FAMILY,
    POOL_WINDOWS_VM_IMAGE_FAMILY,
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


# =============================================================================
# Naming Conventions
# =============================================================================


def get_vm_name(assistant_id: str, vm_type: str = "windows") -> str:
    """Generate consistent VM name from assistant ID and type.

    Windows: unity-win-{id}{-staging}
    Ubuntu: unity-ubuntu-{id}{-staging}
    """
    sanitized = assistant_id.lower().replace("_", "-")
    prefix = "unity-win" if vm_type == "windows" else "unity-ubuntu"
    return f"{prefix}-{sanitized}{ENV_SUFFIX}"


def get_static_ip_name(assistant_id: str, vm_type: str = "windows") -> str:
    """Generate consistent static IP name from assistant ID and type.

    Windows: unity-win-ip-{id}{-staging}
    Ubuntu: unity-ubuntu-ip-{id}{-staging}
    """
    sanitized = assistant_id.lower().replace("_", "-")
    prefix = "unity-win-ip" if vm_type == "windows" else "unity-ubuntu-ip"
    return f"{prefix}-{sanitized}{ENV_SUFFIX}"


def get_dns_hostname(assistant_id: str) -> str:
    """Generate consistent DNS hostname from assistant ID.

    Format: unity-assistant-{id}{-staging}.vm.unify.ai

    NOTE: Same for both Windows and Ubuntu - only one VM per assistant.
    """
    return f"unity-assistant-{assistant_id}{ENV_SUFFIX}.{DOMAIN_SUFFIX}"


# =============================================================================
# Static IP Management
# =============================================================================


def reserve_static_ip(assistant_id: str, vm_type: str = "windows") -> str:
    """
    Reserve a static external IP address for the VM.

    Args:
        assistant_id: The assistant ID
        vm_type: "windows" or "ubuntu"

    Returns:
        The reserved IP address string.
    """
    client = compute_v1.AddressesClient()
    ip_name = get_static_ip_name(assistant_id, vm_type)
    type_label = "Windows" if vm_type == "windows" else "Ubuntu"

    address = compute_v1.Address(
        name=ip_name,
        address_type="EXTERNAL",
        network_tier="PREMIUM",
        description=f"Static IP for Unity {type_label} VM - Assistant {assistant_id}",
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


def release_static_ip(assistant_id: str, vm_type: str = "windows") -> bool:
    """
    Release the static IP address.

    Args:
        assistant_id: The assistant ID
        vm_type: "windows" or "ubuntu"

    Returns:
        True if released, False if not found.
    """
    client = compute_v1.AddressesClient()
    ip_name = get_static_ip_name(assistant_id, vm_type)

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
# VM Creation - Windows
# =============================================================================


def create_windows_vm(
    assistant_id: str,
    static_ip: str,
    hostname: str,
    unify_apikey: str,
    assistant_name: str,
    ssh_public_key: str = "",
) -> Dict[str, Any]:
    """
    Create a new Windows VM with the specified configuration.

    The startup script reads configuration from GCP instance metadata keys:
    - windows-username, windows-password: Windows user credentials
    - vnc-password: VNC password for TightVNC
    - hostname: DNS hostname for Caddy HTTPS
    - office-mak-key: Office MAK activation key
    - github-token: GitHub PAT for cloning private repos
    - unify-key: Unify API key for agent service
    - orchestra-url: Orchestra API base URL
    - comms-url: Communication service base URL
    - ssh-public-key: SSH public key for file sync (optional)
    Note: SSH uses windows-username for authentication (no separate ssh-username).

    Args:
        assistant_id: The assistant ID (numeric string)
        static_ip: The static IP to assign
        hostname: The DNS hostname (unity-assistant-{id}.vm.unify.ai)
        unify_apikey: Unify API key (used for VNC password and Windows password)
        assistant_name: Assistant name (used for Windows username and SSH auth)
        ssh_public_key: SSH public key for file sync (optional)

    Returns:
        Dict with VM details including name, ip, hostname, status, ssh_username, ssh_port.
    """
    client = compute_v1.InstancesClient()
    vm_name = get_vm_name(assistant_id, vm_type="windows")

    # Use unify_apikey for both VNC and Windows password
    # Use assistant_name for Windows username
    windows_username = assistant_name
    windows_password = unify_apikey
    vnc_password = unify_apikey

    # Fetch secrets from Secret Manager
    github_token = get_secret("DEVBOT_GITHUB_TOKEN")

    # Load the startup script from file (reads config from metadata)
    startup_script = load_windows_startup_script()

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
        # Orchestra URL (derived from STAGING flag)
        compute_v1.Items(key="orchestra-url", value=ORCHESTRA_URL),
        # Communication service URL (derived from STAGING flag)
        compute_v1.Items(key="comms-url", value=COMMS_URL),
    ]

    # Add SSH file sync metadata if provided
    # Note: Windows uses windows-username for SSH auth (no separate ssh-username needed)
    if ssh_public_key:
        metadata_items.append(
            compute_v1.Items(key="ssh-public-key", value=ssh_public_key),
        )
        logger.info(
            f"Added SSH public key for file sync (uses Windows user: {windows_username})",
        )

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

    # Use the passed unify_apikey directly (same key used for VNC/Windows auth)
    metadata_items.append(compute_v1.Items(key="unify-key", value=unify_apikey))
    logger.info("Added Unify key to VM metadata")

    # Add wildcard TLS cert from Secret Manager (eliminates per-VM ACME requests)
    tls_cert = get_secret(VM_WILDCARD_CERT_SECRET)
    tls_key = get_secret(VM_WILDCARD_KEY_SECRET)
    if tls_cert and tls_key:
        metadata_items.append(compute_v1.Items(key="tls-fullchain", value=tls_cert))
        metadata_items.append(compute_v1.Items(key="tls-privkey", value=tls_key))
        logger.info("Added wildcard TLS cert to VM metadata")

    # Configure the VM
    instance = compute_v1.Instance(
        name=vm_name,
        machine_type=f"zones/{ZONE}/machineTypes/{WINDOWS_VM_MACHINE_TYPE}",
        description=f"Unity Windows VM for Assistant {assistant_id}",
        labels={
            "unity-assistant": assistant_id.lower().replace("_", "-"),
            "unity-type": "windows-vm",
        },
        tags=compute_v1.Tags(items=WINDOWS_VM_TAGS),
        disks=[
            compute_v1.AttachedDisk(
                boot=True,
                auto_delete=True,
                initialize_params=compute_v1.AttachedDiskInitializeParams(
                    disk_size_gb=WINDOWS_VM_DISK_SIZE_GB,
                    disk_type=f"zones/{ZONE}/diskTypes/{VM_DISK_TYPE}",
                    source_image=f"projects/{WINDOWS_VM_IMAGE_PROJECT}/global/images/family/{WINDOWS_VM_IMAGE_FAMILY}",
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
        "desktop_url": f"https://{hostname}",
        "status": "RUNNING",
        # SSH uses the Windows username for authentication
        "ssh_username": windows_username if ssh_public_key else None,
        "ssh_port": SSH_SYNC_PORT if ssh_public_key else None,
    }


# =============================================================================
# VM Creation - Ubuntu
# =============================================================================


def create_ubuntu_vm(
    assistant_id: str,
    static_ip: str,
    hostname: str,
    unify_apikey: str,
    assistant_name: str,
    ssh_public_key: str = "",
    ssh_username: str = "",
) -> Dict[str, Any]:
    """
    Create a new Ubuntu VM with the specified configuration.

    Uses custom Ubuntu image (unity-ubuntu-vm) with bash startup script.
    The startup script reads configuration from GCP instance metadata keys:
    - vnc-password: VNC password
    - hostname: DNS hostname for Caddy HTTPS
    - github-token: GitHub PAT for cloning repos
    - unify-key: Unify API key for agent service
    - orchestra-url: Orchestra API base URL
    - comms-url: Communication service base URL
    - staging: Use staging branch
    - ssh-public-key: SSH public key for file sync (optional)
    - ssh-username: SSH username for file sync (optional)

    Args:
        assistant_id: The assistant ID (numeric string)
        static_ip: The static IP to assign
        hostname: The DNS hostname (unity-assistant-{id}.vm.unify.ai)
        unify_apikey: Unify API key (used for VNC password)
        assistant_name: Assistant name (used for SSH username derivation)
        ssh_public_key: SSH public key for file sync (optional)
        ssh_username: SSH username for file sync (optional)

    Returns:
        Dict with VM details including name, ip, hostname, status, ssh_username, ssh_port.
    """
    client = compute_v1.InstancesClient()
    vm_name = get_vm_name(assistant_id, vm_type="ubuntu")

    # Use unify_apikey for VNC password (same as Windows)
    vnc_password = unify_apikey

    # Fetch secrets from Secret Manager
    github_token = get_secret("DEVBOT_GITHUB_TOKEN")

    # Load the startup script from file
    startup_script = load_ubuntu_startup_script()

    # Build metadata items - script reads these at runtime
    metadata_items = [
        # The bash startup script
        compute_v1.Items(key="startup-script", value=startup_script),
        # Configuration metadata keys (read by the script via GCP metadata API)
        compute_v1.Items(key="vnc-password", value=vnc_password),
        compute_v1.Items(key="hostname", value=hostname),
        # Orchestra URL (derived from STAGING flag)
        compute_v1.Items(key="orchestra-url", value=ORCHESTRA_URL),
        # Communication service URL (derived from STAGING flag)
        compute_v1.Items(key="comms-url", value=COMMS_URL),
    ]

    # Add SSH file sync metadata if provided
    if ssh_public_key and ssh_username:
        metadata_items.append(
            compute_v1.Items(key="ssh-public-key", value=ssh_public_key),
        )
        metadata_items.append(compute_v1.Items(key="ssh-username", value=ssh_username))
        logger.info(f"Added SSH file sync config for user: {ssh_username}")

    # Add staging flag only when STAGING is true
    if STAGING:
        metadata_items.append(compute_v1.Items(key="staging", value="true"))
        logger.info("Added staging=true to VM metadata")

    # Add secrets from Secret Manager (if available)
    if github_token:
        metadata_items.append(compute_v1.Items(key="github-token", value=github_token))
        logger.info("Added GitHub token to VM metadata")

    # Use the passed unify_apikey directly
    metadata_items.append(compute_v1.Items(key="unify-key", value=unify_apikey))
    logger.info("Added Unify key to VM metadata")

    # Add wildcard TLS cert from Secret Manager (eliminates per-VM ACME requests)
    tls_cert = get_secret(VM_WILDCARD_CERT_SECRET)
    tls_key = get_secret(VM_WILDCARD_KEY_SECRET)
    if tls_cert and tls_key:
        metadata_items.append(compute_v1.Items(key="tls-fullchain", value=tls_cert))
        metadata_items.append(compute_v1.Items(key="tls-privkey", value=tls_key))
        logger.info("Added wildcard TLS cert to VM metadata")

    # Configure the VM
    instance = compute_v1.Instance(
        name=vm_name,
        machine_type=f"zones/{ZONE}/machineTypes/{UBUNTU_VM_MACHINE_TYPE}",
        description=f"Unity Ubuntu VM for Assistant {assistant_id}",
        labels={
            "unity-assistant": assistant_id.lower().replace("_", "-"),
            "unity-type": "ubuntu-vm",
        },
        tags=compute_v1.Tags(items=UBUNTU_VM_TAGS),
        disks=[
            compute_v1.AttachedDisk(
                boot=True,
                auto_delete=True,
                initialize_params=compute_v1.AttachedDiskInitializeParams(
                    disk_size_gb=UBUNTU_VM_DISK_SIZE_GB,
                    disk_type=f"zones/{ZONE}/diskTypes/{VM_DISK_TYPE}",
                    source_image=f"projects/{UBUNTU_VM_IMAGE_PROJECT}/global/images/family/{UBUNTU_VM_IMAGE_FAMILY}",
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
    )

    operation = client.insert(
        project=VM_PROJECT_ID,
        zone=ZONE,
        instance_resource=instance,
    )
    operation.result()  # Wait for completion

    logger.info(f"Created Ubuntu VM: {vm_name} with IP {static_ip}")

    return {
        "vm_name": vm_name,
        "assistant_id": assistant_id,
        "ip_address": static_ip,
        "hostname": hostname,
        "desktop_url": f"https://{hostname}",
        "status": "RUNNING",
        "ssh_username": ssh_username or None,
        "ssh_port": SSH_SYNC_PORT if ssh_username else None,
    }


# =============================================================================
# VM Lifecycle Management (Generalized)
# =============================================================================


def start_vm(assistant_id: str, vm_type: str = "windows") -> Dict[str, Any]:
    """
    Start a stopped VM (Windows or Ubuntu).

    Args:
        assistant_id: The assistant ID
        vm_type: "windows" or "ubuntu"

    Returns:
        Dict with VM status.
    """
    client = compute_v1.InstancesClient()
    vm_name = get_vm_name(assistant_id, vm_type)

    try:
        operation = client.start(
            project=VM_PROJECT_ID,
            zone=ZONE,
            instance=vm_name,
        )
        operation.result()
        logger.info(f"Started {vm_type} VM: {vm_name}")

        return {
            "vm_name": vm_name,
            "assistant_id": assistant_id,
            "status": "RUNNING",
            "message": "VM started successfully",
        }
    except NotFound:
        logger.error(f"VM not found: {vm_name}")
        raise ValueError(f"VM not found for assistant {assistant_id}")


def stop_vm(assistant_id: str, vm_type: str = "windows") -> Dict[str, Any]:
    """
    Stop a running VM (Windows or Ubuntu). Preserves disk and data.

    Args:
        assistant_id: The assistant ID
        vm_type: "windows" or "ubuntu"

    Returns:
        Dict with VM status.
    """
    client = compute_v1.InstancesClient()
    vm_name = get_vm_name(assistant_id, vm_type)

    try:
        operation = client.stop(
            project=VM_PROJECT_ID,
            zone=ZONE,
            instance=vm_name,
        )
        operation.result()
        logger.info(f"Stopped {vm_type} VM: {vm_name}")

        return {
            "vm_name": vm_name,
            "assistant_id": assistant_id,
            "status": "TERMINATED",
            "message": "VM stopped successfully",
        }
    except NotFound:
        logger.error(f"VM not found: {vm_name}")
        raise ValueError(f"VM not found for assistant {assistant_id}")


def delete_vm(assistant_id: str, vm_type: str = "windows") -> bool:
    """
    Delete a VM (Windows or Ubuntu).

    Args:
        assistant_id: The assistant ID
        vm_type: "windows" or "ubuntu"

    Returns:
        True if deleted, False if not found.
    """
    client = compute_v1.InstancesClient()
    vm_name = get_vm_name(assistant_id, vm_type)

    try:
        operation = client.delete(
            project=VM_PROJECT_ID,
            zone=ZONE,
            instance=vm_name,
        )
        operation.result()
        logger.info(f"Deleted {vm_type} VM: {vm_name}")
        return True
    except NotFound:
        logger.warning(f"VM not found: {vm_name}")
        return False


def get_vm_status(
    assistant_id: str,
    vm_type: str = "windows",
) -> Optional[Dict[str, Any]]:
    """
    Get the current status of a VM (Windows or Ubuntu).

    Args:
        assistant_id: The assistant ID
        vm_type: "windows" or "ubuntu"

    Returns:
        Dict with VM details and status, or None if not found.
    """
    client = compute_v1.InstancesClient()
    vm_name = get_vm_name(assistant_id, vm_type)

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

        # Calculate vm_ready_at: max(creation+Xmin, last_start+20sec)
        # Windows: 5 min after creation, Ubuntu: 2 min after creation
        vm_ready_at = None
        vm_ready = False

        if creation_ts:
            # Parse creation timestamp
            creation_dt = datetime.fromisoformat(creation_ts.replace("Z", "+00:00"))
            creation_wait_minutes = 4 if vm_type == "ubuntu" else 8
            creation_ready = creation_dt + timedelta(minutes=creation_wait_minutes)

            # Check if there's a last_start_timestamp
            if last_start_ts:
                last_start_dt = datetime.fromisoformat(
                    last_start_ts.replace("Z", "+00:00"),
                )
                start_wait_seconds = 60 if vm_type == "ubuntu" else 90
                start_ready = last_start_dt + timedelta(seconds=start_wait_seconds)
                # Take the max (whichever requires longer wait)
                ready_at_dt = max(creation_ready, start_ready)
            else:
                ready_at_dt = creation_ready

            vm_ready_at = ready_at_dt.isoformat()
            now = datetime.now(timezone.utc)
            timer_ready = now >= ready_at_dt

            # After the timer expires, verify the VM is actually serving HTTPS.
            # The timer is a lower-bound estimate; Caddy may still be starting
            # up or waiting for its ACME certificate.
            if timer_ready and external_ip:
                vm_ready = _probe_vm_https(hostname)
            else:
                vm_ready = False

        return {
            "vm_name": vm_name,
            "assistant_id": assistant_id,
            "status": instance.status,
            "ip_address": external_ip,
            "hostname": hostname,
            "desktop_url": f"https://{hostname}" if external_ip else None,
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


def provision_vm_full(
    assistant_id: str,
    unify_apikey: str,
    assistant_name: str,
    vm_type: str = "windows",
) -> Dict[str, Any]:
    """
    Full provisioning of a VM (Windows or Ubuntu):
    1. Generate SSH keypair for file sync
    2. Store SSH private key as assistant secret
    3. Reserve static IP
    4. Create DNS A record
    5. Create and start VM with SSH public key

    Args:
        assistant_id: The assistant ID (numeric string)
        unify_apikey: Unify API key (used for VNC, Windows password, and secret storage)
        assistant_name: Assistant identifier, typically str(assistant_id) (used for Windows/SSH username)
        vm_type: "windows" or "ubuntu"

    Returns:
        Dict with full VM details including ssh_username and ssh_port.
    """
    logger.info(
        f"Starting full provisioning for assistant: {assistant_id} (type: {vm_type})",
    )

    # Step 1: Generate SSH keypair for file sync
    # Note: assistant_name is typically str(assistant_id) passed by the caller
    ssh_username = assistant_name
    private_key, public_key = generate_ssh_keypair()
    logger.info(f"Generated SSH keypair for user: {ssh_username}")

    # Step 2: Store private key as assistant secret
    key_stored = store_ssh_private_key(assistant_id, private_key, unify_apikey)
    if not key_stored:
        logger.warning(
            f"Failed to store SSH private key for assistant {assistant_id}, "
            "file sync may not work",
        )

    # Step 3: Reserve static IP
    static_ip = reserve_static_ip(assistant_id, vm_type)
    logger.info(f"Reserved static IP: {static_ip}")

    # Step 4: Create DNS record (shared hostname for both types)
    hostname = get_dns_hostname(assistant_id)
    create_dns_record(assistant_id, static_ip)
    logger.info(f"Created DNS record: {hostname} -> {static_ip}")

    # Step 5: Create VM based on type (with SSH credentials)
    if vm_type == "ubuntu":
        # Ubuntu uses ssh_username for SSH file sync
        result = create_ubuntu_vm(
            assistant_id=assistant_id,
            static_ip=static_ip,
            hostname=hostname,
            unify_apikey=unify_apikey,
            assistant_name=assistant_name,
            ssh_public_key=public_key,
            ssh_username=ssh_username,
        )
    else:
        # Windows uses windows-username for SSH (no separate ssh_username)
        result = create_windows_vm(
            assistant_id=assistant_id,
            static_ip=static_ip,
            hostname=hostname,
            unify_apikey=unify_apikey,
            assistant_name=assistant_name,
            ssh_public_key=public_key,
        )

    logger.info(f"Full provisioning complete for assistant: {assistant_id}")
    return result


def deprovision_vm_full(assistant_id: str, vm_type: str = "windows") -> Dict[str, Any]:
    """
    Full deprovisioning of a VM (Windows or Ubuntu):
    1. Delete VM
    2. Delete DNS record
    3. Release static IP

    Args:
        assistant_id: The assistant ID
        vm_type: "windows" or "ubuntu"

    Returns:
        Dict with deprovisioning status.
    """
    logger.info(
        f"Starting full deprovisioning for assistant: {assistant_id} (type: {vm_type})",
    )

    results = {
        "assistant_id": assistant_id,
        "vm_deleted": False,
        "dns_deleted": False,
        "ip_released": False,
    }

    # Step 1: Delete VM
    results["vm_deleted"] = delete_vm(assistant_id, vm_type)

    # Step 2: Delete DNS record (shared hostname)
    results["dns_deleted"] = delete_dns_record(assistant_id)

    # Step 3: Release static IP
    results["ip_released"] = release_static_ip(assistant_id, vm_type)

    logger.info(f"Full deprovisioning complete for assistant: {assistant_id}")
    return results


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

    metadata_items = [
        compute_v1.Items(key=cfg["startup_script_key"], value=startup_script),
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


def claim_idle_vm(assistant_id: str, vm_type: str) -> Dict[str, Any]:
    """Atomically claim an idle pool VM using label fingerprint CAS.

    Retries on Conflict (another request claimed the same VM).
    Waits up to 300s if VMs are provisioning but none idle yet.
    Raises ValueError if no idle VMs are available.
    """
    client = compute_v1.InstancesClient()
    label_filter = (
        f"labels.pool-role=idle AND labels.vm-type={vm_type} AND status=RUNNING"
    )
    max_wait = 300
    elapsed = 0

    while True:
        request = compute_v1.ListInstancesRequest(
            project=VM_PROJECT_ID,
            zone=ZONE,
            filter=label_filter,
        )
        idle_vms = list(client.list(request=request))
        if not idle_vms:
            provisioning_request = compute_v1.ListInstancesRequest(
                project=VM_PROJECT_ID,
                zone=ZONE,
                filter=f"labels.pool-role=provisioning AND labels.vm-type={vm_type}",
            )
            provisioning_vms = list(client.list(request=provisioning_request))
            if provisioning_vms and elapsed < max_wait:
                logger.info(
                    f"{len(provisioning_vms)} {vm_type} VMs provisioning, "
                    f"waiting for idle (elapsed {elapsed}s)..."
                )
                time.sleep(5)
                elapsed += 5
                continue
            raise ValueError(f"No idle {vm_type} pool VMs available")

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
) -> Dict[str, Any]:
    """Full pool assignment: claim VM, create/attach disk, set metadata."""
    claimed = claim_idle_vm(assistant_id, vm_type)
    vm_name = claimed["vm_name"]

    # Create disk if it doesn't exist, then attach
    create_assistant_disk(assistant_id)
    device_name = attach_assistant_disk(vm_name, assistant_id)

    # Generate SSH keypair and store private key
    private_key, public_key = generate_ssh_keypair()
    store_ssh_private_key(assistant_id, private_key, unify_apikey)

    # Update metadata to trigger watcher reconfiguration
    _update_instance_metadata(
        vm_name,
        {
            "unify-key": unify_apikey,
            "vnc-password": unify_apikey,
            "ssh-public-key": public_key,
            "disk-device": device_name,
            "assistant-id": assistant_id,
        },
    )

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
    if len(idle_vms) <= 2:
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

    # Rule 2: ensure stopped reserve
    effective_stopped = len(stopped_vms) - (1 if started_one else 0)
    if effective_stopped <= 2:
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

    # Scale down: too many idle VMs
    if len(idle_vms) > POOL_TARGET_IDLE:
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

    return actions
