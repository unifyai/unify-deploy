from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass, field
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient
from kubernetes.client.rest import ApiException

fake_kopf = types.SimpleNamespace()


def _identity_decorator(*_args, **_kwargs):
    def decorator(func):
        return func

    return decorator


fake_kopf.on = types.SimpleNamespace(
    startup=_identity_decorator,
    create=_identity_decorator,
    update=_identity_decorator,
    delete=_identity_decorator,
    probe=_identity_decorator,
)
fake_kopf.timer = _identity_decorator
fake_kopf.OperatorSettings = type("OperatorSettings", (), {})


class _TemporaryError(Exception):
    def __init__(
        self,
        *args,
        delay=None,
    ):
        super().__init__(*args)
        self.delay = delay


fake_kopf.TemporaryError = _TemporaryError
sys.modules.setdefault("kopf", fake_kopf)

from communication.assistant_session_controller import controller, workers
from communication.dependencies import authenticate_vm_identity
from communication.infra.assistant_sessions import assistant_session_name
from communication.infra.views import tunnel_router, vm_self_router
from communication.infra.vm_helpers import AssistantDiskInUseError


def _job(
    *,
    session_name: str,
    binding_id: str,
    job_name: str,
    assistant_id: str,
    container_ready: bool = True,
):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=job_name,
            labels={
                "app": "unity",
                "assistant-id": assistant_id,
                controller.SESSION_REF_LABEL: session_name,
                controller.BINDING_ID_LABEL: binding_id,
            },
            annotations={
                controller.SESSION_REF_ANNOTATION: session_name,
                controller.BINDING_ID_ANNOTATION: binding_id,
                controller.CONTAINER_READY_ANNOTATION: (
                    "true" if container_ready else "false"
                ),
            },
            deletion_timestamp=None,
        ),
        status=SimpleNamespace(
            active=1,
            conditions=[],
        ),
    )


def _pod(
    *,
    pod_name: str,
    job_name: str,
    phase: str = "Running",
):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=pod_name,
            labels={"job-name": job_name},
        ),
        status=SimpleNamespace(phase=phase),
    )


def _secret(payload: dict):
    raw = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
    return SimpleNamespace(data={"startup.json": raw}, string_data=None)


class _ImmediateRuntime:
    """Execute queued worker tasks immediately for deterministic component tests."""

    def submit(
        self,
        *,
        task_type: str,
        task_assistant_id: str,
        task_binding_id: str,
        fn,
        **kwargs,
    ) -> bool:
        fn(**kwargs)
        return True

    def stats(self) -> dict[str, int]:
        return {"inflight": 0}


class _FakeCustomApi:
    def __init__(
        self,
        harness: "AssistantSessionComponentHarness",
    ) -> None:
        self._harness = harness

    def get_namespaced_custom_object(self, **kwargs):
        if kwargs["name"] != self._harness.session_name:
            raise ApiException(status=404)
        return deepcopy(self._harness.session)

    def replace_namespaced_custom_object_status(self, **kwargs):
        if kwargs["name"] != self._harness.session_name:
            raise ApiException(status=404)
        next_session = deepcopy(kwargs["body"])
        next_session.setdefault("metadata", {})
        next_session["metadata"]["resourceVersion"] = str(
            int(self._harness.session["metadata"]["resourceVersion"]) + 1,
        )
        self._harness.session.clear()
        self._harness.session.update(next_session)
        return deepcopy(self._harness.session)

    def list_namespaced_custom_object(self, **_kwargs):
        return {"items": [deepcopy(self._harness.session)]}


class _FakeBatchApi:
    def __init__(
        self,
        harness: "AssistantSessionComponentHarness",
    ) -> None:
        self._harness = harness

    def read_namespaced_job(
        self,
        name: str,
        namespace: str,
    ):
        if namespace != self._harness.namespace or name not in self._harness.jobs:
            raise ApiException(status=404)
        return deepcopy(self._harness.jobs[name])

    def list_namespaced_job(
        self,
        namespace: str,
        label_selector: str = "",
    ):
        if namespace != self._harness.namespace:
            return SimpleNamespace(items=[])
        selectors = {}
        if label_selector:
            for token in label_selector.split(","):
                if "=" not in token:
                    continue
                key, value = token.split("=", 1)
                selectors[key] = value
        items = []
        for job in self._harness.jobs.values():
            labels = job.metadata.labels or {}
            if all(labels.get(key) == value for key, value in selectors.items()):
                items.append(deepcopy(job))
        return SimpleNamespace(items=items)

    def patch_namespaced_job(
        self,
        name: str,
        namespace: str,
        body,
    ):
        if namespace != self._harness.namespace or name not in self._harness.jobs:
            raise ApiException(status=404)
        job = self._harness.jobs[name]
        metadata_patch = getattr(body, "metadata", None) or {}
        spec_patch = getattr(body, "spec", None) or {}
        if isinstance(metadata_patch, dict):
            labels = metadata_patch.get("labels") or {}
            annotations = metadata_patch.get("annotations") or {}
        else:
            labels = getattr(metadata_patch, "labels", None) or {}
            annotations = getattr(metadata_patch, "annotations", None) or {}
        if labels:
            job.metadata.labels.update(labels)
        if annotations:
            job.metadata.annotations.update(annotations)
        suspend = None
        if isinstance(spec_patch, dict):
            suspend = spec_patch.get("suspend")
        else:
            suspend = getattr(spec_patch, "suspend", None)
        if suspend is True:
            job.status.active = 0
        return deepcopy(job)


class _FakeCoreApi:
    def __init__(
        self,
        harness: "AssistantSessionComponentHarness",
    ) -> None:
        self._harness = harness

    def list_namespaced_pod(
        self,
        namespace: str,
        label_selector: str = "",
    ):
        if namespace != self._harness.namespace:
            return SimpleNamespace(items=[])
        selectors = {}
        if label_selector:
            for token in label_selector.split(","):
                if "=" not in token:
                    continue
                key, value = token.split("=", 1)
                selectors[key] = value
        items = []
        for pod in self._harness.pods.values():
            labels = pod.metadata.labels or {}
            if all(labels.get(key) == value for key, value in selectors.items()):
                items.append(deepcopy(pod))
        return SimpleNamespace(items=items)

    def read_namespaced_pod(
        self,
        name: str,
        namespace: str,
    ):
        if namespace != self._harness.namespace or name not in self._harness.pods:
            raise ApiException(status=404)
        return deepcopy(self._harness.pods[name])

    def read_namespaced_secret(
        self,
        name: str,
        namespace: str,
    ):
        if namespace != self._harness.namespace or name not in self._harness.secrets:
            raise ApiException(status=404)
        return self._harness.secrets[name]


@dataclass
class AssistantSessionComponentHarness:
    """Stateful in-process harness for AssistantSession component flows."""

    namespace: str = controller.WATCH_NAMESPACE
    assistant_id: str = "1207"
    binding_id: str = "binding-1"
    activation_id: str = "act-1"
    job_name: str = "unity-job-1"
    pod_name: str = "unity-job-1-pod"
    vm_name: str = "unity-pool-ubuntu-1-staging"
    vm_hostname: str = "vm-1.vm.unify.ai"
    user_api_key: str = "user-key"
    assignment_mode: str = "assigned"
    probe_results: list[bool] = field(default_factory=lambda: [True])
    force_assignment_loss: bool = False
    session: dict = field(init=False)
    session_name: str = field(init=False)
    secret_name: str = field(init=False)
    jobs: dict = field(init=False)
    pods: dict = field(init=False)
    secrets: dict = field(init=False)
    runtime_vm_present: bool = field(init=False, default=False)
    runtime_binding_id: str | None = field(init=False, default=None)
    runtime_pool_role: str = field(init=False, default="idle")
    vm_labels: dict = field(init=False)
    custom_api: _FakeCustomApi = field(init=False)
    batch_api: _FakeBatchApi = field(init=False)
    core_api: _FakeCoreApi = field(init=False)
    client: TestClient = field(init=False)
    published_ready_events: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.session_name = assistant_session_name(self.assistant_id)
        self.secret_name = f"assistant-session-bootstrap-{self.assistant_id}"
        self.session = {
            "metadata": {
                "name": self.session_name,
                "resourceVersion": "1",
            },
            "spec": {
                "assistantId": self.assistant_id,
                "activationId": self.activation_id,
                "desiredState": "Running",
                "desktop": {"required": True, "mode": "ubuntu"},
                "startupSecretRef": self.secret_name,
            },
            "status": {
                "phase": "PendingVM",
                "observedActivationId": self.activation_id,
                "binding": {
                    "id": self.binding_id,
                    "jobRef": {
                        "name": self.job_name,
                        "namespace": self.namespace,
                    },
                    "containerReadyAt": "2026-04-06T00:00:00+00:00",
                },
                "conditions": [
                    {
                        "type": "ContainerReady",
                        "status": "True",
                        "reason": "Ready",
                        "message": "Unity session ready",
                    },
                ],
                "bootstrapRetries": 0,
                "vmRetries": 0,
                "desktopProbeFailures": 0,
            },
        }
        self.jobs = {
            self.job_name: _job(
                session_name=self.session_name,
                binding_id=self.binding_id,
                job_name=self.job_name,
                assistant_id=self.assistant_id,
            ),
        }
        self.pods = {
            self.pod_name: _pod(pod_name=self.pod_name, job_name=self.job_name),
        }
        self.secrets = {self.secret_name: _secret({"api_key": self.user_api_key})}
        self.vm_labels = {"pool-role": self.runtime_pool_role}
        self.custom_api = _FakeCustomApi(self)
        self.batch_api = _FakeBatchApi(self)
        self.core_api = _FakeCoreApi(self)

        app = FastAPI()
        app.include_router(tunnel_router, prefix="/infra")
        app.include_router(vm_self_router, prefix="/infra")
        app.dependency_overrides[authenticate_vm_identity] = lambda: {
            "google": {
                "compute_engine": {
                    "instance_name": self.vm_name,
                },
            },
        }
        self.client = TestClient(app)

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(controller, "_custom_api", self.custom_api)
        monkeypatch.setattr(controller, "_core_api", self.core_api)
        monkeypatch.setattr(controller, "_batch_api", self.batch_api)
        monkeypatch.setattr(controller, "_coord_api", object())
        monkeypatch.setattr(
            workers,
            "get_worker_runtime",
            lambda: _ImmediateRuntime(),
        )
        monkeypatch.setattr(workers, "replenish_pool", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            workers,
            "assign_pool_vm",
            self.assign_pool_vm,
        )
        monkeypatch.setattr(
            workers,
            "probe_vm_agent_service",
            self.probe_vm_agent_service,
        )
        monkeypatch.setattr(
            workers,
            "release_pool_vm",
            self.release_pool_vm,
        )
        monkeypatch.setattr(
            controller,
            "verify_vm_assignment",
            self.verify_vm_assignment,
        )
        monkeypatch.setattr(
            controller,
            "acquire_assignment_lease",
            lambda *_args, **_kwargs: True,
        )
        monkeypatch.setattr(
            controller,
            "release_assignment_lease",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            controller,
            "split_binding_runtime_vms",
            self.split_binding_runtime_vms,
        )
        monkeypatch.setattr(
            controller,
            "find_vm_with_disk",
            self.find_vm_with_disk,
        )
        monkeypatch.setattr(
            controller,
            "complete_pool_vm_release",
            self.complete_pool_vm_release,
        )
        monkeypatch.setattr(
            "communication.infra.views.extract_api_key",
            lambda _request: self.user_api_key,
        )
        monkeypatch.setattr(
            "communication.infra.views.authenticate_user_api_key",
            AsyncMock(),
        )
        monkeypatch.setattr(
            "communication.infra.views.get_custom_objects_api",
            lambda: self.custom_api,
        )
        monkeypatch.setattr(
            "communication.infra.views._get_k8s_clients",
            AsyncMock(return_value=(None, self.core_api, None, None)),
        )
        monkeypatch.setattr(
            "communication.infra.views.verify_vm_assignment",
            self.verify_vm_assignment,
        )
        monkeypatch.setattr(
            "communication.infra.views.probe_vm_agent_service_authenticated",
            lambda *_args, **_kwargs: True,
        )
        monkeypatch.setattr(
            "communication.infra.views._publish_desktop_ready",
            AsyncMock(side_effect=self.publish_desktop_ready),
        )
        monkeypatch.setattr(
            "communication.infra.views.compute_v1.InstancesClient",
            lambda: SimpleNamespace(get=self.get_vm_instance),
        )
        monkeypatch.setattr(
            "communication.infra.views.complete_pool_vm_release",
            self.complete_pool_vm_release,
        )

    def assign_pool_vm(
        self,
        *,
        assistant_id: str,
        binding_id: str,
        unify_apikey: str,
        vm_type: str,
    ) -> dict:
        assert assistant_id == self.assistant_id
        assert unify_apikey == self.user_api_key
        if self.assignment_mode == "capacity":
            raise ValueError("Waiting for VM capacity")
        if self.assignment_mode == "waiting_release":
            raise AssistantDiskInUseError("assistant disk still attached")
        if self.assignment_mode == "error":
            raise RuntimeError("assignment boom")

        self.runtime_vm_present = True
        self.runtime_binding_id = binding_id
        self.runtime_pool_role = "assigned"
        self.vm_labels = {
            "assistant-id": assistant_id,
            "binding-id": binding_id,
            "pool-role": self.runtime_pool_role,
        }
        return {
            "vm_name": self.vm_name,
            "hostname": self.vm_hostname,
            "desktop_url": f"https://{self.vm_hostname}",
            "vm_type": vm_type,
        }

    def probe_vm_agent_service(self, _hostname: str, timeout: float = 3.0) -> bool:
        assert timeout == 3.0
        if self.probe_results:
            return self.probe_results.pop(0)
        return True

    def release_pool_vm(
        self,
        assistant_id: str,
        binding_id: str,
        *,
        vm_name: str,
        release_generation: int | None = None,
    ) -> dict:
        assert assistant_id == self.assistant_id
        if (
            not self.runtime_vm_present
            or binding_id != self.runtime_binding_id
            or vm_name != self.vm_name
        ):
            return {"released": False, "pool_role": self.runtime_pool_role}
        self.runtime_pool_role = "releasing"
        self.vm_labels = {
            "assistant-id": assistant_id,
            "binding-id": binding_id,
            "pool-role": self.runtime_pool_role,
        }
        return {
            "released": True,
            "pool_role": self.runtime_pool_role,
            "release_generation": release_generation,
        }

    def verify_vm_assignment(
        self,
        vm_name: str,
        binding_id: str,
        assistant_id: str,
    ) -> dict | None:
        if self.force_assignment_loss:
            return None
        binding = (self.session.get("status") or {}).get("binding") or {}
        vm_ref = binding.get("vmRef") or {}
        if (
            assistant_id == self.assistant_id
            and binding_id == binding.get("id")
            and vm_name == vm_ref.get("name")
        ):
            return deepcopy(vm_ref)
        return None

    async def publish_desktop_ready(
        self,
        assistant_id: str,
        hostname: str,
        vm_type: str,
        *,
        binding_id: str,
    ) -> str:
        self.published_ready_events.append(
            {
                "assistant_id": assistant_id,
                "hostname": hostname,
                "vm_type": vm_type,
                "binding_id": binding_id,
            },
        )
        return f"message-{len(self.published_ready_events)}"

    def split_binding_runtime_vms(
        self,
        assistant_id: str,
        *,
        binding_id: str | None = None,
    ) -> tuple[list[dict], list[dict]]:
        if assistant_id != self.assistant_id or not self.runtime_vm_present:
            return [], []
        vm = {
            "assistant_id": assistant_id,
            "binding_id": self.runtime_binding_id,
            "pool_role": self.runtime_pool_role,
            "vm_name": self.vm_name,
        }
        if binding_id and binding_id == self.runtime_binding_id:
            return [vm], []
        return [], [vm]

    def find_vm_with_disk(self, assistant_id: str) -> str | None:
        if assistant_id == self.assistant_id and self.runtime_vm_present:
            return self.vm_name
        return None

    def complete_pool_vm_release(self, vm_name: str, binding_id: str) -> dict:
        if (
            not self.runtime_vm_present
            or vm_name != self.vm_name
            or binding_id != self.runtime_binding_id
        ):
            return {"skipped": True, "reason": "binding_changed"}
        self.runtime_vm_present = False
        self.runtime_binding_id = None
        self.runtime_pool_role = "idle"
        self.vm_labels = {"pool-role": self.runtime_pool_role}
        return {
            "vm_name": vm_name,
            "binding_id": binding_id,
            "pool_role": self.runtime_pool_role,
        }

    def get_vm_instance(self, **_kwargs):
        return SimpleNamespace(labels=deepcopy(self.vm_labels))

    def reconcile(self) -> dict:
        controller._update_status_for_session(deepcopy(self.session))
        return deepcopy(self.session)

    def post_vm_ready(
        self,
        *,
        binding_id: str | None = None,
        hostname: str | None = None,
    ):
        return self.client.post(
            "/infra/vm/ready",
            json={
                "assistant_id": self.assistant_id,
                "binding_id": binding_id or self.binding_id,
                "hostname": hostname or self.vm_hostname,
                "vm_type": "ubuntu",
            },
            headers={"Authorization": f"Bearer {self.user_api_key}"},
        )

    def post_vm_release_complete(
        self,
        *,
        binding_id: str | None = None,
        release_generation: int | None = None,
    ):
        binding = (self.session.get("status") or {}).get("binding") or {}
        return self.client.post(
            "/infra/vm/release-complete",
            json={
                "binding_id": binding_id or self.binding_id,
                "release_generation": (
                    release_generation
                    if release_generation is not None
                    else binding.get("releaseGeneration")
                ),
            },
        )
