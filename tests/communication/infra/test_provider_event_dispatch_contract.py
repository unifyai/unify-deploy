"""Provider-event dispatch envelope contract tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from communication.infra.provider_event_dispatch import ProviderEventDispatchRequest

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "task_trigger_contract"


def test_provider_event_dispatch_request_v1_forbids_extra_and_rejects_raw_payload() -> None:
    payload = json.loads(
        (_FIXTURE_DIR / "provider_event_dispatch_request.v1.json").read_text(
            encoding="utf-8",
        ),
    )
    payload["dispatch_mode"] = "offline"
    payload["audience"] = "communication:provider-event-dispatch"
    request = ProviderEventDispatchRequest.model_validate(payload)
    assert request.contract_version == "1"
    assert request.dispatch_mode == "offline"

    with pytest.raises(ValueError):
        ProviderEventDispatchRequest.model_validate({**payload, "raw_body": "secret"})
