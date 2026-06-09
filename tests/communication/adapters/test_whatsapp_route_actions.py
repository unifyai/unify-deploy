"""WhatsApp adapter route-action tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from adapters import main


def test_twilio_whatsapp_reject_ambiguous_returns_closed_response(monkeypatch):
    monkeypatch.setattr(
        main,
        "resolve_whatsapp_route",
        lambda pool_number, sender: {"action": "reject_ambiguous"},
    )
    main.app.dependency_overrides[main.validate_twilio_wa_signature] = lambda: None
    try:
        with TestClient(main.app) as client:
            response = client.post(
                "/twilio/whatsapp",
                data={
                    "To": "whatsapp:+15550800001",
                    "From": "whatsapp:+15550800002",
                    "Body": "Hello",
                },
            )
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "text/xml" in response.headers["content-type"]
    assert "This number is not accepting new messages." in response.text
