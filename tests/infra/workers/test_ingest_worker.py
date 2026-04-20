"""Unit tests for ``unity_deploy.infra.workers.ingest_worker``.

These focus narrowly on the per-message ``UNIFY_KEY`` lifecycle managed
by :func:`_with_unify_key`:

* each message resolves its own key and installs it for the duration of
  the context only;
* the prior environment is restored after normal exit and after errors
  raised inside the message body;
* if key resolution fails before installation, the existing environment
  is left untouched.
"""

from __future__ import annotations

import os

import pytest

from unity.common.pipeline.types import DmBinding, FmBinding
from unity_deploy.infra.workers import ingest_worker


@pytest.mark.asyncio
async def test_with_unify_key_swaps_env_per_message_and_cleans_up(
    monkeypatch,
) -> None:
    """Each message gets its own resolved key with no cross-message bleed."""
    monkeypatch.delenv("UNIFY_KEY", raising=False)

    bindings = [
        FmBinding(user_id="alice", assistant_id="42", logical_path="fm.csv"),
        DmBinding(user_id="alice", assistant_id="77", target_context="Orders"),
    ]
    resolved_keys = iter(["fm-key", "dm-key"])
    seen_before_install: list[tuple[str, str, str | None]] = []

    async def fake_resolve_api_key(binding):
        seen_before_install.append(
            (
                binding.user_id,
                binding.assistant_id or "",
                os.environ.get("UNIFY_KEY"),
            ),
        )
        return next(resolved_keys)

    monkeypatch.setattr(ingest_worker, "resolve_api_key", fake_resolve_api_key)

    async with ingest_worker._with_unify_key(bindings[0]) as key:
        assert key == "fm-key"
        assert os.environ["UNIFY_KEY"] == "fm-key"
    assert "UNIFY_KEY" not in os.environ

    async with ingest_worker._with_unify_key(bindings[1]) as key:
        assert key == "dm-key"
        assert os.environ["UNIFY_KEY"] == "dm-key"
    assert "UNIFY_KEY" not in os.environ

    assert seen_before_install == [
        ("alice", "42", None),
        ("alice", "77", None),
    ]


@pytest.mark.asyncio
async def test_with_unify_key_restores_previous_value_after_body_error(
    monkeypatch,
) -> None:
    """A message-specific key must not leak when the ingest body raises."""
    monkeypatch.setenv("UNIFY_KEY", "previous-key")

    async def fake_resolve_api_key(binding):
        return "message-key"

    monkeypatch.setattr(ingest_worker, "resolve_api_key", fake_resolve_api_key)
    binding = DmBinding(
        user_id="alice",
        assistant_id="42",
        target_context="Orders",
    )

    with pytest.raises(RuntimeError, match="boom"):
        async with ingest_worker._with_unify_key(binding) as key:
            assert key == "message-key"
            assert os.environ["UNIFY_KEY"] == "message-key"
            raise RuntimeError("boom")

    assert os.environ["UNIFY_KEY"] == "previous-key"


@pytest.mark.asyncio
async def test_with_unify_key_does_not_mutate_env_when_resolution_fails(
    monkeypatch,
) -> None:
    """Resolver failures happen before install, so the old env survives."""
    monkeypatch.setenv("UNIFY_KEY", "previous-key")

    async def fake_resolve_api_key(binding):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(ingest_worker, "resolve_api_key", fake_resolve_api_key)
    binding = FmBinding(user_id="alice", assistant_id="42", logical_path="x.csv")

    with pytest.raises(RuntimeError, match="lookup failed"):
        async with ingest_worker._with_unify_key(binding):
            pytest.fail("context body should never run when resolution fails")

    assert os.environ["UNIFY_KEY"] == "previous-key"
