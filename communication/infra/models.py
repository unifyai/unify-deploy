"""
VM Management - Request/Response Models

Supports both Windows and Ubuntu VMs via vm_type parameter.
"""

from pydantic import BaseModel
from typing import Optional, Literal


class VMCreateRequest(BaseModel):
    assistant_id: str  # Numeric ID (e.g., "12345")
    unify_apikey: (
        str  # Required - used for VNC password, Windows password, and secret storage
    )
    assistant_name: str  # Required - used for Windows/SSH username
    vm_type: Literal["windows", "ubuntu"] = "windows"  # VM type: "windows" or "ubuntu"


class VMActionRequest(BaseModel):
    assistant_id: str
    vm_type: Literal["windows", "ubuntu"] = "windows"  # VM type: "windows" or "ubuntu"


class VMCreateResponse(BaseModel):
    vm_name: str
    assistant_id: str
    ip_address: str
    hostname: str  # Format: unity-assistant-{id}.vm.unify.ai
    desktop_url: str  # https://unity-assistant-{id}.vm.unify.ai
    status: str
    # SSH file sync configuration
    ssh_username: Optional[str] = None  # SSH username for file sync (str(agent_id))
    ssh_port: Optional[int] = None  # SSH port for file sync (2222)


class VMStatusResponse(BaseModel):
    vm_name: str
    assistant_id: str
    status: str
    ip_address: Optional[str]
    hostname: str
    desktop_url: Optional[str]
    machine_type: str
    zone: str
    # Timestamps
    creation_timestamp: Optional[str]  # ISO timestamp when VM was created
    last_start_timestamp: Optional[str]  # ISO timestamp when VM was last started
    # Readiness (max of creation+15min, last_start+2min)
    vm_ready_at: Optional[str]  # ISO timestamp when VM will be ready
    vm_ready: bool  # True if VM is ready now


class VMActionResponse(BaseModel):
    vm_name: str
    assistant_id: str
    status: str
    message: str


class VMDeleteResponse(BaseModel):
    assistant_id: str
    vm_deleted: bool
    dns_deleted: bool
    ip_released: bool


class VMReadyRequest(BaseModel):
    assistant_id: str
    vm_type: Literal["windows", "ubuntu"] = "windows"


# =============================================================================
# Tunnel Management Models
# =============================================================================


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
