"""Live bootstrap plumbing test for assistant space memberships."""

from __future__ import annotations

import time
import uuid
import json

import pytest
import requests

from communication.infra.assistant_sessions import read_bootstrap_secret
from tests.communication.infra.integration.conftest import (
    ADMIN_KEY,
    NAMESPACE,
    ORCHESTRA_URL,
    UNIFY_KEY,
    _admin_record_to_data,
    cleanup_assistant_jobs,
    get_assistant_session,
    start_real_job,
    wait_for_assistant_container_ready,
)


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _create_space(name: str) -> int:
    response = requests.post(
        f"{ORCHESTRA_URL}/spaces",
        json={
            "name": name,
            "description": "Communication bootstrap plumbing integration test",
            "organization_id": None,
        },
        headers=_headers(UNIFY_KEY),
        timeout=30,
    )
    assert response.status_code == 201, response.text
    return int(response.json()["space_id"])


def _add_member(space_id: int, assistant_id: str) -> None:
    response = requests.post(
        f"{ORCHESTRA_URL}/spaces/{space_id}/members",
        json={"assistant_id": int(assistant_id)},
        headers=_headers(UNIFY_KEY),
        timeout=30,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["membership_status"] == "active", body


def _fetch_assistant_data(assistant_id: str) -> dict:
    response = requests.get(
        f"{ORCHESTRA_URL}/admin/assistant",
        params={"agent_id": assistant_id},
        headers=_headers(ADMIN_KEY),
        timeout=30,
    )
    assert response.status_code == 200, response.text
    return _admin_record_to_data(response.json()["info"][0])


def _remove_member(space_id: int, assistant_id: str) -> None:
    requests.delete(
        f"{ORCHESTRA_URL}/spaces/{space_id}/members/{assistant_id}",
        headers=_headers(UNIFY_KEY),
        timeout=30,
    )


def _delete_space(space_id: int) -> None:
    requests.delete(
        f"{ORCHESTRA_URL}/spaces/{space_id}",
        headers=_headers(UNIFY_KEY),
        timeout=30,
    )


@pytest.mark.integration
@pytest.mark.slow
def test_two_spaces_round_trip(
    comms,
    real_assistant_data,
    batch_api,
    core_api,
    gce_client,
):
    """A real start-job bootstrap secret carries the assistant's live spaces."""

    assert UNIFY_KEY, "UNIFY_KEY required to create spaces"
    assert ADMIN_KEY, "ORCHESTRA_ADMIN_KEY required to fetch assistant data"

    assistant_id = str(real_assistant_data["assistant_id"])
    space_ids: list[int] = []
    name_prefix = f"space-plumbing-{int(time.time())}-{uuid.uuid4().hex[:8]}"

    try:
        space_ids.append(_create_space(f"{name_prefix}-a"))
        space_ids.append(_create_space(f"{name_prefix}-b"))
        for space_id in space_ids:
            _add_member(space_id, assistant_id)

        assistant_data = _fetch_assistant_data(assistant_id)
        assert sorted(json.loads(assistant_data["space_ids"])) == sorted(space_ids)

        start_real_job(comms, assistant_data)
        session = get_assistant_session(comms, assistant_id)
        assert session is not None
        secret_name = str((session.get("spec") or {}).get("startupSecretRef") or "")
        assert secret_name, f"Expected startupSecretRef on session: {session}"
        bootstrap_payload = read_bootstrap_secret(core_api, NAMESPACE, secret_name)
        assert sorted(bootstrap_payload["space_ids"]) == sorted(space_ids)

        wait_for_assistant_container_ready(
            assistant_id,
            batch_api=batch_api,
            core_api=core_api,
            gce_client=gce_client,
            timeout=300,
            interval=5,
        )
    finally:
        cleanup_assistant_jobs(
            batch_api,
            [assistant_id],
            context="space-ids-plumbing",
        )
        for space_id in space_ids:
            _remove_member(space_id, assistant_id)
        for space_id in space_ids:
            _delete_space(space_id)
