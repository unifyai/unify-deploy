"""Provider-event dispatch envelope contract tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from communication.infra.provider_event_dispatch import (
    PROVIDER_EVENT_DISPATCH_AUDIENCE,
    ProviderEventDispatchRequest,
    ProviderEventDispatchValidationError,
    validate_provider_event_dispatch_request,
)

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "fixtures" / "task_trigger_contract"
)


def _fixture_payload(**overrides) -> dict:
    payload = json.loads(
        (_FIXTURE_DIR / "provider_event_dispatch_request.v1.json").read_text(
            encoding="utf-8",
        ),
    )
    payload["dispatch_mode"] = "offline"
    payload["audience"] = PROVIDER_EVENT_DISPATCH_AUDIENCE
    payload.update(overrides)
    return payload


def test_provider_event_dispatch_request_v1_forbids_extra_and_rejects_raw_payload() -> (
    None
):
    request = ProviderEventDispatchRequest.model_validate(_fixture_payload())
    assert request.contract_version == "1"
    assert request.dispatch_mode == "offline"

    with pytest.raises(ValueError):
        ProviderEventDispatchRequest.model_validate(
            {**_fixture_payload(), "raw_body": "secret"},
        )


def test_provider_event_dispatch_validator_rejects_invalid_audience_mode_and_ttl() -> (
    None
):
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    request = ProviderEventDispatchRequest.model_validate(_fixture_payload())
    validate_provider_event_dispatch_request(request, ttl_seconds=300, now=now)

    with pytest.raises(ProviderEventDispatchValidationError) as exc:
        validate_provider_event_dispatch_request(
            ProviderEventDispatchRequest.model_validate(
                _fixture_payload(audience="unity:provider-event-dispatch"),
            ),
            ttl_seconds=300,
            now=now,
        )
    assert exc.value.reason_code == "invalid_audience"

    with pytest.raises(ProviderEventDispatchValidationError) as exc:
        validate_provider_event_dispatch_request(
            ProviderEventDispatchRequest.model_validate(
                _fixture_payload(dispatch_mode="live"),
            ),
            ttl_seconds=300,
            now=now,
        )
    assert exc.value.reason_code == "invalid_dispatch_mode"

    with pytest.raises(ProviderEventDispatchValidationError) as exc:
        validate_provider_event_dispatch_request(
            ProviderEventDispatchRequest.model_validate(
                _fixture_payload(
                    issued_at=(now - timedelta(minutes=10)).isoformat(),
                ),
            ),
            ttl_seconds=300,
            now=now,
        )
    assert exc.value.reason_code == "dispatch_request_expired"
