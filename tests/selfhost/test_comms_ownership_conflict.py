"""Ownership contention on the shared voice number is not a broken call edge.

A conflict means provider state is healthy and owned by another live install,
so it exits distinctly from a genuine failure and lets `stack.sh up` continue.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "deploy" / "selfhost" / "sync_comms_webhooks.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sync_comms_webhooks", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _isolate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    phone: bool = False,
) -> None:
    """Keep owner id, lock, and state off the real ~/.unity, and Twilio unread."""
    monkeypatch.setenv("SELF_HOST_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SELF_HOST_COMMS_TWILIO_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv(
        "UNIFY_CONVERSATION_LOCAL_COMMS_PUBLIC_URL",
        "https://tunnel.invalid",
    )
    monkeypatch.setenv("COMMS_BRIDGE_WHATSAPP_NUMBER", "+15550000000")
    monkeypatch.delenv("UNITY_COORDINATOR_PHONE", raising=False)
    monkeypatch.delenv("UNIFY_COORDINATOR_PHONE_US", raising=False)
    if phone:
        monkeypatch.setenv("COMMS_BRIDGE_SMS_NUMBER", "+15551111111")
    else:
        monkeypatch.delenv("COMMS_BRIDGE_SMS_NUMBER", raising=False)
    monkeypatch.setattr(sys, "argv", ["sync_comms_webhooks.py", "--set-voice"])


def test_ownership_conflict_is_a_runtime_error() -> None:
    """Existing `except RuntimeError` handlers must keep catching conflicts."""
    module = _load_module()
    assert issubclass(module.OwnershipConflict, RuntimeError)


def test_conflict_exits_distinctly_from_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    _isolate(monkeypatch, tmp_path)

    def conflict(*_args: object, **_kwargs: object) -> bool:
        raise module.OwnershipConflict("WhatsApp voice is active for someone-else")

    monkeypatch.setattr(module, "reconcile_whatsapp", conflict)
    monkeypatch.setattr(module, "reconcile_whatsapp_voice", conflict)

    assert module.main() == module._EXIT_OWNERSHIP_CONFLICT


def test_real_failure_still_exits_two(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A broken call edge must stay fatal — only contention is downgraded."""
    module = _load_module()
    _isolate(monkeypatch, tmp_path)

    def broken(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("Twilio credentials rejected")

    monkeypatch.setattr(module, "reconcile_whatsapp", broken)
    monkeypatch.setattr(module, "reconcile_whatsapp_voice", broken)

    assert module.main() == 2


def test_contended_whatsapp_still_acquires_the_phone_number(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Channels are separate resources; one being taken must not skip the other."""
    module = _load_module()
    _isolate(monkeypatch, tmp_path, phone=True)
    phone_calls: list[str] = []

    def conflict(*_args: object, **_kwargs: object) -> bool:
        raise module.OwnershipConflict("WhatsApp voice is active for someone-else")

    def phone(number: str, *_args: object, **_kwargs: object) -> bool:
        phone_calls.append(number)
        return True

    monkeypatch.setattr(module, "reconcile_whatsapp", conflict)
    monkeypatch.setattr(module, "reconcile_whatsapp_voice", conflict)
    monkeypatch.setattr(module, "reconcile_phone", phone)

    assert module.main() == module._EXIT_OWNERSHIP_CONFLICT
    assert phone_calls == ["+15551111111"]


def test_a_broken_channel_outranks_a_contended_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Contention is survivable; a broken call edge must still stop startup."""
    module = _load_module()
    _isolate(monkeypatch, tmp_path, phone=True)

    def conflict(*_args: object, **_kwargs: object) -> bool:
        raise module.OwnershipConflict("WhatsApp voice is active for someone-else")

    def broken(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("Twilio credentials rejected")

    monkeypatch.setattr(module, "reconcile_whatsapp", conflict)
    monkeypatch.setattr(module, "reconcile_whatsapp_voice", conflict)
    monkeypatch.setattr(module, "reconcile_phone", broken)

    assert module.main() == 2
