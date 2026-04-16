from unity_deploy.runtime.assistant_jobs_backend import HostedAssistantJobsBackend
from unity_deploy.runtime.log_archive import HostedShutdownLogBackend
from unity_deploy.runtime.metrics_backend import HostedMetricsBackend
from unity_deploy.runtime.session_assignment import HostedSessionAssignmentBackend

_SESSION_BACKEND = HostedSessionAssignmentBackend()
_JOBS_BACKEND = HostedAssistantJobsBackend()
_METRICS_BACKEND = HostedMetricsBackend()
_LOG_BACKEND = HostedShutdownLogBackend()


def get_runtime_backend_overrides() -> dict[str, object]:
    return {
        "session": _SESSION_BACKEND,
        "jobs": _JOBS_BACKEND,
        "metrics": _METRICS_BACKEND,
        "logs": _LOG_BACKEND,
    }


def register_runtime_backends() -> dict[str, object]:
    return get_runtime_backend_overrides()
