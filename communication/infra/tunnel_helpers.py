"""
Tunnel Management Helpers

Control-plane logic for the tunnel relay service.
Runs in Cloud Run (comms app). Manages tunnel state in GCS and pushes
configuration updates that the data-plane VM watches and hot-reloads.

State storage:
  gs://bucket/registry.json   – active tunnel metadata
  gs://bucket/server.toml     – rathole server config
  gs://bucket/port-map.json   – tunnel_id → internal port (for Caddy)
"""

import json
import logging
import os
import secrets
import string
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from google.cloud import storage
from google.oauth2.service_account import Credentials

from common.settings import SETTINGS
from .tunnel_config import (
    TUNNEL_REGISTRY_BLOB,
    TUNNEL_SERVER_CONFIG_BLOB,
    TUNNEL_PORT_MAP_BLOB,
    TUNNEL_CONTROL_PORT,
    TUNNEL_PORT_RANGE_START,
    TUNNEL_PORT_RANGE_END,
)

logger = logging.getLogger(__name__)


# =============================================================================
# GCS Client
# =============================================================================


def _get_storage_client() -> storage.Client:
    """Get an authenticated GCS client using the service account key."""
    creds_json = os.getenv("GCP_SA_KEY")
    if creds_json:
        creds = Credentials.from_service_account_info(json.loads(creds_json))
        return storage.Client(credentials=creds)
    # Fall back to Application Default Credentials
    return storage.Client()


# =============================================================================
# Registry (GCS JSON file)
# =============================================================================


def _load_registry() -> Dict[str, Any]:
    """
    Load the tunnel registry from GCS.

    Returns:
        Dict mapping tunnel_id → tunnel metadata.
    """
    client = _get_storage_client()
    bucket = client.bucket(SETTINGS.tunnel_gcs_bucket)
    blob = bucket.blob(TUNNEL_REGISTRY_BLOB)

    try:
        content = blob.download_as_text()
        return json.loads(content)
    except Exception:
        # File doesn't exist or is empty — return empty registry
        logger.info("No existing registry found, starting fresh")
        return {}


def _save_registry(registry: Dict[str, Any]) -> None:
    """Save the tunnel registry to GCS."""
    client = _get_storage_client()
    bucket = client.bucket(SETTINGS.tunnel_gcs_bucket)
    blob = bucket.blob(TUNNEL_REGISTRY_BLOB)
    blob.upload_from_string(
        json.dumps(registry, indent=2, default=str),
        content_type="application/json",
    )
    logger.info(f"Saved registry with {len(registry)} tunnel(s)")


# =============================================================================
# Server Config Push (GCS → tunnel VM watches)
# =============================================================================


def _push_server_config(registry: Dict[str, Any]) -> None:
    """
    Rebuild rathole server.toml and Caddy port-map.json from registry,
    then upload both to GCS. The tunnel VM watches these files and
    hot-reloads rathole + Caddy when they change.
    """
    client = _get_storage_client()
    bucket = client.bucket(SETTINGS.tunnel_gcs_bucket)

    # Build server.toml
    lines = [
        "[server]",
        f'bind_addr = "0.0.0.0:{TUNNEL_CONTROL_PORT}"',
        "",
    ]
    port_map = {}

    for tunnel_id, tunnel in registry.items():
        token = tunnel["token"]
        internal_port = tunnel["internal_port"]
        lines.append(f"[server.services.{tunnel_id}]")
        lines.append(f'token = "{token}"')
        lines.append(f'bind_addr = "0.0.0.0:{internal_port}"')
        lines.append("")
        port_map[tunnel_id] = internal_port

    if not registry:
        lines.append("[server.services]")
        lines.append("")

    server_toml = "\n".join(lines)

    # Upload server.toml
    blob_toml = bucket.blob(TUNNEL_SERVER_CONFIG_BLOB)
    blob_toml.upload_from_string(server_toml, content_type="text/plain")

    # Upload port-map.json
    blob_map = bucket.blob(TUNNEL_PORT_MAP_BLOB)
    blob_map.upload_from_string(
        json.dumps(port_map, indent=2),
        content_type="application/json",
    )

    logger.info(f"Pushed server config ({len(registry)} tunnel(s)) and port-map to GCS")


# =============================================================================
# ID / Token / Port Generation
# =============================================================================


def generate_tunnel_id(length: int = 8) -> str:
    """Generate a short unique tunnel ID (lowercase alphanumeric)."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_tunnel_token(length: int = 32) -> str:
    """Generate a secure token for tunnel authentication."""
    return secrets.token_urlsafe(length)


def allocate_port(registry: Dict[str, Any]) -> int:
    """
    Allocate an unused internal port for a new tunnel.
    Scans the registry to avoid collisions.
    """
    used_ports = {t["internal_port"] for t in registry.values()}
    # Start from a random point in the range to reduce clustering
    import random

    start = random.randint(TUNNEL_PORT_RANGE_START, TUNNEL_PORT_RANGE_END)
    for offset in range(TUNNEL_PORT_RANGE_END - TUNNEL_PORT_RANGE_START):
        port = TUNNEL_PORT_RANGE_START + (
            (start - TUNNEL_PORT_RANGE_START + offset)
            % (TUNNEL_PORT_RANGE_END - TUNNEL_PORT_RANGE_START)
        )
        if port not in used_ports:
            return port
    raise RuntimeError("No available ports in tunnel range")


# =============================================================================
# Hostname / URL Helpers
# =============================================================================


def get_tunnel_hostname(tunnel_id: str) -> str:
    """Get the public hostname for a tunnel."""
    return f"{tunnel_id}.{SETTINGS.tunnel_subdomain}"


def get_tunnel_url(tunnel_id: str) -> str:
    """Get the public HTTPS URL for a tunnel."""
    return f"https://{get_tunnel_hostname(tunnel_id)}"


# =============================================================================
# Client Config Generation
# =============================================================================


def generate_client_config(
    tunnel_id: str,
    token: str,
    local_port: int,
) -> str:
    """Generate rathole client TOML config content."""
    return (
        f"# Unity Tunnel — config for tunnel: {tunnel_id}\n"
        f"# Save as client.toml and run: rathole client.toml\n"
        f"\n"
        f"[client]\n"
        f'remote_addr = "{SETTINGS.tunnel_subdomain}:{TUNNEL_CONTROL_PORT}"\n'
        f'default_token = "{token}"\n'
        f"heartbeat_timeout = 40\n"
        f"retry_interval = 5\n"
        f"\n"
        f"[client.services.{tunnel_id}]\n"
        f'local_addr = "127.0.0.1:{local_port}"\n'
    )


def generate_setup_commands(
    tunnel_id: str,
    token: str,
    local_port: int,
) -> Dict[str, str]:
    """Generate per-OS one-liner install + start commands."""
    bash_cmd = (
        f"curl -sSL https://{SETTINGS.tunnel_subdomain}/install.sh | bash -s -- "
        f'--token "{token}" '
        f'--tunnel-id "{tunnel_id}" '
        f"--local-port {local_port}"
    )
    ps_cmd = (
        f"irm https://{SETTINGS.tunnel_subdomain}/install.ps1 -OutFile install.ps1; "
        f'.\\install.ps1 -Token "{token}" '
        f'-TunnelId "{tunnel_id}" '
        f"-LocalPort {local_port}; "
        f"Remove-Item install.ps1"
    )
    return {"bash": bash_cmd, "powershell": ps_cmd}


# =============================================================================
# Tunnel Lifecycle
# =============================================================================


def register_tunnel(
    user_id: str,
    local_port: int = 8080,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Register a new tunnel for a user.

    1. Generate tunnel_id and token
    2. Allocate unused internal port
    3. Add to registry in GCS
    4. Push updated server.toml and port-map.json to GCS
    5. Return response with client config and setup command

    Args:
        user_id: The user who owns this tunnel.
        local_port: The port on the client's machine to tunnel to.
        name: Optional friendly name for the tunnel.

    Returns:
        Dict with tunnel details (matches TunnelRegisterResponse).
    """
    registry = _load_registry()

    tunnel_id = generate_tunnel_id()
    # Ensure uniqueness (extremely unlikely collision with 8-char random)
    while tunnel_id in registry:
        tunnel_id = generate_tunnel_id()

    token = generate_tunnel_token()
    internal_port = allocate_port(registry)

    # Store tunnel entry
    registry[tunnel_id] = {
        "tunnel_id": tunnel_id,
        "user_id": user_id,
        "token": token,
        "internal_port": internal_port,
        "local_port": local_port,
        "name": name,
        "status": "pending",
        "hostname": get_tunnel_hostname(tunnel_id),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # Persist registry and push config to tunnel VM
    _save_registry(registry)
    _push_server_config(registry)

    logger.info(
        f"Registered tunnel {tunnel_id} for user {user_id} "
        f"(internal port {internal_port})",
    )

    return {
        "tunnel_id": tunnel_id,
        "hostname": get_tunnel_hostname(tunnel_id),
        "url": get_tunnel_url(tunnel_id),
        "status": "pending",
        "client_token": token,
        "client_config": generate_client_config(tunnel_id, token, local_port),
        "setup_commands": generate_setup_commands(tunnel_id, token, local_port),
    }


def unregister_tunnel(tunnel_id: str, user_id: str) -> Dict[str, Any]:
    """
    Unregister and delete a tunnel.

    1. Verify user owns the tunnel
    2. Remove from registry
    3. Push updated config to GCS

    Args:
        tunnel_id: The tunnel to delete.
        user_id: The requesting user (for ownership check).

    Returns:
        Dict with deletion status (matches TunnelDeleteResponse).
    """
    registry = _load_registry()

    if tunnel_id not in registry:
        return {
            "tunnel_id": tunnel_id,
            "deleted": False,
            "message": "Tunnel not found",
        }

    tunnel = registry[tunnel_id]
    if tunnel["user_id"] != user_id:
        return {
            "tunnel_id": tunnel_id,
            "deleted": False,
            "message": "Not authorized to delete this tunnel",
        }

    del registry[tunnel_id]

    _save_registry(registry)
    _push_server_config(registry)

    logger.info(f"Unregistered tunnel {tunnel_id} for user {user_id}")

    return {
        "tunnel_id": tunnel_id,
        "deleted": True,
        "message": "Tunnel deleted",
    }


def get_tunnel_status(tunnel_id: str) -> Optional[Dict[str, Any]]:
    """
    Get the current status of a tunnel.

    Args:
        tunnel_id: The tunnel to look up.

    Returns:
        Dict with tunnel status (matches TunnelStatusResponse), or None.
    """
    registry = _load_registry()

    if tunnel_id not in registry:
        return None

    tunnel = registry[tunnel_id]
    return {
        "tunnel_id": tunnel["tunnel_id"],
        "user_id": tunnel["user_id"],
        "hostname": tunnel["hostname"],
        "url": get_tunnel_url(tunnel_id),
        "status": tunnel.get("status", "pending"),
        "name": tunnel.get("name"),
        "local_port": tunnel["local_port"],
        "created_at": tunnel.get("created_at"),
    }


def list_user_tunnels(user_id: str) -> Dict[str, Any]:
    """
    List all tunnels owned by a user.

    Args:
        user_id: The user whose tunnels to list.

    Returns:
        Dict with list of tunnels (matches TunnelListResponse).
    """
    registry = _load_registry()

    tunnels = []
    for tunnel_id, tunnel in registry.items():
        if tunnel["user_id"] == user_id:
            tunnels.append(
                {
                    "tunnel_id": tunnel["tunnel_id"],
                    "hostname": tunnel["hostname"],
                    "url": get_tunnel_url(tunnel_id),
                    "status": tunnel.get("status", "pending"),
                    "name": tunnel.get("name"),
                    "local_port": tunnel["local_port"],
                    "created_at": tunnel.get("created_at"),
                },
            )

    return {
        "tunnels": tunnels,
        "total": len(tunnels),
    }
