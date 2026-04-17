"""Unit tests for the scheduled Teams watch renewal endpoint.

Focuses on the provider-gate logic (us-provisioned vs BYOD vs Gmail),
the switch from direct Graph calls to the comms-service
``/teams/subscriptions`` endpoint, and licensing-error classification.
"""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from adapters.main import _is_teams_licensing_error, app


def _resp(
    status_code: int, json_payload: dict | None = None, text: str = ""
) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_payload if json_payload is not None else {}
    r.text = text
    return r


def _assistants_payload(assistants: list[dict]) -> MagicMock:
    return _resp(200, {"info": assistants})


def test_is_teams_licensing_error_detects_payment_and_extension_errors():
    assert _is_teams_licensing_error("ExtensionError: PaymentRequired")
    assert _is_teams_licensing_error("Operation: Create; PaymentRequired")
    assert _is_teams_licensing_error(
        "change notifications subscription requires payment model",
    )
    assert _is_teams_licensing_error("Billing not configured")
    assert not _is_teams_licensing_error("validation timeout")
    assert not _is_teams_licensing_error("")
    assert not _is_teams_licensing_error(None)  # type: ignore[arg-type]


def test_scheduled_teams_watches_skips_gmail_and_includes_both_ms_modes():
    """Gmail assistants are skipped; BYOD and us-provisioned both get watched."""

    client = TestClient(app)

    byod = {
        "email": "byod@unify.ai",
        "email_provider": "microsoft_365",
        "secrets": {"MICROSOFT_ACCESS_TOKEN": "user-token"},
    }
    provisioned = {
        "email": "provisioned@unify.ai",
        "email_provider": "microsoft_365",
        "secrets": {},
    }
    gmail = {
        "email": "gmail@unify.ai",
        "email_provider": "google_workspace",
        "secrets": {},
    }

    watch_post_emails: list[str] = []
    subs_get_emails: list[str] = []

    def fake_get(url, *args, **kwargs):
        if url.endswith("/admin/assistant"):
            return _assistants_payload([byod, provisioned, gmail])
        if url.endswith("/teams/subscriptions"):
            subs_get_emails.append(kwargs.get("params", {}).get("primary_email", ""))
            return _resp(200, {"success": True, "subscriptions": []})
        return _resp(404)

    def fake_post(url, *args, **kwargs):
        if url.endswith("/teams/watch"):
            email = kwargs.get("json", {}).get("primary_email")
            watch_post_emails.append(email)
            return _resp(
                200,
                {
                    "success": True,
                    "action": "created",
                    "subscription_id": f"sub-{email}",
                },
            )
        if url.endswith("/teams/watch-channel"):
            return _resp(200, {"success": True, "subscription_id": "sub-ch"})
        return _resp(404)

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.requests.get", side_effect=fake_get),
        patch("adapters.main.requests.post", side_effect=fake_post),
    ):
        response = client.post(
            "/scheduled/teams-watches",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"test": False},
        )

    assert response.status_code == 200
    body = response.json()

    assert sorted(watch_post_emails) == ["byod@unify.ai", "provisioned@unify.ai"]
    assert sorted(subs_get_emails) == ["byod@unify.ai", "provisioned@unify.ai"]

    renewed_emails = sorted(entry["email"] for entry in body["chats_renewed"])
    assert renewed_emails == ["byod@unify.ai", "provisioned@unify.ai"]
    assert body["failed"] == []
    assert body["skipped"] == []


def test_scheduled_teams_watches_classifies_licensing_errors_as_skipped():
    """App-only chat subs failing on licensing go to ``skipped`` not ``failed``."""

    client = TestClient(app)

    provisioned = {
        "email": "provisioned@unify.ai",
        "email_provider": "microsoft_365",
        "secrets": {},
    }

    def fake_get(url, *args, **kwargs):
        if url.endswith("/admin/assistant"):
            return _assistants_payload([provisioned])
        if url.endswith("/teams/subscriptions"):
            return _resp(200, {"success": True, "subscriptions": []})
        return _resp(404)

    def fake_post(url, *args, **kwargs):
        if url.endswith("/teams/watch"):
            return _resp(
                500,
                {"success": False, "error": "ExtensionError: PaymentRequired"},
                text="ExtensionError: PaymentRequired",
            )
        return _resp(404)

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.requests.get", side_effect=fake_get),
        patch("adapters.main.requests.post", side_effect=fake_post),
    ):
        response = client.post(
            "/scheduled/teams-watches",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"test": False},
        )

    assert response.status_code == 200
    body = response.json()

    assert body["chats_renewed"] == []
    assert body["failed"] == []
    assert len(body["skipped"]) == 1
    skip_entry = body["skipped"][0]
    assert skip_entry["email"] == "provisioned@unify.ai"
    assert skip_entry["type"] == "chat"
    assert skip_entry["reason"] == "licensing"


def test_scheduled_teams_watches_fallback_provider_detection_via_token():
    """Assistants without ``email_provider`` fall back to token-sniffing."""

    client = TestClient(app)

    legacy_ms = {
        "email": "legacy@unify.ai",
        "secrets": {"MICROSOFT_ACCESS_TOKEN": "legacy-token"},
    }
    legacy_gmail = {
        "email": "legacy-gmail@unify.ai",
        "secrets": {},
    }

    watch_post_emails: list[str] = []

    def fake_get(url, *args, **kwargs):
        if url.endswith("/admin/assistant"):
            return _assistants_payload([legacy_ms, legacy_gmail])
        if url.endswith("/teams/subscriptions"):
            return _resp(200, {"success": True, "subscriptions": []})
        return _resp(404)

    def fake_post(url, *args, **kwargs):
        if url.endswith("/teams/watch"):
            email = kwargs.get("json", {}).get("primary_email")
            watch_post_emails.append(email)
            return _resp(200, {"success": True, "subscription_id": "sub"})
        return _resp(404)

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.requests.get", side_effect=fake_get),
        patch("adapters.main.requests.post", side_effect=fake_post),
    ):
        response = client.post(
            "/scheduled/teams-watches",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"test": False},
        )

    assert response.status_code == 200
    assert watch_post_emails == ["legacy@unify.ai"]


def test_scheduled_teams_watches_renews_channel_subscriptions_via_comms_endpoint():
    """Channel subscriptions discovered through /teams/subscriptions get renewed."""

    client = TestClient(app)

    provisioned = {
        "email": "provisioned@unify.ai",
        "email_provider": "microsoft_365",
        "secrets": {},
    }

    channel_posts: list[dict] = []

    def fake_get(url, *args, **kwargs):
        if url.endswith("/admin/assistant"):
            return _assistants_payload([provisioned])
        if url.endswith("/teams/subscriptions"):
            return _resp(
                200,
                {
                    "success": True,
                    "subscriptions": [
                        {
                            "id": "sub-1",
                            "resource": ("/teams/team-abc/channels/chan-xyz/messages"),
                        },
                        {
                            "id": "sub-2",
                            "resource": "/users/foo@bar.com/chats/getAllMessages",
                        },
                    ],
                },
            )
        return _resp(404)

    def fake_post(url, *args, **kwargs):
        if url.endswith("/teams/watch"):
            return _resp(200, {"success": True, "subscription_id": "sub-chat"})
        if url.endswith("/teams/watch-channel"):
            channel_posts.append(kwargs.get("json", {}))
            return _resp(200, {"success": True, "subscription_id": "sub-ch"})
        return _resp(404)

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.requests.get", side_effect=fake_get),
        patch("adapters.main.requests.post", side_effect=fake_post),
    ):
        response = client.post(
            "/scheduled/teams-watches",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"test": False},
        )

    assert response.status_code == 200
    body = response.json()

    assert len(channel_posts) == 1
    assert channel_posts[0] == {
        "primary_email": "provisioned@unify.ai",
        "team_id": "team-abc",
        "channel_id": "chan-xyz",
    }
    assert len(body["channels_renewed"]) == 1
