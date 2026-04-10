from __future__ import annotations

from dataclasses import dataclass
import os

from common.settings import SETTINGS


@dataclass(frozen=True)
class ControllerConfig:
    """Immutable controller configuration loaded from the process environment."""

    watch_namespace: str
    reconcile_interval_seconds: float
    container_bootstrap_deadline_seconds: float
    max_bootstrap_retries: int
    vm_readiness_deadline_seconds: float
    max_vm_readiness_retries: int
    vm_assignment_retry_interval_seconds: float
    vm_assignment_in_progress_timeout_seconds: float
    desktop_liveness_failure_threshold: int
    image_hash_bucket: str
    image_hash_cache_ttl: float
    job_claim_lease_duration_seconds: int

    @classmethod
    def from_env(cls) -> "ControllerConfig":
        vm_readiness_deadline_seconds = float(
            os.environ.get("VM_READINESS_DEADLINE_SECONDS", "300"),
        )
        return cls(
            watch_namespace=os.environ.get(
                "WATCH_NAMESPACE",
                SETTINGS.default_namespace,
            ),
            reconcile_interval_seconds=float(
                os.environ.get("SESSION_RECONCILE_INTERVAL", "5"),
            ),
            container_bootstrap_deadline_seconds=float(
                os.environ.get("CONTAINER_BOOTSTRAP_DEADLINE_SECONDS", "90"),
            ),
            max_bootstrap_retries=int(os.environ.get("MAX_BOOTSTRAP_RETRIES", "2")),
            vm_readiness_deadline_seconds=vm_readiness_deadline_seconds,
            max_vm_readiness_retries=int(
                os.environ.get("MAX_VM_READINESS_RETRIES", "2"),
            ),
            vm_assignment_retry_interval_seconds=float(
                os.environ.get("VM_ASSIGNMENT_RETRY_INTERVAL_SECONDS", "5"),
            ),
            vm_assignment_in_progress_timeout_seconds=float(
                os.environ.get("VM_ASSIGNMENT_IN_PROGRESS_TIMEOUT_SECONDS", "60"),
            ),
            desktop_liveness_failure_threshold=int(
                os.environ.get("DESKTOP_LIVENESS_FAILURE_THRESHOLD", "3"),
            ),
            image_hash_bucket="unity-image-hash",
            image_hash_cache_ttl=float(os.environ.get("IMAGE_HASH_CACHE_TTL", "60")),
            job_claim_lease_duration_seconds=int(
                os.environ.get(
                    "JOB_CLAIM_LEASE_DURATION_SECONDS",
                    str(SETTINGS.lease_duration_seconds),
                ),
            ),
        )
