import time

import pytest
from kubernetes.client.rest import ApiException

from communication.infra.assistant_sessions import (
    assistant_session_name,
    assistant_session_secret_name,
)

from .conftest import (
    NAMESPACE,
    _create_test_assistant,
    _delete_test_assistant,
    get_assistant_session,
    list_assigned_vms,
    list_jobs_with_assistant_id,
    list_jobs_with_session_ref,
    start_real_job,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _secret_exists(core_api, secret_name: str) -> bool:
    try:
        core_api.read_namespaced_secret(name=secret_name, namespace=NAMESPACE)
    except ApiException as exc:
        if exc.status == 404:
            return False
        raise
    return True


def test_delete_session_endpoint_tears_down_pending_runtime(
    comms,
    batch_api,
    core_api,
    gce_client,
    job_tracker,
    poll,
):
    assistant = _create_test_assistant(int(time.time() * 1000) % 1000000)
    assistant_id = str(assistant["assistant_id"])
    session_name = assistant_session_name(assistant_id)
    secret_name = assistant_session_secret_name(assistant_id)

    try:
        start_real_job(comms, assistant)

        session = poll(
            lambda: (
                candidate
                if (
                    (candidate := get_assistant_session(comms, assistant_id))
                    and (
                        (candidate.get("status") or {}).get("phase")
                        in {
                            "ContainerAssigned",
                            "PendingContainer",
                            "PendingVM",
                        }
                    )
                )
                else None
            ),
            timeout=180,
            interval=5,
            description=f"AssistantSession {assistant_id} to reach a pending phase",
            failure_snapshot=lambda: get_assistant_session(comms, assistant_id),
        )
        assert session is not None

        job_name = ((session.get("status") or {}).get("jobRef") or {}).get("name")
        assert job_name, f"Expected jobRef in pending session, got: {session}"
        job_tracker.track(job_name)

        delete_resp = comms.delete(f"/infra/session/{assistant_id}")
        assert (
            delete_resp.status_code == 200
        ), f"session delete failed: {delete_resp.status_code} {delete_resp.text}"
        assert delete_resp.json()["deleted"] is True

        poll(
            lambda: (
                get_assistant_session(comms, assistant_id) is None
                and not _secret_exists(core_api, secret_name)
                and not list_jobs_with_session_ref(batch_api, session_name)
                and not list_jobs_with_assistant_id(batch_api, assistant_id)
                and (
                    gce_client is None
                    or not list_assigned_vms(gce_client, assistant_id)
                )
            ),
            timeout=240,
            interval=5,
            description=f"AssistantSession teardown for {assistant_id}",
            failure_snapshot=lambda: {
                "session": get_assistant_session(comms, assistant_id),
                "secret_exists": _secret_exists(core_api, secret_name),
                "session_jobs": [
                    job.metadata.name
                    for job in list_jobs_with_session_ref(batch_api, session_name)
                ],
                "assistant_jobs": [
                    job.metadata.name
                    for job in list_jobs_with_assistant_id(batch_api, assistant_id)
                ],
                "assigned_vms": (
                    []
                    if gce_client is None
                    else [vm.name for vm in list_assigned_vms(gce_client, assistant_id)]
                ),
            },
        )
    finally:
        _delete_test_assistant(assistant_id, batch_api)
