"""Shared fixtures for assistant deployment tests."""

from __future__ import annotations

import os

# Importing ``tests.helpers`` builds a unisdk logger, which demands a key
# before any test runs. Nothing under this tree needs a real one: the tests
# that reach a backend are either served from memory by ``fake_orchestra`` or
# gated on ``requires_orchestra``, which skips unless a live Orchestra accepts
# the key it was given. A placeholder therefore keeps the tree runnable with
# no credential at all, while a real key in the environment still selects the
# live path.
os.environ.setdefault("UNIFY_KEY", "unify-deploy-hermetic-tests")

import pytest

from tests import fake_orchestra


@pytest.fixture(autouse=True)
def _embedded_client_mode(monkeypatch):
    monkeypatch.setenv("UNIFY_DEPLOY_CLIENT_MODE", "embedded")


@pytest.fixture
def fake_orchestra_store(monkeypatch):
    """Back unisdk's logs API with an in-memory store for one test."""
    store = fake_orchestra.install(monkeypatch)
    yield store
    store.assert_fully_served()
