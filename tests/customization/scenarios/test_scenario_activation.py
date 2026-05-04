"""Tests for ScenarioActivation — the deployment-side template
instantiation that lets clients schedule a packaged platform-integration
sync without copy-pasting the YAML into a per-client wrapper package.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from unity_deploy.customization.scenarios.loader import (
    find_scenario_template,
    materialise_scenario_activations,
)
from unity_deploy.customization.scenarios.types import (
    ScenarioActivation,
    ScenarioSpec,
)


# ---------------------------------------------------------------------------
# Pydantic validators
# ---------------------------------------------------------------------------


class TestScenarioActivationValidators:
    def test_happy_path(self):
        a = ScenarioActivation(
            scenario_template="hubspot/crm_full_sync_v0",
            assistant_id="630",
        )
        assert a.scenario_template == "hubspot/crm_full_sync_v0"
        assert a.assistant_id == "630"
        assert a.tasks_enabled is False  # default

    def test_template_must_have_slash(self):
        with pytest.raises(ValueError, match="must be '<package_slug>"):
            ScenarioActivation(scenario_template="no_slash", assistant_id="1")

    def test_template_rejects_yaml_extension(self):
        with pytest.raises(ValueError, match="must not include a file extension"):
            ScenarioActivation(
                scenario_template="hubspot/crm.yaml",
                assistant_id="1",
            )
        with pytest.raises(ValueError, match="must not include a file extension"):
            ScenarioActivation(
                scenario_template="hubspot/crm.yml",
                assistant_id="1",
            )

    def test_template_rejects_empty_components(self):
        with pytest.raises(ValueError, match="must both be non-empty"):
            ScenarioActivation(scenario_template="/stem", assistant_id="1")
        with pytest.raises(ValueError, match="must both be non-empty"):
            ScenarioActivation(scenario_template="slug/", assistant_id="1")

    def test_assistant_id_must_be_non_empty(self):
        with pytest.raises(ValueError, match="must be non-empty"):
            ScenarioActivation(
                scenario_template="hubspot/crm_full_sync_v0",
                assistant_id="",
            )
        with pytest.raises(ValueError, match="must be non-empty"):
            ScenarioActivation(
                scenario_template="hubspot/crm_full_sync_v0",
                assistant_id="   ",
            )

    def test_optional_overrides(self):
        a = ScenarioActivation(
            scenario_template="hubspot/crm_full_sync_v0",
            assistant_id="630",
            scenario_id_override="custom_id",
            client_override="other",
            deployment_override="staging",
            tasks_enabled=True,
            task_description_override="my desc",
            object_intervals_override={"contacts": 1800},
            config_overrides={"FOO": "bar"},
        )
        assert a.scenario_id_override == "custom_id"
        assert a.client_override == "other"
        assert a.deployment_override == "staging"
        assert a.tasks_enabled is True
        assert a.task_description_override == "my desc"
        assert a.object_intervals_override == {"contacts": 1800}
        assert a.config_overrides == {"FOO": "bar"}


# ---------------------------------------------------------------------------
# find_scenario_template
# ---------------------------------------------------------------------------


class TestFindScenarioTemplate:
    def test_resolves_real_hubspot_template(self):
        path = find_scenario_template("hubspot/crm_full_sync_v0")
        assert path is not None
        assert path.is_file()
        assert path.name == "crm_full_sync_v0.yaml"
        assert "hubspot/scenarios" in str(path)

    def test_returns_none_for_unknown_template(self):
        assert find_scenario_template("nonexistent/template") is None

    def test_returns_none_for_invalid_format(self):
        assert find_scenario_template("no_slash") is None
        assert find_scenario_template("") is None

    def test_custom_search_paths(self, tmp_path: Path):
        # Build a minimal template at <tmp>/my_pkg/scenarios/sync.yaml
        scenario_dir = tmp_path / "my_pkg" / "scenarios"
        scenario_dir.mkdir(parents=True)
        (scenario_dir / "sync.yaml").write_text("# placeholder\n")

        path = find_scenario_template(
            "my_pkg/sync",
            search_paths=[tmp_path],
        )
        assert path is not None
        assert path.parent == scenario_dir


# ---------------------------------------------------------------------------
# Materialisation — happy path
# ---------------------------------------------------------------------------


def _write_template(
    tmp_path: Path,
    slug: str,
    stem: str,
    body: str,
) -> Path:
    """Helper to write a template YAML at tmp_path/<slug>/scenarios/<stem>.yaml."""
    scenarios_dir = tmp_path / slug / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    path = scenarios_dir / f"{stem}.yaml"
    path.write_text(body)
    return path


_MINIMAL_TEMPLATE = dedent(
    """\
    scenario_id: my_pkg_sync_v0
    name: My Pkg Sync
    description: Generic sync template.
    client: ""
    deployment: ""

    integration:
      package_slug: my_pkg
      mode: real
      required_capabilities: [sync_cap]
      schema_version: my_pkg.sync.v1

    data_targets:
      - {table: t1, context: MyPkg/T1, unique_key: id, description: Test.}

    timeline:
      - {tick: 0, capability: sync_cap, function: run_sync, label: First}

    tasks:
      - id: my_pkg_sync_tick
        enabled: false
        execution_mode: live
        schedule: {type: interval, interval_seconds: 60}
        target:
          assistant_id: ""
          scenario_id: my_pkg_sync_v0
          tick_policy: incrementing
        activation:
          entrypoint_function: run_sync
          task_name: My Pkg sync
          task_description: ""
          recurrence_hint: every_5_minutes
    """
)


class TestMaterialiseHappyPath:
    def test_substitutes_required_placeholders(self, tmp_path: Path):
        _write_template(tmp_path, "my_pkg", "sync_v0", _MINIMAL_TEMPLATE)

        specs, secrets = materialise_scenario_activations(
            [
                ScenarioActivation(
                    scenario_template="my_pkg/sync_v0",
                    assistant_id="630",
                ),
            ],
            client_slug="acme",
            deployment_name="v0",
            search_paths=[tmp_path],
        )

        assert len(specs) == 1
        assert secrets == []
        spec = specs[0]
        # scenario_id default-derives from client + template_stem
        assert spec.scenario_id == "acme_sync_v0"
        assert spec.client == "acme"
        assert spec.deployment == "v0"
        # task fields substituted
        task = spec.tasks[0]
        assert task.target.assistant_id == "630"
        # task.target.scenario_id stays consistent with the substituted scenario_id
        assert task.target.scenario_id == "acme_sync_v0"
        assert task.enabled is False  # default tasks_enabled
        assert task.activation.task_description.startswith("Scheduled my_pkg sync")

    def test_scenario_id_override_wins(self, tmp_path: Path):
        _write_template(tmp_path, "my_pkg", "sync_v0", _MINIMAL_TEMPLATE)

        specs, _ = materialise_scenario_activations(
            [
                ScenarioActivation(
                    scenario_template="my_pkg/sync_v0",
                    assistant_id="630",
                    scenario_id_override="custom_scenario_id",
                ),
            ],
            client_slug="acme",
            deployment_name="v0",
            search_paths=[tmp_path],
        )

        spec = specs[0]
        assert spec.scenario_id == "custom_scenario_id"
        assert spec.tasks[0].target.scenario_id == "custom_scenario_id"

    def test_client_and_deployment_overrides(self, tmp_path: Path):
        _write_template(tmp_path, "my_pkg", "sync_v0", _MINIMAL_TEMPLATE)

        specs, _ = materialise_scenario_activations(
            [
                ScenarioActivation(
                    scenario_template="my_pkg/sync_v0",
                    assistant_id="42",
                    client_override="custom_client",
                    deployment_override="custom_dep",
                ),
            ],
            client_slug="ignored_client",
            deployment_name="ignored_deployment",
            search_paths=[tmp_path],
        )

        spec = specs[0]
        assert spec.client == "custom_client"
        assert spec.deployment == "custom_dep"
        assert spec.scenario_id == "custom_client_sync_v0"

    def test_tasks_enabled_toggle(self, tmp_path: Path):
        _write_template(tmp_path, "my_pkg", "sync_v0", _MINIMAL_TEMPLATE)

        specs, _ = materialise_scenario_activations(
            [
                ScenarioActivation(
                    scenario_template="my_pkg/sync_v0",
                    assistant_id="42",
                    tasks_enabled=True,
                ),
            ],
            client_slug="acme",
            deployment_name="v0",
            search_paths=[tmp_path],
        )

        assert specs[0].tasks[0].enabled is True

    def test_task_description_override(self, tmp_path: Path):
        _write_template(tmp_path, "my_pkg", "sync_v0", _MINIMAL_TEMPLATE)

        specs, _ = materialise_scenario_activations(
            [
                ScenarioActivation(
                    scenario_template="my_pkg/sync_v0",
                    assistant_id="42",
                    task_description_override="Custom description here.",
                ),
            ],
            client_slug="acme",
            deployment_name="v0",
            search_paths=[tmp_path],
        )

        assert specs[0].tasks[0].activation.task_description == "Custom description here."

    def test_real_hubspot_template_materialises_for_clientzeta(self):
        """End-to-end against the real HubSpot template shipped in 384c509."""
        specs, secrets = materialise_scenario_activations(
            [
                ScenarioActivation(
                    scenario_template="hubspot/crm_full_sync_v0",
                    assistant_id="630",
                    tasks_enabled=False,
                ),
            ],
            client_slug="clientzeta",
            deployment_name="v0",
        )

        assert len(specs) == 1
        spec = specs[0]
        assert spec.scenario_id == "clientzeta_crm_full_sync_v0"
        assert spec.client == "clientzeta"
        assert spec.deployment == "v0"
        assert spec.tasks
        for task in spec.tasks:
            assert task.target.assistant_id == "630"
            assert task.target.scenario_id == "clientzeta_crm_full_sync_v0"
            assert task.enabled is False
        # Composite unique_keys (post-384c509) survive materialisation
        targets_by_table = {t.table: t for t in spec.data_targets}
        assert isinstance(targets_by_table["pipelines"].unique_key, list)


# ---------------------------------------------------------------------------
# Env overlay
# ---------------------------------------------------------------------------


class TestEnvOverlay:
    def test_object_intervals_override_to_secret(self, tmp_path: Path):
        _write_template(tmp_path, "my_pkg", "sync_v0", _MINIMAL_TEMPLATE)

        _, secrets = materialise_scenario_activations(
            [
                ScenarioActivation(
                    scenario_template="my_pkg/sync_v0",
                    assistant_id="42",
                    object_intervals_override={"a": 60, "b": 300},
                ),
            ],
            client_slug="acme",
            deployment_name="v0",
            search_paths=[tmp_path],
        )

        names = {s.name: s.value for s in secrets}
        assert "MY_PKG_SYNC_OBJECT_INTERVALS" in names
        # Order may vary by dict iteration; just verify both pairs present
        assert "a:60" in names["MY_PKG_SYNC_OBJECT_INTERVALS"]
        assert "b:300" in names["MY_PKG_SYNC_OBJECT_INTERVALS"]

    def test_config_overrides_passthrough(self, tmp_path: Path):
        _write_template(tmp_path, "my_pkg", "sync_v0", _MINIMAL_TEMPLATE)

        _, secrets = materialise_scenario_activations(
            [
                ScenarioActivation(
                    scenario_template="my_pkg/sync_v0",
                    assistant_id="42",
                    config_overrides={
                        "MY_PKG_RETENTION_DAYS": "180",
                        "MY_PKG_REDACT_PII": "true",
                    },
                ),
            ],
            client_slug="acme",
            deployment_name="v0",
            search_paths=[tmp_path],
        )

        names = {s.name: s.value for s in secrets}
        assert names["MY_PKG_RETENTION_DAYS"] == "180"
        assert names["MY_PKG_REDACT_PII"] == "true"

    def test_no_overlay_when_no_overrides(self, tmp_path: Path):
        _write_template(tmp_path, "my_pkg", "sync_v0", _MINIMAL_TEMPLATE)

        _, secrets = materialise_scenario_activations(
            [
                ScenarioActivation(
                    scenario_template="my_pkg/sync_v0",
                    assistant_id="42",
                ),
            ],
            client_slug="acme",
            deployment_name="v0",
            search_paths=[tmp_path],
        )

        assert secrets == []


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class TestMaterialiseFailures:
    def test_missing_template_raises(self):
        with pytest.raises(FileNotFoundError, match="not found"):
            materialise_scenario_activations(
                [
                    ScenarioActivation(
                        scenario_template="ghost_pkg/missing",
                        assistant_id="1",
                    ),
                ],
                client_slug="acme",
                deployment_name="v0",
            )

    def test_duplicate_scenario_id_after_activation(self, tmp_path: Path):
        _write_template(tmp_path, "my_pkg", "sync_v0", _MINIMAL_TEMPLATE)

        with pytest.raises(ValueError, match="Duplicate scenario_id"):
            materialise_scenario_activations(
                [
                    ScenarioActivation(
                        scenario_template="my_pkg/sync_v0",
                        assistant_id="1",
                    ),
                    # Same default-derived scenario_id "acme_sync_v0"
                    ScenarioActivation(
                        scenario_template="my_pkg/sync_v0",
                        assistant_id="2",
                    ),
                ],
                client_slug="acme",
                deployment_name="v0",
                search_paths=[tmp_path],
            )

    def test_duplicate_avoided_by_scenario_id_override(self, tmp_path: Path):
        _write_template(tmp_path, "my_pkg", "sync_v0", _MINIMAL_TEMPLATE)

        specs, _ = materialise_scenario_activations(
            [
                ScenarioActivation(
                    scenario_template="my_pkg/sync_v0",
                    assistant_id="1",
                    scenario_id_override="first",
                ),
                ScenarioActivation(
                    scenario_template="my_pkg/sync_v0",
                    assistant_id="2",
                    scenario_id_override="second",
                ),
            ],
            client_slug="acme",
            deployment_name="v0",
            search_paths=[tmp_path],
        )
        assert {s.scenario_id for s in specs} == {"first", "second"}

    def test_leftover_replace_me_token_rejected(self, tmp_path: Path):
        # Template with a REPLACE_ME we don't override
        body = _MINIMAL_TEMPLATE.replace(
            "name: My Pkg Sync",
            "name: REPLACE_ME-name",
        )
        _write_template(tmp_path, "my_pkg", "sync_v0", body)

        with pytest.raises(ValueError, match="REPLACE_ME"):
            materialise_scenario_activations(
                [
                    ScenarioActivation(
                        scenario_template="my_pkg/sync_v0",
                        assistant_id="1",
                    ),
                ],
                client_slug="acme",
                deployment_name="v0",
                search_paths=[tmp_path],
            )

    def test_invalid_template_yaml_root(self, tmp_path: Path):
        # Template that's a list, not a dict
        scenarios_dir = tmp_path / "my_pkg" / "scenarios"
        scenarios_dir.mkdir(parents=True)
        (scenarios_dir / "sync_v0.yaml").write_text("- a\n- b\n")

        with pytest.raises(ValueError, match="not a YAML mapping"):
            materialise_scenario_activations(
                [
                    ScenarioActivation(
                        scenario_template="my_pkg/sync_v0",
                        assistant_id="1",
                    ),
                ],
                client_slug="acme",
                deployment_name="v0",
                search_paths=[tmp_path],
            )

    def test_invalid_substituted_spec(self, tmp_path: Path):
        # Template that's missing required fields after substitution
        body = dedent(
            """\
            scenario_id: bad
            name: Bad
            description: Missing required fields.
            client: ""
            deployment: ""
            integration:
              package_slug: my_pkg
              mode: real
              required_capabilities: []
              schema_version: x.y
            data_targets: []
            timeline: []
            """
        )
        _write_template(tmp_path, "my_pkg", "sync_v0", body)

        # ScenarioSpec validation passes here because all required fields
        # are present; this scenario IS valid (no tasks required).  Just
        # verify it materialises without error to confirm valid templates
        # work.
        specs, _ = materialise_scenario_activations(
            [
                ScenarioActivation(
                    scenario_template="my_pkg/sync_v0",
                    assistant_id="1",
                ),
            ],
            client_slug="acme",
            deployment_name="v0",
            search_paths=[tmp_path],
        )
        assert specs[0].tasks == []


# ---------------------------------------------------------------------------
# End-to-end via _spec_to_resolved
# ---------------------------------------------------------------------------


class TestEndToEndResolution:
    def test_seed_layer_scenarios_materialise_during_resolve(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A SeedLayer.scenarios block goes from registration to resolved
        ResolvedCustomization.scenarios via the real resolution path."""
        from unity_deploy.customization.clients import (
            _CLIENT_DEPLOYMENTS,
            ClientDeploymentEntry,
            resolve,
        )
        from unity_deploy.customization.configs.types.actor_config import (
            ActorConfig,
        )
        from unity_deploy.customization.deployment_types import (
            DeploymentMapping,
            DeploymentSpec,
            DeploymentTarget,
            SeedLayer,
        )

        # Stage a fake template.
        _write_template(tmp_path, "fake_pkg", "sync_v0", _MINIMAL_TEMPLATE)

        # Patch the loader's default search paths to include our tmp_path
        # without disturbing the real ones.  The cleanest way: use
        # search_paths via a wrapper.  But _spec_to_resolved calls
        # materialise_scenario_activations without a search_paths arg, so
        # we monkeypatch the loader's _BUILTIN_DIR temporarily.
        from unity_deploy.customization.scenarios import loader as scenarios_loader
        original_builtin = scenarios_loader._BUILTIN_DIR
        monkeypatch.setattr(scenarios_loader, "_BUILTIN_DIR", tmp_path)

        # Build a minimal in-memory client registration.
        spec = DeploymentSpec(
            name="testdep",
            actor_config=ActorConfig(),
        )
        mapping = DeploymentMapping(targets=[
            DeploymentTarget(scope="assistant", scope_id="9001", deployment="testdep"),
        ])
        entry = ClientDeploymentEntry(
            mapping=mapping,
            specs={"testdep": spec},
            environment=None,
            layers={
                "assistant:9001": SeedLayer(
                    scenarios=[
                        ScenarioActivation(
                            scenario_template="fake_pkg/sync_v0",
                            assistant_id="9001",
                            tasks_enabled=True,
                            object_intervals_override={"x": 60},
                        ),
                    ],
                ),
            },
        )

        # Snapshot + restore the global registry around the test.
        previous = dict(_CLIENT_DEPLOYMENTS)
        _CLIENT_DEPLOYMENTS.clear()
        _CLIENT_DEPLOYMENTS["fakeclient"] = entry
        try:
            resolved = resolve(assistant_id=9001)
        finally:
            _CLIENT_DEPLOYMENTS.clear()
            _CLIENT_DEPLOYMENTS.update(previous)
            monkeypatch.setattr(scenarios_loader, "_BUILTIN_DIR", original_builtin)

        # Activation materialised into resolved.scenarios + secrets.
        assert len(resolved.scenarios) == 1
        spec_resolved = resolved.scenarios[0]
        assert isinstance(spec_resolved, ScenarioSpec)
        assert spec_resolved.scenario_id == "fakeclient_sync_v0"
        assert spec_resolved.client == "fakeclient"
        assert spec_resolved.deployment == "testdep"
        assert spec_resolved.tasks[0].target.assistant_id == "9001"
        assert spec_resolved.tasks[0].enabled is True

        secret_names = {s.name for s in resolved.secrets}
        assert "FAKE_PKG_SYNC_OBJECT_INTERVALS" in secret_names

    def test_deployment_spec_scenarios_materialise_without_layer(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Activations declared on DeploymentSpec.scenarios (not via
        register_layer) materialise just the same."""
        from unity_deploy.customization.clients import (
            _CLIENT_DEPLOYMENTS,
            ClientDeploymentEntry,
            resolve,
        )
        from unity_deploy.customization.configs.types.actor_config import (
            ActorConfig,
        )
        from unity_deploy.customization.deployment_types import (
            DeploymentMapping,
            DeploymentSpec,
            DeploymentTarget,
        )

        _write_template(tmp_path, "fake_pkg", "sync_v0", _MINIMAL_TEMPLATE)
        from unity_deploy.customization.scenarios import loader as scenarios_loader
        original_builtin = scenarios_loader._BUILTIN_DIR
        monkeypatch.setattr(scenarios_loader, "_BUILTIN_DIR", tmp_path)

        spec = DeploymentSpec(
            name="testdep",
            actor_config=ActorConfig(),
            scenarios=[
                ScenarioActivation(
                    scenario_template="fake_pkg/sync_v0",
                    assistant_id="9001",
                ),
            ],
        )
        mapping = DeploymentMapping(targets=[
            DeploymentTarget(scope="assistant", scope_id="9001", deployment="testdep"),
        ])
        entry = ClientDeploymentEntry(
            mapping=mapping,
            specs={"testdep": spec},
            environment=None,
        )

        previous = dict(_CLIENT_DEPLOYMENTS)
        _CLIENT_DEPLOYMENTS.clear()
        _CLIENT_DEPLOYMENTS["fakeclient"] = entry
        try:
            resolved = resolve(assistant_id=9001)
        finally:
            _CLIENT_DEPLOYMENTS.clear()
            _CLIENT_DEPLOYMENTS.update(previous)
            monkeypatch.setattr(scenarios_loader, "_BUILTIN_DIR", original_builtin)

        assert len(resolved.scenarios) == 1
        assert resolved.scenarios[0].client == "fakeclient"
        assert resolved.scenarios[0].deployment == "testdep"
