"""Tests for brain_operator environment targeting."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def _clear_brain_operator_env(monkeypatch):
    monkeypatch.delenv("BRAIN_OPERATOR_ASSISTANT_ID", raising=False)
    monkeypatch.delenv("ORCHESTRA_URL", raising=False)


def _reload():
    import unity_deploy.assistant_deployments.clients.unify_company as mod

    return importlib.reload(mod)


def test_assistant_id_env_override(monkeypatch):
    monkeypatch.setenv("ORCHESTRA_URL", "https://api.unify.ai/v0")
    monkeypatch.setenv("BRAIN_OPERATOR_ASSISTANT_ID", "9999")
    mod = _reload()
    assert mod._operator_assistant_id() == "9999"


def test_assistant_id_defaults_to_production_1406(monkeypatch):
    monkeypatch.setenv("ORCHESTRA_URL", "https://api.unify.ai/v0")
    mod = _reload()
    assert mod._operator_assistant_id() == "1406"


def test_assistant_id_defaults_to_staging_7367(monkeypatch):
    monkeypatch.setenv("ORCHESTRA_URL", "https://internal.example.com/v0")
    mod = _reload()
    assert mod._operator_assistant_id() == "7367"


def test_assistant_id_unset_on_unknown_environment(monkeypatch):
    monkeypatch.setenv("ORCHESTRA_URL", "http://127.0.0.1:8000/v0")
    mod = _reload()
    assert mod._operator_assistant_id() is None
