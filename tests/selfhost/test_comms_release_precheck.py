"""Releasing a claim must distinguish "never wrote it" from "someone took it".

An acquisition that fails between claim and commit records a pending write that
never landed. Treating that like a lost resource made --release-voice (and so
`stack.sh down --full`) fail for the rest of the installation's life.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "deploy" / "selfhost" / "sync_comms_webhooks.py"

OURS = "AP-ours"
THEIRS = "AP-theirs"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sync_comms_webhooks", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resource(*, prior: str, applied: str, pending: str | None) -> dict:
    resource = {
        "metadata": {"kind": "whatsapp_voice"},
        "prior": {"voice_application_sid": prior},
        "applied": {"voice_application_sid": applied},
    }
    if pending is not None:
        resource["pending"] = {"voice_application_sid": pending}
    return resource


def _precheck(module: ModuleType, current: str, resource: dict) -> bool | None:
    return module._release_precheck(
        "whatsapp_voice",
        {"voice_application_sid": current},
        resource,
        "voice app",
    )


def test_untouched_provider_settles_as_released() -> None:
    module = _load_module()
    resource = _resource(prior="", applied="", pending=OURS)
    assert _precheck(module, "", resource) is True


def test_provider_holding_our_write_is_restored() -> None:
    """None means: go ahead and put the prior state back."""
    module = _load_module()
    resource = _resource(prior="", applied=OURS, pending=None)
    assert _precheck(module, OURS, resource) is None


def test_provider_holding_our_uncommitted_write_is_restored() -> None:
    """The mutation landed but the commit did not; it is still ours to undo."""
    module = _load_module()
    resource = _resource(prior="", applied="", pending=OURS)
    assert _precheck(module, OURS, resource) is None


def test_claim_that_never_landed_is_dropped() -> None:
    """applied == prior: nothing was ever written, so there is nothing to undo."""
    module = _load_module()
    resource = _resource(prior="", applied="", pending=OURS)
    assert _precheck(module, THEIRS, resource) is True


def test_losing_a_write_we_did_land_still_refuses() -> None:
    """We changed the provider and someone replaced it; restoring would clobber."""
    module = _load_module()
    resource = _resource(prior="", applied=OURS, pending=None)
    assert _precheck(module, THEIRS, resource) is False
