"""Unit tests for the Teams watch resource-path selector."""

from communication.teams.views import _chat_watch_resource


def test_chat_watch_resource_uses_me_for_delegated_token():
    assert (
        _chat_watch_resource("user@example.com", has_user_token=True)
        == "/me/chats/getAllMessages"
    )


def test_chat_watch_resource_uses_users_for_app_only():
    assert (
        _chat_watch_resource("user@example.com", has_user_token=False)
        == "/users/user@example.com/chats/getAllMessages"
    )
