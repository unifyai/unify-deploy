from __future__ import annotations

from types import SimpleNamespace
import threading

from unity_deploy.runtime_reconcile import runner
from unity_deploy.runtime_reconcile.context import (
    RuntimeIdentity,
    runtime_identity_from_session,
)
from unity_deploy.runtime_reconcile.status import (
    RuntimeReconcileStatusHandle,
    runtime_reconcile_prompt_note,
)


def test_runtime_identity_uses_session_unify_key():
    session = SimpleNamespace(
        assistant=SimpleNamespace(agent_id=123),
        user=SimpleNamespace(id="user-1"),
        org_id=7,
        team_ids=[9],
        unify_key="assistant-key",
    )

    identity = runtime_identity_from_session(session)

    assert identity == RuntimeIdentity(
        assistant_id="123",
        user_id="user-1",
        org_id=7,
        team_ids=(9,),
        api_key="assistant-key",
    )


def test_runtime_status_prompt_note_hides_internal_reconcile_language():
    status = RuntimeReconcileStatusHandle()
    status.update(phase="syncing_seed_data")

    note = runtime_reconcile_prompt_note(status)

    assert note is not None
    assert "setup is still finishing" in note
    assert "runtime reconciliation" not in note.lower()


def test_start_runtime_reconcile_returns_before_materialization_finishes(
    monkeypatch,
):
    started = threading.Event()
    release = threading.Event()
    notifications: list[str] = []

    def fake_activate_runtime_context(identity):
        assert identity.api_key == "assistant-key"

    def fake_materialize_runtime_state(
        resolved,
        identity,
        *,
        revision=None,
        status=None,
    ):
        started.set()
        assert release.wait(timeout=2)
        status.update(phase="complete")

    cm = SimpleNamespace(
        loop=None,
        notifications_bar=SimpleNamespace(
            push_notif=lambda _type, message, _timestamp: notifications.append(message),
        ),
    )
    identity = RuntimeIdentity(
        assistant_id="123",
        user_id="user-1",
        api_key="assistant-key",
    )

    monkeypatch.setattr(
        runner,
        "activate_runtime_context",
        fake_activate_runtime_context,
    )
    monkeypatch.setattr(
        runner,
        "materialize_runtime_state",
        fake_materialize_runtime_state,
    )

    handle = runner.start_runtime_reconcile(
        cm,
        SimpleNamespace(),
        identity,
        mode="async",
    )

    assert handle.thread is not None
    assert started.wait(timeout=2)
    assert handle.thread.is_alive()
    assert cm.deployment_runtime_reconcile_status is handle.status

    release.set()
    handle.thread.join(timeout=2)

    assert not handle.thread.is_alive()
    assert handle.status.snapshot().current_phase == "complete"
    assert notifications == [
        "Assistant setup complete; deployment-defined data and custom tools are ready.",
    ]
