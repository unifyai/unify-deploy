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
import json
import logging
import random
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Dict, Any, Tuple

import requests
from google.cloud import compute_v1
from google.cloud import dns
from google.cloud import secretmanager
from google.api_core.exceptions import NotFound, Conflict, PreconditionFailed
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from common.settings import SETTINGS


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
    POOL_ASSISTANT_DISK_SIZE_GB,
    POOL_ASSISTANT_DISK_TYPE,
    POOL_VM_NAME_PREFIX,
    POOL_UBUNTU_VM_IMAGE_FAMILY,
    POOL_WINDOWS_VM_IMAGE_FAMILY,
)

logger = logging.getLogger(__name__)


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
                **_compact_vm_log_fields(fields),
            },
            sort_keys=True,
            default=str,
        ),
    )


# ---------------------------------------------------------------------------
# Demand tracking for pool replenishment
# ---------------------------------------------------------------------------
_pending_claims: Dict[str, int] = {}
_pending_lock = threading.Lock()

_replenish_locks: Dict[str, threading.Lock] = {}
_replenish_locks_guard = threading.Lock()

_trim_locks: Dict[str, threading.Lock] = {}
_trim_locks_guard = threading.Lock()

_vm_claim_locks: Dict[str, threading.Lock] = {}
_vm_claim_locks_guard = threading.Lock()


def _get_replenish_lock(vm_type: str) -> threading.Lock:
    with _replenish_locks_guard:
        if vm_type not in _replenish_locks:
            _replenish_locks[vm_type] = threading.Lock()
        return _replenish_locks[vm_type]


def _get_trim_lock(vm_type: str) -> threading.Lock:
    with _trim_locks_guard:
        if vm_type not in _trim_locks:
            _trim_locks[vm_type] = threading.Lock()
        return _trim_locks[vm_type]


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


def _instance_boot_reference_time(instance) -> Optional[datetime]:
    return _parse_gce_timestamp(
        getattr(instance, "last_start_timestamp", None)
        or getattr(instance, "creation_timestamp", None),
    )


def _is_stale_inflight_vm(
    instance,
    now: Optional[datetime] = None,
    timeout_seconds: int = POOL_BOOT_TIMEOUT_SECONDS,
) -> bool:
    labels = dict(instance.labels) if instance.labels else {}
    if labels.get("pool-role") not in ("starting", "provisioning"):
        return False
    reference_time = _instance_boot_reference_time(instance)
    if reference_time is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - reference_time).total_seconds() > timeout_seconds


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
        try:
            client.stop(
                project=SETTINGS.vm_project_id,
                zone=SETTINGS.vm_zone,
                instance=instance.name,
            ).result()
        except Exception as exc:
            logger.error(
                "Quarantine: failed stopping %s after quarantine: %s",
                instance.name,
                exc,
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


def _quarantine_stale_inflight_vms(vm_type: str) -> list[str]:
    client = compute_v1.InstancesClient()
    request = compute_v1.ListInstancesRequest(
        project=SETTINGS.vm_project_id,
        zone=SETTINGS.vm_zone,
        filter=f"labels.vm-type={vm_type}",
    )
    now = datetime.now(timezone.utc)
    actions: list[str] = []
    for instance in client.list(request=request):
        if not _is_stale_inflight_vm(instance, now=now):
            continue
        reference_time = _instance_boot_reference_time(instance)
        if reference_time is None:
            continue
        age_seconds = int((now - reference_time).total_seconds())
        action = _quarantine_pool_vm(
            client,
            instance,
            reason=(
                f"pool-role={dict(instance.labels or {}).get('pool-role', '')}, "
                f"status={instance.status}, age={age_seconds}s"
            ),
        )
        if action:
            actions.append(action)
    return actions


def _probe_vm_https(hostname: str, timeout: float = 5.0) -> bool:
    """Probe whether the agent-service is alive behind Caddy.

    Hits ``/api/sessions`` which Caddy reverse-proxies to the agent-service
    on port 3000.  Without an Authorization header the agent-service auth
    middleware returns **401** immediately — proving the process is up.
    If the agent-service is down but Caddy is running, Caddy returns **502**.
    If everything is down the connection fails outright.

    This is strictly stronger than probing the Caddy root (``/``), which
    only confirms Caddy itself is listening and tells us nothing about
    the agent-service process.

    Uses ``verify=False`` because Caddy may still be using a temporary
    self-signed certificate while the ACME challenge completes.  The
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


def probe_vm_https(hostname: str, timeout: float = 5.0) -> bool:
    """Public wrapper — check if a VM's agent-service is reachable."""
    return _probe_vm_https(hostname, timeout)


def probe_vm_agent_authenticated(
    hostname: str,
    api_key: str,
    timeout: float = 10.0,
) -> bool:
    """Verify the VM agent is reachable with the expected bearer token.

    This is stricter than plain HTTPS reachability: it proves the agent
    process is up and accepts the key that the session expects.
    """
    if not api_key:
        return False
    try:
        resp = requests.post(
            f"https://{hostname}/api/exec",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"command": "echo assistant-session-ready", "timeout": 5000},
            timeout=timeout,
            verify=False,
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
    return f"{POOL_VM_NAME_PREFIX}-{vm_type}-{n}{SETTINGS.env_suffix}"


def _pool_ip_name(vm_type: str, n: int) -> str:
    return f"{POOL_VM_NAME_PREFIX}-{vm_type}-ip-{n}{SETTINGS.env_suffix}"


def _pool_hostname(vm_type: str, n: int) -> str:
    return f"{POOL_VM_NAME_PREFIX}-{vm_type}-{n}{SETTINGS.env_suffix}.{DOMAIN_SUFFIX}"


def _assistant_disk_name(assistant_id: str) -> str:
    sanitized = assistant_id.lower().replace("_", "-")
    return f"unity-disk-{sanitized}{SETTINGS.env_suffix}"


def find_vm_with_disk(assistant_id: str) -> Optional[str]:
    """Find a VM that has the assistant's disk attached, regardless of labels.

    Scans all VMs in the zone. Returns the VM name, or None.
    """
    client = compute_v1.InstancesClient()
    disk_name = _assistant_disk_name(assistant_id)
    disk_suffix = f"/disks/{disk_name}"

    request = compute_v1.ListInstancesRequest(
        project=SETTINGS.vm_project_id,
        zone=SETTINGS.vm_zone,
    )
    for instance in client.list(request=request):
        if instance.disks:
            for d in instance.disks:
                if d.source and d.source.endswith(disk_suffix):
                    return instance.name
    return None


def list_pool_vms(vm_type: Optional[str] = None) -> list[Dict[str, Any]]:
    """List all pool VMs, optionally filtered by type."""
    client = compute_v1.InstancesClient()

    label_filter = f"labels.pool-role:*"
    if vm_type:
        label_filter += f" AND labels.vm-type={vm_type}"

    request = compute_v1.ListInstancesRequest(
        project=SETTINGS.vm_project_id,
        zone=SETTINGS.vm_zone,
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
        entry: Dict[str, Any] = {
            "vm_name": instance.name,
            "pool_role": labels.get("pool-role", "unknown"),
            "assistant_id": labels.get("assistant-id", "") or None,
            "vm_type": labels.get("vm-type", "unknown"),
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
            region=SETTINGS.vm_region,
            address_resource=address,
        )
        op.result()
        logger.info(f"Reserved static IP: {ip_name}")
    except Conflict:
        logger.info(f"Static IP {ip_name} already exists, reusing")

    ip_result = ip_client.get(
        project=SETTINGS.vm_project_id,
        region=SETTINGS.vm_region,
        address=ip_name,
    )
    static_ip = ip_result.address

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

    # Fetch secrets
    github_token = get_secret("DEVBOT_GITHUB_TOKEN")
    tls_cert = get_secret(VM_WILDCARD_CERT_SECRET)
    tls_key = get_secret(VM_WILDCARD_KEY_SECRET)

    startup_script = cfg["startup_script_loader"]()
    pool_watcher_script = cfg["pool_watcher_loader"]()

    metadata_items = [
        compute_v1.Items(key=cfg["startup_script_key"], value=startup_script),
        compute_v1.Items(key="pool-watcher-script", value=pool_watcher_script),
    ]

    supervisord_conf_loader = cfg.get("supervisord_conf_loader")
    if supervisord_conf_loader:
        metadata_items.append(
            compute_v1.Items(key="supervisord-conf", value=supervisord_conf_loader()),
        )

    metadata_items += [
        compute_v1.Items(key="hostname", value=hostname),
        compute_v1.Items(key="orchestra-url", value=SETTINGS.orchestra_url),
        compute_v1.Items(key="comms-url", value=SETTINGS.comms_url),
    ]
    if github_token:
        metadata_items.append(compute_v1.Items(key="github-token", value=github_token))
    if tls_cert and tls_key:
        metadata_items.append(compute_v1.Items(key="tls-fullchain", value=tls_cert))
        metadata_items.append(compute_v1.Items(key="tls-privkey", value=tls_key))
    metadata_items.append(
        compute_v1.Items(key="unity-environment", value=SETTINGS.deploy_env),
    )

    labels = {
        "pool-role": "provisioning",
        "assistant-id": "",
        "vm-type": vm_type,
        "pool-hostname": hostname.replace(".", "-"),
    }

    instance_kwargs = dict(
        name=vm_name,
        machine_type=f"zones/{SETTINGS.vm_zone}/machineTypes/{cfg['machine_type']}",
        description=f"Unity pool VM ({vm_type}) #{n}",
        labels=labels,
        tags=compute_v1.Tags(items=cfg["tags"]),
        disks=[
            compute_v1.AttachedDisk(
                boot=True,
                auto_delete=True,
                initialize_params=compute_v1.AttachedDiskInitializeParams(
                    disk_size_gb=cfg["disk_size_gb"],
                    disk_type=f"zones/{SETTINGS.vm_zone}/diskTypes/{VM_DISK_TYPE}",
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
        zone=SETTINGS.vm_zone,
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
            zone=SETTINGS.vm_zone,
            instance=vm_name,
        )
        labels = dict(fresh.labels) if fresh.labels else {}
        if expected_role is not None and labels.get("pool-role") != expected_role:
            logger.info(
                f"Skipping label update on {vm_name}: "
                f"expected pool-role={expected_role}, "
                f"got {labels.get('pool-role')}",
            )
            return False
        labels.update(label_overrides)
        try:
            client.set_labels(
                project=SETTINGS.vm_project_id,
                zone=SETTINGS.vm_zone,
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


def claim_idle_vm(
    assistant_id: str,
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
        f"labels.pool-role=idle AND labels.vm-type={vm_type} AND status=RUNNING"
    )

    with _pending_lock:
        _pending_claims[vm_type] = _pending_claims.get(vm_type, 0) + 1

    try:
        return _claim_idle_vm_inner(
            client,
            label_filter,
            assistant_id,
            vm_type,
            vm_number,
        )
    finally:
        with _pending_lock:
            _pending_claims[vm_type] = max(0, _pending_claims.get(vm_type, 0) - 1)


def _claim_idle_vm_inner(
    client,
    label_filter: str,
    assistant_id: str,
    vm_type: str,
    vm_number: int | None,
) -> Dict[str, Any]:
    while True:
        request = compute_v1.ListInstancesRequest(
            project=SETTINGS.vm_project_id,
            zone=SETTINGS.vm_zone,
            filter=label_filter,
        )
        idle_vms = list(client.list(request=request))
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
                zone=SETTINGS.vm_zone,
                instance=candidate_name,
            )
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
            if hostname and not _probe_vm_https(hostname, timeout=2.0):
                logger.warning(
                    "Idle pool VM %s failed HTTPS probe before claim; quarantining",
                    candidate_name,
                )
                _log_vm_pool_event(
                    "claim_probe_failed",
                    assistant_id=assistant_id,
                    vm_name=candidate_name,
                    vm_type=vm_type,
                    hostname=hostname,
                )
                _quarantine_pool_vm(
                    client,
                    fresh,
                    reason="failed health probe during claim",
                )
                continue

            sanitized = assistant_id.lower().replace("_", "-")
            new_labels = dict(fresh.labels) if fresh.labels else {}
            new_labels["pool-role"] = "assigned"
            new_labels["assistant-id"] = sanitized

            try:
                op = client.set_labels(
                    project=SETTINGS.vm_project_id,
                    zone=SETTINGS.vm_zone,
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
        type_=f"zones/{SETTINGS.vm_zone}/diskTypes/{POOL_ASSISTANT_DISK_TYPE}",
        description=f"Persistent storage for assistant {assistant_id}",
    )

    try:
        op = client.insert(
            project=SETTINGS.vm_project_id,
            zone=SETTINGS.vm_zone,
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
        zone=SETTINGS.vm_zone,
        disk=disk_name,
    )
    return result.self_link


def attach_assistant_disk(vm_name: str, assistant_id: str) -> str:
    """Attach an assistant's persistent disk to a pool VM.

    Returns the device name used for mounting.
    """
    client = compute_v1.InstancesClient()
    disk_name = _assistant_disk_name(assistant_id)
    disk_source = (
        f"projects/{SETTINGS.vm_project_id}/zones/{SETTINGS.vm_zone}/disks/{disk_name}"
    )

    attached_disk = compute_v1.AttachedDisk(
        source=disk_source,
        device_name=disk_name,
        auto_delete=False,
        mode="READ_WRITE",
    )

    op = client.attach_disk(
        project=SETTINGS.vm_project_id,
        zone=SETTINGS.vm_zone,
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
        zone=SETTINGS.vm_zone,
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
        zone=SETTINGS.vm_zone,
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
    client = compute_v1.DisksClient()
    disk_name = _assistant_disk_name(assistant_id)

    try:
        op = client.delete(
            project=SETTINGS.vm_project_id,
            zone=SETTINGS.vm_zone,
            disk=disk_name,
        )
        op.result()
        logger.info(f"Deleted assistant disk: {disk_name}")
        return True
    except NotFound:
        logger.warning(f"Assistant disk {disk_name} not found")
        return False


def _update_instance_metadata(
    vm_name: str,
    updates: Dict[str, str],
    max_retries: int = 3,
) -> None:
    """Update metadata on a running instance (merge with existing).

    Retries on PreconditionFailed (412) which occurs when the metadata
    fingerprint is stale due to a concurrent update.
    """
    client = compute_v1.InstancesClient()

    for attempt in range(max_retries + 1):
        instance = client.get(
            project=SETTINGS.vm_project_id,
            zone=SETTINGS.vm_zone,
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
                zone=SETTINGS.vm_zone,
                instance=vm_name,
                metadata_resource=metadata,
            )
            op.result()
            logger.info(f"Updated metadata on {vm_name}: {list(updates.keys())}")
            _log_vm_pool_event(
                "metadata_update",
                vm_name=vm_name,
                metadata_keys=sorted(updates.keys()),
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


def assign_pool_vm(
    assistant_id: str,
    unify_apikey: str,
    vm_type: str = "ubuntu",
    vm_number: int | None = None,
) -> Dict[str, Any]:
    """Full pool assignment: release any existing VM, claim a fresh one.

    Always releases the assistant's current VM (if any) before claiming,
    ensuring metadata, disk, and labels are cleanly handed back to the pool.

    Uses a per-assistant K8s Lease to prevent concurrent calls for the
    same assistant from claiming multiple VMs (RACE-3).
    """
    from .helpers import (
        acquire_assignment_lease,
        release_assignment_lease,
        setup_kubernetes_client,
    )

    _, _, _, coord_api = setup_kubernetes_client()
    holder_id = f"vm-assign-{uuid.uuid4().hex[:8]}"
    namespace = SETTINGS.default_namespace

    acquired = acquire_assignment_lease(
        coord_api,
        f"vm-{assistant_id}",
        namespace,
        holder_id,
        duration=180,
    )
    if not acquired:
        raise RuntimeError(
            f"Another VM assignment is already in progress for assistant {assistant_id}",
        )

    try:
        release_pool_vm(assistant_id)
        claimed = claim_idle_vm(assistant_id, vm_type, vm_number=vm_number)
        vm_name = claimed["vm_name"]
        create_assistant_disk(assistant_id)
        device_name = attach_assistant_disk(vm_name, assistant_id)
        existing_key = _fetch_existing_ssh_key(assistant_id)
        if existing_key:
            private_key = existing_key
            public_key = _derive_public_key(existing_key)
        else:
            private_key, public_key = generate_ssh_keypair()
            store_ssh_private_key(assistant_id, private_key)

        github_token = get_secret("DEVBOT_GITHUB_TOKEN") or ""
        metadata = {
            "unify-key": unify_apikey,
            "vnc-password": unify_apikey,
            "ssh-public-key": public_key,
            "disk-device": device_name,
            "assistant-id": assistant_id,
            "github-token": github_token,
        }
        if vm_type == "windows" and MAK_KEY:
            metadata["office-mak-key"] = MAK_KEY
        _update_instance_metadata(vm_name, metadata)

        logger.info(
            f"Pool assignment complete: {vm_name} -> assistant {assistant_id}",
        )
        _log_vm_pool_event(
            "assign_complete",
            assistant_id=assistant_id,
            vm_name=vm_name,
            vm_type=vm_type,
            hostname=claimed["hostname"],
            disk_device=device_name,
        )
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
    finally:
        release_assignment_lease(coord_api, f"vm-{assistant_id}", namespace)


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
        zone=SETTINGS.vm_zone,
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


def verify_vm_assignment(
    vm_name: str,
    assistant_id: str,
) -> Optional[Dict[str, Any]]:
    """Strongly consistent check: GET the named VM and verify its labels.

    Returns a vm_ref dict if the instance exists with pool-role=assigned
    and the expected assistant-id. Returns None otherwise.
    """
    client = compute_v1.InstancesClient()
    sanitized = assistant_id.lower().replace("_", "-")
    try:
        vm = client.get(
            project=SETTINGS.vm_project_id,
            zone=SETTINGS.vm_zone,
            instance=vm_name,
        )
    except Exception:
        return None
    labels = dict(vm.labels) if vm.labels else {}
    if labels.get("pool-role") != "assigned":
        return None
    if labels.get("assistant-id") != sanitized:
        return None
    return _vm_ref_from_instance(vm)


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


def reconcile_orphaned_vms(batch_api, vm_type: str = "ubuntu") -> Dict[str, Any]:
    """Release VMs assigned to assistants that no longer have live K8s Jobs.

    When a K8s pod crashes or is force-deleted, release_pool_vm is never
    called, leaving the VM stuck in pool-role=assigned. This reconciler
    detects such orphans by cross-referencing the K8s Job list and releases
    them back to the pool.

    Uses the same non-terminal job semantics as the AssistantSession
    controller so that pods in restart-backoff are not mistaken for
    dead jobs.

    Idempotent and safe to call on a cron schedule.
    """
    client = compute_v1.InstancesClient()
    request = compute_v1.ListInstancesRequest(
        project=SETTINGS.vm_project_id,
        zone=SETTINGS.vm_zone,
        filter=f"labels.pool-role=assigned AND labels.vm-type={vm_type}",
    )
    assigned_vms = list(client.list(request=request))

    released = []
    kept = []
    for vm in assigned_vms:
        aid = vm.labels.get("assistant-id", "")
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

        if not live_jobs:
            logger.info(
                "Orphaned VM %s assigned to %s (no live K8s Job) — releasing",
                vm.name,
                aid,
            )
            try:
                release_pool_vm(aid)
                released.append({"vm_name": vm.name, "assistant_id": aid})
            except Exception as e:
                logger.error(
                    "Failed to release orphaned VM %s: %s",
                    vm.name,
                    e,
                )
        else:
            kept.append({"vm_name": vm.name, "assistant_id": aid})

    return {
        "checked": len(assigned_vms),
        "released": released,
        "kept": kept,
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
        zone=SETTINGS.vm_zone,
        filter=f"labels.pool-role=quarantined AND labels.vm-type={vm_type}",
    )
    quarantined = list(client.list(request=request))
    if not quarantined:
        return {"found": 0, "deleted": [], "errors": []}

    deleted: list[str] = []
    errors: list[dict] = []

    def _delete_one(vm) -> Optional[str]:
        try:
            client.delete(
                project=SETTINGS.vm_project_id,
                zone=SETTINGS.vm_zone,
                instance=vm.name,
            ).result()
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


def release_pool_vm(assistant_id: str) -> Dict[str, Any]:
    """Release a pool VM: clear metadata, detach disk, reset labels.

    Idempotent — returns success if no VM is currently assigned.
    """
    client = compute_v1.InstancesClient()
    sanitized = assistant_id.lower().replace("_", "-")
    label_filter = f"labels.pool-role=assigned AND labels.assistant-id={sanitized}"

    request = compute_v1.ListInstancesRequest(
        project=SETTINGS.vm_project_id,
        zone=SETTINGS.vm_zone,
        filter=label_filter,
    )
    vms = list(client.list(request=request))
    if not vms:
        logger.info(
            f"No pool VM assigned to assistant {assistant_id} — nothing to release",
        )
        _log_vm_pool_event(
            "release_skipped",
            assistant_id=assistant_id,
            reason="no_assigned_vm",
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

    _set_pool_labels(client, vm_name, {"pool-role": "idle", "assistant-id": ""})

    logger.info(f"Released pool VM {vm_name} from assistant {assistant_id}")
    _log_vm_pool_event(
        "release",
        assistant_id=assistant_id,
        vm_name=vm_name,
    )

    vm_type = (dict(vm.labels) if vm.labels else {}).get("vm-type", "ubuntu")
    return {
        "released": True,
        "assistant_id": assistant_id,
        "vm_name": vm_name,
        "vm_type": vm_type,
    }


def _list_pool_state(vm_type: str):
    """Snapshot current pool state for a VM type.

    Returns (client, pool_vms, idle_vms, stopped_vms, in_flight_vms, existing_names).
    in_flight_vms are VMs that are booting but not yet idle (provisioning or
    recently started stopped VMs).
    """
    client = compute_v1.InstancesClient()
    type_filter = f"labels.vm-type={vm_type}"
    request = compute_v1.ListInstancesRequest(
        project=SETTINGS.vm_project_id,
        zone=SETTINGS.vm_zone,
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
        if vm.labels.get("pool-role") == "stopped" and vm.status == "TERMINATED"
    ]
    in_flight_vms = [
        vm
        for vm in pool_vms
        if vm.status in ("STAGING", "RUNNING")
        and vm.labels.get("pool-role") in ("provisioning", "starting")
    ]
    existing_names = {vm.name for vm in pool_vms}
    return client, pool_vms, idle_vms, stopped_vms, in_flight_vms, existing_names


def _start_one_stopped_vm(client, vm) -> bool:
    """Start a single stopped VM.

    Sets pool-role=starting BEFORE issuing client.start() so the scrub
    function (which targets pool-role=stopped + RUNNING) cannot kill a
    VM that is legitimately booting. The startup script transitions
    starting → idle via mark-idle once boot completes.

    Restores the github-token metadata before starting, because the
    previous boot's startup script wipes it for security. Without it,
    the startup script can't clone private repos and crashes (set -e).
    """
    try:
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

        github_token = get_secret("DEVBOT_GITHUB_TOKEN") or ""
        if github_token:
            _update_instance_metadata(vm.name, {"github-token": github_token})

        op = client.start(
            project=SETTINGS.vm_project_id,
            zone=SETTINGS.vm_zone,
            instance=vm.name,
        )
        logger.info(
            "Replenish: start request submitted for stopped VM %s "
            "(pool-role=starting)",
            vm.name,
        )
        _log_vm_pool_event(
            "replenish_start",
            vm_name=vm.name,
            vm_type=(dict(vm.labels) if vm.labels else {}).get("vm-type", "ubuntu"),
            operation_name=getattr(op, "name", None),
        )
        return True
    except Exception as e:
        logger.error(f"Replenish: failed to start {vm.name}: {e}")
        _set_pool_labels(client, vm.name, {"pool-role": "stopped"})
        _log_vm_pool_event(
            "replenish_start_failed",
            vm_name=vm.name,
            vm_type=(dict(vm.labels) if vm.labels else {}).get("vm-type", "ubuntu"),
            error=str(e),
        )
        return False


def start_pool_vm(vm_type: str, vm_number: int) -> Dict[str, Any]:
    """Start a specific stopped pool VM by type and number.

    Uses the same stopped → starting label transition as
    _start_one_stopped_vm so the VM is protected from scrub during
    boot and can legitimately transition to idle via mark-idle.
    """
    vm_name = _pool_vm_name(vm_type, vm_number)
    client = compute_v1.InstancesClient()

    try:
        vm = client.get(
            project=SETTINGS.vm_project_id,
            zone=SETTINGS.vm_zone,
            instance=vm_name,
        )
    except NotFound:
        raise ValueError(f"VM {vm_name} not found")

    if vm.status != "TERMINATED":
        raise Conflict(f"VM {vm_name} is {vm.status}, expected TERMINATED")

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

    github_token = get_secret("DEVBOT_GITHUB_TOKEN") or ""
    if github_token:
        _update_instance_metadata(vm_name, {"github-token": github_token})

    try:
        op = client.start(
            project=SETTINGS.vm_project_id,
            zone=SETTINGS.vm_zone,
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
        zone=SETTINGS.vm_zone,
        filter=f"labels.pool-role=idle AND labels.vm-type={vm_type} AND status=RUNNING",
    )
    idle_vms = list(client.list(request=request))
    if not idle_vms:
        return []

    actions: list[str] = []

    def _check_one(vm) -> Optional[str]:
        ref = _vm_ref_from_instance(vm)
        hostname = ref.get("hostname", "")
        if not hostname:
            return None
        if _probe_vm_https(hostname, timeout=2.0):
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
    actions = _quarantine_stale_inflight_vms(vm_type)
    actions.extend(_scrub_inconsistent_vms(vm_type))
    actions.extend(_probe_and_quarantine_unhealthy_idle_vms(vm_type))

    client, _, idle_vms, stopped_vms, in_flight_vms, existing_names = _list_pool_state(
        vm_type,
    )

    with _pending_lock:
        pending = _pending_claims.get(vm_type, 0)

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
            f = pool.submit(_start_one_stopped_vm, client, vm)
            futures[f] = f"Started stopped VM {vm.name}"
        for num in numbers_to_provision:
            f = pool.submit(provision_pool_vm, vm_type, num)
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
                    rf = pool.submit(provision_pool_vm, vm_type, nr)
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


def trim_pool(vm_type: str) -> Dict[str, Any]:
    """Stop excess idle VMs to maintain POOL_TARGET_IDLE.

    Uses a non-blocking per-vm_type lock so concurrent callers (fire-and-
    forget from release_pool_endpoint) don't duplicate work.
    """
    lock = _get_trim_lock(vm_type)
    if not lock.acquire(blocking=False):
        return {"vm_type": vm_type, "actions": [], "skipped": True}
    try:
        return _trim_pool_inner(vm_type)
    finally:
        lock.release()


def _trim_pool_inner(vm_type: str) -> Dict[str, Any]:
    """Uses label-first ordering: CAS-sets pool-role from idle to stopped
    before issuing the stop, so a concurrent claim that already flipped
    the label to assigned will cause the CAS to fail cleanly.

    Re-verifies idle count on each iteration so that concurrent claims
    reducing the pool below target cause the loop to break early.
    """
    client = compute_v1.InstancesClient()
    actions: list[str] = []
    max_iterations = 10

    for _ in range(max_iterations):
        try:
            _, _, idle_vms, _, _, _ = _list_pool_state(vm_type)
            if len(idle_vms) <= POOL_TARGET_IDLE:
                break

            candidate = sorted(idle_vms, key=lambda v: v.name, reverse=True)[0]

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
                    zone=SETTINGS.vm_zone,
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
                zone=SETTINGS.vm_zone,
                instance=candidate.name,
            )
            if fresh.status == "RUNNING":
                logger.warning(
                    f"Trim: {candidate.name} still RUNNING after stop, retrying",
                )
                try:
                    client.stop(
                        project=SETTINGS.vm_project_id,
                        zone=SETTINGS.vm_zone,
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
            )
        except Exception as e:
            # Per-VM errors (e.g. CAS exhausting retries, or a failed
            # label revert after a failed stop) must not abort the loop
            # — remaining VMs still need processing.
            logger.error(f"Trim: failed to process VM: {e}")

    _, _, final_idle, _, _, _ = _list_pool_state(vm_type)
    return {"vm_type": vm_type, "idle_count": len(final_idle), "actions": actions}


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
        zone=SETTINGS.vm_zone,
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
                client.stop(
                    project=SETTINGS.vm_project_id,
                    zone=SETTINGS.vm_zone,
                    instance=vm.name,
                ).result()
                msg = f"Scrub: stopped {vm.name} ({anomaly})"
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
    """Full rebalance: scrub ghosts, replenish, then trim. For manual use."""
    scrub_actions = _scrub_inconsistent_vms(vm_type)
    replenish_result = replenish_pool(vm_type)
    trim_result = trim_pool(vm_type)
    return {
        "vm_type": vm_type,
        "actions": scrub_actions + replenish_result["actions"] + trim_result["actions"],
    }


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
        zone=SETTINGS.vm_zone,
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
