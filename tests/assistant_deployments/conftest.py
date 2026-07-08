"""Shared fixtures for assistant deployment tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _embedded_client_mode(monkeypatch):
    monkeypatch.setenv("UNITY_DEPLOY_CLIENT_MODE", "embedded")
