from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT_PATH = (
    Path(__file__).parents[2] / "scripts" / "reconcile_whatsapp_call_permissions.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "reconcile_whatsapp_call_permissions",
    _SCRIPT_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
reconcile_script = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reconcile_script)


def _args(**overrides) -> argparse.Namespace:
    values = {
        "pool_number": ["+447700900001"],
        "contact_number": "+4915550100009",
        "lookback_days": 7,
        "limit": 200,
        "orchestra_url": "http://orchestra.test/v0",
        "orchestra_admin_key": "admin-key",
        "permission_cache": "",
        "mark_accepted_local": True,
        "yes_i_verified_whatsapp_approved": True,
        "dry_run": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_manual_repair_requires_confirmation() -> None:
    with pytest.raises(RuntimeError, match="yes-i-verified"):
        reconcile_script.reconcile(_args(yes_i_verified_whatsapp_approved=False))


def test_manual_repair_marks_accepted_and_writes_cache(monkeypatch, tmp_path) -> None:
    posts = []
    cache_path = tmp_path / "wa-permissions.json"

    def fake_post(url, **kwargs):
        posts.append((url, kwargs))
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "status": "accepted",
                "expires_at": "2999-01-01T00:00:00+00:00",
            },
        )

    monkeypatch.setattr(reconcile_script.requests, "post", fake_post)

    result = reconcile_script.reconcile(_args(permission_cache=str(cache_path)))

    assert result == 1
    assert posts == [
        (
            "http://orchestra.test/v0/admin/whatsapp/call-permission",
            {
                "headers": {"Authorization": "Bearer admin-key"},
                "json": {
                    "pool_number": "+447700900001",
                    "contact_number": "+4915550100009",
                    "status": "accepted",
                    "source": "manual_local_repair",
                },
                "timeout": 10,
            },
        ),
    ]
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["+447700900001|+4915550100009"]["status"] == "accepted"
    assert (
        cache["+447700900001|+4915550100009"]["expires_at"]
        == "2999-01-01T00:00:00+00:00"
    )
