"""
VM Management - Request/Response Models

Supports both Windows and Ubuntu VMs via vm_type parameter.
"""

from pydantic import BaseModel
from typing import Optional, Literal


class VMReadyRequest(BaseModel):
    assistant_id: str
    vm_type: Literal["windows", "ubuntu"] = "windows"
    hostname: Optional[str] = None


# =============================================================================
# VM Pool Models
# =============================================================================


class PoolProvisionRequest(BaseModel):
    vm_type: Literal["windows", "ubuntu"] = "ubuntu"
    count: int = 1


class PoolAssignRequest(BaseModel):
    assistant_id: str
    unify_apikey: str
    vm_type: Literal["windows", "ubuntu"] = "ubuntu"
    vm_number: Optional[int] = None


class PoolAssignResponse(BaseModel):
    vm_name: str
    assistant_id: str
    ip_address: str
    hostname: str
    desktop_url: str
    status: str
    ssh_username: str
    ssh_port: int


class PoolReleaseRequest(BaseModel):
    assistant_id: str


class PoolDiskDeleteRequest(BaseModel):
    assistant_id: str


class PoolVMStatus(BaseModel):
    vm_name: str
    pool_role: str
    assistant_id: Optional[str] = None
    vm_type: str
    ip_address: Optional[str] = None
    hostname: str
    status: str


class PoolStatusResponse(BaseModel):
    vms: list[PoolVMStatus]
    total: int
    idle: int
    assigned: int
    provisioning: int
    stopped: int


# =============================================================================
# Tunnel Management Models
# =============================================================================


class VMWipeMetadataKeyRequest(BaseModel):
    key: str


class TunnelRegisterRequest(BaseModel):
    """Request to register a new tunnel for a user's local application."""

    local_port: int = 8080  # Client's local application port
    name: Optional[str] = None  # Optional friendly name for the tunnel


class TunnelRegisterResponse(BaseModel):
    """Response with tunnel details and client setup instructions."""

    tunnel_id: str  # Short unique ID (e.g., "x7k9m2p4")
    hostname: str  # e.g., x7k9m2p4.tunnel.unify.ai
    url: str  # https://x7k9m2p4.tunnel.unify.ai
    status: str  # "pending" — becomes "connected" when client connects
    client_token: str  # Secret token for tunnel authentication
    client_config: str  # rathole client TOML config content
    setup_commands: dict[str, str]  # Per-OS commands (keys: "bash", "powershell")


class TunnelStatusResponse(BaseModel):
    """Tunnel status information."""

    tunnel_id: str
    hostname: str
    url: str
    status: str  # "pending" | "connected" | "disconnected"
    name: Optional[str] = None
    local_port: int
    created_at: Optional[str] = None  # ISO timestamp


class TunnelListResponse(BaseModel):
    """List of tunnels for a user."""

    tunnels: list[TunnelStatusResponse]
    total: int


class TunnelDeleteResponse(BaseModel):
    """Response after deleting a tunnel."""

    tunnel_id: str
    deleted: bool
    message: str
