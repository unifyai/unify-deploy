"""The pod-side bootstrap Secret reader and the comms-side writer agree.

These two live in different packages — the writer in ``communication.infra``,
the reader installed into the assistant image as ``unify_deploy.runtime`` — and
they are only correct relative to each other. When the writer moved Secrets into
``{env}-sessions`` the reader kept reading the pod's own namespace, so every
bootstrap 404'd, every session hit BootstrapTimeout, and no assistant in the
environment could start. Nothing failed at build time; the two namespaces are
only compared at runtime, which is what these tests do here instead.
"""

import base64
import json

import pytest
from kubernetes.client.rest import ApiException

from communication.infra.assistant_sessions import bootstrap_namespace
from unify_deploy.runtime import session_assignment


def _secret(payload: dict, *, name: str = "assistant-session-bootstrap-2103-act-1"):
    class FakeSecret:
        def __init__(self):
            self.metadata = type(
                "Metadata",
                (),
                {"name": name, "annotations": {}, "resource_version": "1"},
            )()
            self.data = {
                "startup.json": base64.b64encode(
                    json.dumps(payload).encode("utf-8"),
                ).decode("utf-8"),
            }

    return FakeSecret()


@pytest.fixture
def pod_namespace(monkeypatch):
    monkeypatch.setattr(session_assignment, "_load_clients", lambda: None)
    monkeypatch.setattr(session_assignment, "_namespace", lambda: "staging")
    return "staging"


def test_reader_resolves_the_namespace_the_writer_writes(pod_namespace):
    """The reader's first choice is exactly the writer's target."""

    assert session_assignment._bootstrap_namespace() == bootstrap_namespace(
        pod_namespace,
    )


def test_reader_prefers_the_sessions_namespace(monkeypatch, pod_namespace):
    asked = []

    class FakeCoreApi:
        def read_namespaced_secret(self, name, namespace):
            asked.append(namespace)
            return _secret({"api_key": "k"})

    monkeypatch.setattr(session_assignment, "_core_api", FakeCoreApi())
    record = session_assignment.read_session_bootstrap_secret_record(
        "assistant-session-bootstrap-2103-act-1",
    )

    assert record.payload == {"api_key": "k"}
    assert asked == ["staging-sessions"]


def test_reader_falls_back_to_the_session_namespace(monkeypatch, pod_namespace):
    """A Secret written before the move is still readable."""

    asked = []

    class FakeCoreApi:
        def read_namespaced_secret(self, name, namespace):
            asked.append(namespace)
            if namespace == "staging-sessions":
                raise ApiException(status=404)
            return _secret({"api_key": "legacy"})

    monkeypatch.setattr(session_assignment, "_core_api", FakeCoreApi())
    record = session_assignment.read_session_bootstrap_secret_record(
        "assistant-session-bootstrap-2103-act-1",
    )

    assert record.payload == {"api_key": "legacy"}
    assert asked == ["staging-sessions", "staging"]


def test_missing_bootstrap_names_every_namespace_tried(monkeypatch, pod_namespace):
    """The failure has to say where it looked.

    A bare 404 carrying only the Secret name cannot distinguish "the writer has
    not written it yet" from "the writer wrote it somewhere this pod does not
    read" — the ambiguity that hid the namespace split for the length of an
    outage.
    """

    class FakeCoreApi:
        def read_namespaced_secret(self, name, namespace):
            raise ApiException(status=404)

    monkeypatch.setattr(session_assignment, "_core_api", FakeCoreApi())
    with pytest.raises(RuntimeError) as excinfo:
        session_assignment.read_session_bootstrap_secret_record(
            "assistant-session-bootstrap-2103-act-1",
        )

    message = str(excinfo.value)
    assert "staging-sessions" in message
    assert "staging" in message


def test_non_404_errors_are_not_swallowed_by_the_fallback(monkeypatch, pod_namespace):
    """A 403 is a broken RoleBinding, not a missing Secret."""

    class FakeCoreApi:
        def read_namespaced_secret(self, name, namespace):
            raise ApiException(status=403)

    monkeypatch.setattr(session_assignment, "_core_api", FakeCoreApi())
    with pytest.raises(ApiException) as excinfo:
        session_assignment.read_session_bootstrap_secret_record(
            "assistant-session-bootstrap-2103-act-1",
        )

    assert excinfo.value.status == 403
