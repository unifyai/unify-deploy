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
    ssh_username: Optional[str] = None  # SSH username for file sync (e.g., "JohnDoe")
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
