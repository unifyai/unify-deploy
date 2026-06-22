"""
VM Management - Request/Response Models

Supports both Windows and Ubuntu VMs via vm_type parameter.
"""

from pydantic import BaseModel
from typing import Optional, Literal
from datetime import datetime


class VMReadyRequest(BaseModel):
    assistant_id: str
    binding_id: str
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
    binding_id: str
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


class PoolStartVMRequest(BaseModel):
    vm_type: Literal["windows", "ubuntu"] = "ubuntu"
    vm_number: int


class PoolReleaseRequest(BaseModel):
    assistant_id: str
    binding_id: str
    vm_name: Optional[str] = None
    job_name: Optional[str] = None
    release_generation: Optional[int] = None


class ScheduledTaskActivationUpsertRequest(BaseModel):
    """Idempotent request to materialize one scheduled activation."""

    assistant_id: str
    destination: Optional[str] = None
    task_id: int
    source_task_log_id: int
    activation_revision: str
    scheduled_for: datetime
    execution_mode: Literal["live", "offline"] = "live"
    entrypoint: Optional[int] = None
    source_type: Literal["scheduled"] = "scheduled"
    task_label: Optional[str] = None
    task_summary: Optional[str] = None
    visibility_policy: str = "silent_by_default"
    recurrence_hint: str = "one_off"
    previous_activation_revision: Optional[str] = None
    previous_scheduled_for: Optional[datetime] = None
    previous_execution_mode: Optional[Literal["live", "offline"]] = None


class ScheduledTaskActivationDeleteRequest(BaseModel):
    """Delete one previously materialized scheduled activation."""

    assistant_id: str
    destination: Optional[str] = None
    task_id: int
    activation_revision: str
    scheduled_for: datetime
    execution_mode: Literal["live", "offline"] = "live"


class OfflineTaskDispatchRequest(BaseModel):
    """Dispatch one validated offline task execution attempt."""

    assistant_id: str
    destination: Optional[str] = None
    task_id: int
    source_task_log_id: int
    activation_revision: str
    execution_mode: Literal["offline"] = "offline"
    entrypoint: Optional[int] = None
    source_type: Literal["scheduled", "triggered"] = "scheduled"
    scheduled_for: Optional[datetime] = None
    source_ref: Optional[str] = None
    source_medium: Optional[str] = None
    source_contact_id: Optional[int] = None
    source_contact_display_name: Optional[str] = None
    task_name: Optional[str] = None
    task_description: Optional[str] = None


class TaskActivationDiagnosticRequest(BaseModel):
    """Inspect task activation materialization for one assistant task."""

    assistant_id: str
    task_id: int
    source_task_log_id: Optional[int] = None


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


class VMReleaseCompleteRequest(BaseModel):
    binding_id: str
    release_generation: Optional[int] = None


class TunnelRegisterRequest(BaseModel):
    """Request to register a new tunnel for a user's local application."""

    local_port: int = 8080  # Client's local application port
    name: Optional[str] = None  # Optional friendly name for the tunnel
    protocol: str = "http"  # "http" (Caddy-fronted) or "tcp" (raw TCP, e.g. SFTP)


class TunnelRegisterResponse(BaseModel):
    """Response with tunnel details and client setup instructions."""

    tunnel_id: str  # Short unique ID (e.g., "x7k9m2p4")
    hostname: str  # e.g., x7k9m2p4.tunnel.unify.ai
    url: str  # https://x7k9m2p4.tunnel.unify.ai
    status: str  # "pending" — becomes "connected" when client connects
    client_token: str  # Secret token for tunnel authentication
    client_config: str  # rathole client TOML config content
    setup_commands: dict[str, str]  # Per-OS commands (keys: "bash", "powershell")
    tcp_host: Optional[str] = None  # Raw-TCP tunnels only: host to dial
    tcp_port: Optional[int] = None  # Raw-TCP tunnels only: port to dial


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
