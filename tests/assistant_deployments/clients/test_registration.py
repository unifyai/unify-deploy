"""Deployment registration and resolution tests.

Tests the full pipeline: DeploymentSpec -> register_client() ->
resolve_from_deployments() / resolve().  Verifies isolated resolution,
environment guardrails, inheritance via derive(), correct
mapping-based routing, and shared seed-data layers.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from unity_deploy.assistant_deployments.configs.types.actor_config import ActorConfig
from unity_deploy.assistant_deployments.clients import (
    _CLIENT_DEPLOYMENTS,
    resolve,
    resolve_from_deployments,
)
from unity_deploy.assistant_deployments.deployment_types import (
    DeploymentMapping,
    DeploymentSpec,
    DeploymentTarget,
    SecretEntry,
    SeedLayer,
    _merge_actor_configs,
    detect_environment,
    register_client,
    register_layer,
    resolve_deployment_name,
)
from unity_deploy.assistant_deployments.blacklist_source import (
    entry_from_fields,
    write_blacklist_jsonl,
)
from unity_deploy.assistant_deployments.guidance_source import (
    slugify_key,
    write_guidance_jsonl,
)
from unify.blacklist_manager.custom_blacklist import collect_blacklist_from_directories
from unify.guidance_manager.custom_guidance import collect_guidance_from_directories

_GUIDANCE_ROOT = Path("/tmp/unify-deploy-test-guidance")
_BLACKLIST_ROOT = Path("/tmp/unify-deploy-test-blacklist")


def _write_test_guidance(
    name: str,
    *,
    title: str,
    content: str,
) -> Path:
    return write_guidance_jsonl(
        _GUIDANCE_ROOT / name,
        [
            {
                "key": slugify_key(title),
                "title": title,
                "content": content,
            },
        ],
    )


def _write_test_blacklist(
    name: str,
    *,
    medium: str,
    contact_detail: str,
    reason: str,
) -> Path:
    return write_blacklist_jsonl(
        _BLACKLIST_ROOT / name,
        [
            entry_from_fields(
                medium=medium,
                contact_detail=contact_detail,
                reason=reason,
            ),
        ],
    )


def _guidance_titles(resolved) -> set[str]:
    source = collect_guidance_from_directories(resolved.guidance_dirs)
    return {entry["title"] for entry in source.values()}


# ---------------------------------------------------------------------------
# Registry cleanup fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    """Snapshot and restore the client deployment registry so tests don't leak."""
    saved_clients = dict(_CLIENT_DEPLOYMENTS)
    _CLIENT_DEPLOYMENTS.clear()
    yield
    _CLIENT_DEPLOYMENTS.clear()
    _CLIENT_DEPLOYMENTS.update(saved_clients)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(
    name: str,
    guideline_text: str,
    *,
    secrets: list[SecretEntry] | None = None,
    function_dir: Path | None = None,
    console_config: dict | None = None,
) -> DeploymentSpec:
    return DeploymentSpec(
        name=name,
        actor_config=ActorConfig(guidelines=guideline_text),
        guidance_dir=_write_test_guidance(
            name,
            title=f"{name} guide",
            content=f"Guidance content for {name}. " + "x" * 50,
        ),
        secrets=secrets or [],
        function_dir=function_dir,
        console_config=console_config,
    )


_FAKE_DIR_A = Path("/tmp/test_funcs_a")
_FAKE_DIR_B = Path("/tmp/test_funcs_b")


# ═══════════════════════════════════════════════════════════════════════════
# TestDeploymentMappingResolution — resolve_deployment_name() priority
# ═══════════════════════════════════════════════════════════════════════════


class TestDeploymentMappingResolution:

    def test_user_match_wins_over_default(self):
        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(scope="user", scope_id="user-aaa", deployment="v1"),
                DeploymentTarget(scope="default", deployment="v0"),
            ],
        )
        assert resolve_deployment_name(mapping, user_id="user-aaa") == "v1"

    def test_org_match_wins_over_default(self):
        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(scope="org", scope_id="5", deployment="v1"),
                DeploymentTarget(scope="default", deployment="v0"),
            ],
        )
        assert resolve_deployment_name(mapping, org_id=5) == "v1"

    def test_team_match_wins_over_default(self):
        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(scope="team", scope_id="42", deployment="v1"),
                DeploymentTarget(scope="default", deployment="v0"),
            ],
        )
        assert resolve_deployment_name(mapping, team_ids=[42]) == "v1"

    def test_assistant_match_wins_over_default(self):
        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(scope="assistant", scope_id="99", deployment="v1"),
                DeploymentTarget(scope="default", deployment="v0"),
            ],
        )
        assert resolve_deployment_name(mapping, assistant_id=99) == "v1"

    def test_no_match_falls_to_default(self):
        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(scope="user", scope_id="user-aaa", deployment="v1"),
                DeploymentTarget(scope="default", deployment="v0"),
            ],
        )
        assert resolve_deployment_name(mapping, user_id="user-zzz") == "v0"

    def test_no_match_no_default_raises(self):
        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(scope="user", scope_id="user-aaa", deployment="v1"),
            ],
        )
        with pytest.raises(ValueError, match="No matching"):
            resolve_deployment_name(mapping, user_id="user-zzz")

    def test_first_matching_target_wins(self):
        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(scope="user", scope_id="user-aaa", deployment="v1"),
                DeploymentTarget(scope="user", scope_id="user-aaa", deployment="v2"),
                DeploymentTarget(scope="default", deployment="v0"),
            ],
        )
        assert resolve_deployment_name(mapping, user_id="user-aaa") == "v1"

    def test_team_not_matched_without_team_ids(self):
        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(scope="team", scope_id="42", deployment="v1"),
                DeploymentTarget(scope="default", deployment="v0"),
            ],
        )
        assert resolve_deployment_name(mapping) == "v0"

    def test_multiple_teams_first_match_wins(self):
        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(scope="team", scope_id="10", deployment="team10-dep"),
                DeploymentTarget(scope="team", scope_id="20", deployment="team20-dep"),
                DeploymentTarget(scope="default", deployment="v0"),
            ],
        )
        assert resolve_deployment_name(mapping, team_ids=[20, 10]) == "team10-dep"


# ═══════════════════════════════════════════════════════════════════════════
# TestDetectEnvironment — environment detection from ORCHESTRA_URL
# ═══════════════════════════════════════════════════════════════════════════


class TestDetectEnvironment:

    def test_staging_url(self, monkeypatch):
        monkeypatch.setenv("ORCHESTRA_URL", "https://internal.example.com/v0")
        assert detect_environment() == "staging"

    def test_production_url(self, monkeypatch):
        monkeypatch.setenv("ORCHESTRA_URL", "https://api.unify.ai/v0")
        assert detect_environment() == "production"

    def test_localhost_url(self, monkeypatch):
        monkeypatch.setenv("ORCHESTRA_URL", "http://localhost:8000")
        assert detect_environment() == "development"

    def test_127_url(self, monkeypatch):
        monkeypatch.setenv("ORCHESTRA_URL", "http://127.0.0.1:8000")
        assert detect_environment() == "development"

    def test_unset_defaults_to_production(self, monkeypatch):
        monkeypatch.delenv("ORCHESTRA_URL", raising=False)
        assert detect_environment() == "production"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("ORCHESTRA_URL", "https://STAGING.Internal.Saas.Unify.AI/v0")
        assert detect_environment() == "staging"


# ═══════════════════════════════════════════════════════════════════════════
# TestDeploymentSpecDerive — inheritance via .derive()
# ═══════════════════════════════════════════════════════════════════════════


class TestDeploymentSpecDerive:

    def test_basic_field_override(self):
        base = _make_spec("base", "Base guidelines")
        derived = base.derive(name="v1")
        assert derived.name == "v1"
        assert derived.actor_config.guidelines == "Base guidelines"

    def test_actor_config_deep_merge(self):
        base = DeploymentSpec(
            name="base",
            actor_config=ActorConfig(
                guidelines="Base guidelines",
                model="claude-4.5-opus@anthropic",
            ),
        )
        derived = base.derive(
            name="v1",
            actor_config=ActorConfig(guidelines="Override guidelines"),
        )
        assert derived.actor_config.guidelines == "Override guidelines"
        assert derived.actor_config.model == "claude-4.5-opus@anthropic"

    def test_actor_config_base_preserved_when_override_none(self):
        base = DeploymentSpec(
            name="base",
            actor_config=ActorConfig(can_compose=True, timeout=120.0),
        )
        derived = base.derive(
            name="v1",
            actor_config=ActorConfig(can_compose=False),
        )
        assert derived.actor_config.can_compose is False
        assert derived.actor_config.timeout == 120.0

    def test_guidance_replaced_wholesale(self):
        base = _make_spec("base", "Base")
        new_dir = _write_test_guidance(
            "base-v1",
            title="New",
            content="New content " + "x" * 50,
        )
        derived = base.derive(name="v1", guidance_dir=new_dir)
        source = collect_guidance_from_directories([derived.guidance_dir])
        assert len(source) == 1
        assert list(source.values())[0]["title"] == "New"

    def test_guidance_inherited_when_not_passed(self):
        base = _make_spec("base", "Base")
        derived = base.derive(name="v1")
        assert derived.guidance_dir == base.guidance_dir

    def test_function_dir_override(self):
        base = _make_spec("base", "Base", function_dir=_FAKE_DIR_A)
        derived = base.derive(name="v1", function_dir=_FAKE_DIR_B)
        assert derived.function_dir == _FAKE_DIR_B

    def test_secrets_inherited(self):
        secrets = [
            SecretEntry(name="KEY_A", value="val", description="Desc"),
        ]
        base = _make_spec("base", "Base", secrets=secrets)
        derived = base.derive(name="v1")
        assert len(derived.secrets) == 1
        assert derived.secrets[0].name == "KEY_A"

    def test_secrets_replaced_when_passed(self):
        base_secrets = [
            SecretEntry(name="OLD", value="old", description="Old"),
        ]
        new_secrets = [
            SecretEntry(name="NEW", value="new", description="New"),
        ]
        base = _make_spec("base", "Base", secrets=base_secrets)
        derived = base.derive(name="v1", secrets=new_secrets)
        assert len(derived.secrets) == 1
        assert derived.secrets[0].name == "NEW"


# ═══════════════════════════════════════════════════════════════════════════
# TestMergeActorConfigs — the helper used by derive()
# ═══════════════════════════════════════════════════════════════════════════


class TestMergeActorConfigs:

    def test_override_wins(self):
        base = ActorConfig(guidelines="base", model="gpt-4")
        override = ActorConfig(guidelines="override")
        merged = _merge_actor_configs(base, override)
        assert merged.guidelines == "override"
        assert merged.model == "gpt-4"

    def test_all_none_override_keeps_base(self):
        base = ActorConfig(guidelines="base", can_compose=True)
        override = ActorConfig()
        merged = _merge_actor_configs(base, override)
        assert merged.guidelines == "base"
        assert merged.can_compose is True

    def test_both_none_stays_none(self):
        merged = _merge_actor_configs(ActorConfig(), ActorConfig())
        assert merged.guidelines is None
        assert merged.model is None


# ═══════════════════════════════════════════════════════════════════════════
# TestRegisterClient — isolated registration path
# ═══════════════════════════════════════════════════════════════════════════


class TestRegisterClient:

    def test_registers_into_client_deployments(self, monkeypatch):
        from unity_deploy.assistant_deployments import deployment_types as dt

        v0 = _make_spec("v0", "Default v0")
        monkeypatch.setattr(dt, "load_deployment", lambda d, n: v0)

        mapping = DeploymentMapping(
            targets=[DeploymentTarget(scope="default", deployment="v0")],
        )
        loaded = register_client(
            "test_client",
            mapping,
            Path("/fake"),
            environment="production",
        )

        assert "v0" in loaded
        assert "test_client" in _CLIENT_DEPLOYMENTS
        entry = _CLIENT_DEPLOYMENTS["test_client"]
        assert entry.environment == "production"

    def test_multiple_deployments_loaded_once(self, monkeypatch):
        from unity_deploy.assistant_deployments import deployment_types as dt

        call_count = {"v0": 0, "v1": 0}
        v0 = _make_spec("v0", "Default v0")
        v1 = _make_spec("v1", "Assistant v1")

        def counting_load(d, n):
            call_count[n] += 1
            return {"v0": v0, "v1": v1}[n]

        monkeypatch.setattr(dt, "load_deployment", counting_load)

        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(scope="assistant", scope_id="99", deployment="v1"),
                DeploymentTarget(scope="assistant", scope_id="100", deployment="v1"),
                DeploymentTarget(scope="default", deployment="v0"),
            ],
        )
        loaded = register_client("test", mapping, Path("/fake"))

        assert set(loaded.keys()) == {"v0", "v1"}
        assert call_count["v1"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# TestIsolatedResolution — resolve_from_deployments() returns pure specs
# ═══════════════════════════════════════════════════════════════════════════


class TestIsolatedResolution:

    def _register_with_default(self, monkeypatch, *, environment=None):
        """Register a client whose mapping has assistant + default targets."""
        from unity_deploy.assistant_deployments import deployment_types as dt

        v0 = _make_spec("v0", "Default v0")
        v1 = _make_spec("v1", "Assistant v1")
        monkeypatch.setattr(
            dt,
            "load_deployment",
            lambda d, n: {"v0": v0, "v1": v1}[n],
        )

        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(scope="assistant", scope_id="99", deployment="v1"),
                DeploymentTarget(scope="default", deployment="v0"),
            ],
        )
        register_client(
            "test_client",
            mapping,
            Path("/fake"),
            environment=environment,
        )

    def _register_assistant_only(self, monkeypatch, *, environment=None):
        """Register a client whose mapping has only assistant targets (no default)."""
        from unity_deploy.assistant_deployments import deployment_types as dt

        v1 = _make_spec("v1", "Assistant v1")
        monkeypatch.setattr(dt, "load_deployment", lambda d, n: v1)

        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(scope="assistant", scope_id="99", deployment="v1"),
                DeploymentTarget(scope="assistant", scope_id="200", deployment="v1"),
            ],
        )
        register_client(
            "test_client",
            mapping,
            Path("/fake"),
            environment=environment,
        )

    def test_assistant_gets_pure_v1(self, monkeypatch):
        self._register_with_default(monkeypatch)
        result = resolve_from_deployments(org_id=10, assistant_id=99)
        assert result is not None
        assert result.config.guidelines == "Assistant v1"
        assert len(result.guidance_dirs) == 1
        titles = _guidance_titles(result)
        assert "v1 guide" in titles

    def test_default_catches_unmatched_session(self, monkeypatch):
        self._register_with_default(monkeypatch)
        result = resolve_from_deployments(org_id=10, assistant_id=1)
        assert result is not None
        assert result.config.guidelines == "Default v0"

    def test_no_bleed_between_specs(self, monkeypatch):
        """v0 default data should NOT bleed into v1 assistant-level."""
        self._register_with_default(monkeypatch)
        result = resolve_from_deployments(org_id=10, assistant_id=99)
        assert result is not None
        assert "Default v0" not in (result.config.guidelines or "")
        assert len(result.guidance_dirs) == 1

    def test_assistant_match_ignores_org_mismatch(self, monkeypatch):
        """Assistant target matches regardless of org_id in the session."""
        self._register_assistant_only(monkeypatch)
        result = resolve_from_deployments(org_id=999, assistant_id=99)
        assert result is not None
        assert result.config.guidelines == "Assistant v1"

    def test_no_target_match_returns_none(self, monkeypatch):
        """When no target matches and there is no default, return None."""
        self._register_assistant_only(monkeypatch)
        result = resolve_from_deployments(org_id=10, assistant_id=1)
        assert result is None

    def test_resolve_uses_deployment_path(self, monkeypatch):
        self._register_with_default(monkeypatch)
        result = resolve(org_id=10, assistant_id=99)
        assert result.config.guidelines == "Assistant v1"

    def test_resolve_returns_defaults_when_no_client(self):
        result = resolve(org_id=999)
        assert result.config == ActorConfig()
        assert result.function_dirs == []
        assert result.guidance_dirs == []

    def test_function_dir_in_resolved(self, monkeypatch):
        from unity_deploy.assistant_deployments import deployment_types as dt

        spec = _make_spec("v0", "With funcs", function_dir=_FAKE_DIR_A)
        monkeypatch.setattr(dt, "load_deployment", lambda d, n: spec)

        mapping = DeploymentMapping(
            targets=[DeploymentTarget(scope="default", deployment="v0")],
        )
        register_client("test", mapping, Path("/fake"))

        result = resolve(org_id=10)
        assert result.function_dirs == [_FAKE_DIR_A]

    def test_secrets_in_resolved(self, monkeypatch):
        from unity_deploy.assistant_deployments import deployment_types as dt

        secrets = [
            SecretEntry(name="KEY_A", value="val-a", description="Secret A"),
        ]
        spec = _make_spec("v0", "With secrets", secrets=secrets)
        monkeypatch.setattr(dt, "load_deployment", lambda d, n: spec)

        mapping = DeploymentMapping(
            targets=[DeploymentTarget(scope="default", deployment="v0")],
        )
        register_client("test", mapping, Path("/fake"))

        result = resolve(org_id=10)
        secret_names = {s.name for s in result.secrets}
        assert "KEY_A" in secret_names

    def test_console_config_in_resolved(self, monkeypatch):
        from unity_deploy.assistant_deployments import deployment_types as dt

        console_config = {
            "version": "1",
            "layout": {"mode": "dashboard-centric", "defaultTab": "dashboards"},
            "tabs": {"hidden": ["memory", "secrets"]},
        }
        spec = _make_spec(
            "v0",
            "With console config",
            console_config=console_config,
        )
        monkeypatch.setattr(dt, "load_deployment", lambda d, n: spec)

        mapping = DeploymentMapping(
            targets=[DeploymentTarget(scope="default", deployment="v0")],
        )
        register_client("test", mapping, Path("/fake"))

        result = resolve(org_id=10)
        assert result.console_config == console_config


# ═══════════════════════════════════════════════════════════════════════════
# TestEnvironmentGuardrail — env mismatch blocks resolution
# ═══════════════════════════════════════════════════════════════════════════


class TestEnvironmentGuardrail:

    def test_env_mismatch_skips_client(self, monkeypatch):
        from unity_deploy.assistant_deployments import deployment_types as dt

        spec = _make_spec("v0", "Staging-only")
        monkeypatch.setattr(dt, "load_deployment", lambda d, n: spec)

        mapping = DeploymentMapping(
            targets=[DeploymentTarget(scope="default", deployment="v0")],
        )
        register_client(
            "staging_client",
            mapping,
            Path("/fake"),
            environment="staging",
        )

        monkeypatch.setenv("ORCHESTRA_URL", "https://api.unify.ai/v0")
        result = resolve_from_deployments(org_id=10)
        assert result is None

    def test_env_match_resolves(self, monkeypatch):
        from unity_deploy.assistant_deployments import deployment_types as dt

        spec = _make_spec("v0", "Production-only")
        monkeypatch.setattr(dt, "load_deployment", lambda d, n: spec)

        mapping = DeploymentMapping(
            targets=[DeploymentTarget(scope="default", deployment="v0")],
        )
        register_client(
            "prod_client",
            mapping,
            Path("/fake"),
            environment="production",
        )

        monkeypatch.setenv("ORCHESTRA_URL", "https://api.unify.ai/v0")
        result = resolve_from_deployments(org_id=10)
        assert result is not None
        assert result.config.guidelines == "Production-only"

    def test_no_env_tag_always_matches(self, monkeypatch):
        from unity_deploy.assistant_deployments import deployment_types as dt

        spec = _make_spec("v0", "Any env")
        monkeypatch.setattr(dt, "load_deployment", lambda d, n: spec)

        mapping = DeploymentMapping(
            targets=[DeploymentTarget(scope="default", deployment="v0")],
        )
        register_client(
            "universal_client",
            mapping,
            Path("/fake"),
        )

        monkeypatch.setenv("ORCHESTRA_URL", "https://internal.example.com/v0")
        result = resolve_from_deployments(org_id=10)
        assert result is not None


# ═══════════════════════════════════════════════════════════════════════════
# TestUnifyCompanyRouting — default deployment is org-scoped
# ═══════════════════════════════════════════════════════════════════════════


class TestUnifyCompanyRouting:

    @staticmethod
    def _reload_unify_company(
        monkeypatch,
        orchestra_url: str,
        *,
        brain_operator_assistant_id: str | None = None,
    ):
        monkeypatch.setenv("ORCHESTRA_URL", orchestra_url)
        if brain_operator_assistant_id is not None:
            monkeypatch.setenv(
                "BRAIN_OPERATOR_ASSISTANT_ID",
                brain_operator_assistant_id,
            )
        else:
            monkeypatch.delenv("BRAIN_OPERATOR_ASSISTANT_ID", raising=False)
        import unity_deploy.assistant_deployments.clients.unify_company as uc

        return importlib.reload(uc)

    def test_production_default_deployment_is_unify_org_only(self, monkeypatch):
        self._reload_unify_company(monkeypatch, "https://api.unify.ai/v0")

        matched = resolve_from_deployments(org_id=1)
        assert matched is not None
        assert _guidance_titles(matched) >= {
            "CRM stage hygiene",
            "CRM email sending policy",
        }

        assert resolve_from_deployments(org_id=2) is None
        assert resolve_from_deployments(user_id="cli3t38uc0000s60k5zmgj8ez") is None

    def test_staging_default_deployment_is_unify_org_only(self, monkeypatch):
        self._reload_unify_company(
            monkeypatch,
            "https://internal.example.com/v0",
        )

        matched = resolve_from_deployments(org_id=5)
        assert matched is not None
        assert _guidance_titles(matched) >= {
            "CRM stage hygiene",
            "CRM email sending policy",
        }

        assert resolve_from_deployments(org_id=1) is None

    def test_org_id_override(self, monkeypatch):
        monkeypatch.setenv("UNIFY_COMPANY_ORG_ID", "123")
        self._reload_unify_company(monkeypatch, "http://127.0.0.1:8000/v0")

        assert resolve_from_deployments(org_id=123) is not None
        assert resolve_from_deployments(org_id=5) is None

    def test_brain_operator_targets_production_assistant(self, monkeypatch):
        uc = self._reload_unify_company(
            monkeypatch,
            "https://api.unify.ai/v0",
            brain_operator_assistant_id="1406",
        )
        targets = uc._MAPPING.targets
        brain_targets = [t for t in targets if t.deployment == "brain_operator"]
        assert len(brain_targets) == 1
        assert brain_targets[0].scope_id == "1406"

    def test_brain_operator_targets_staging_assistant(self, monkeypatch):
        uc = self._reload_unify_company(
            monkeypatch,
            "https://internal.example.com/v0",
            brain_operator_assistant_id="7367",
        )
        brain_targets = [
            t for t in uc._MAPPING.targets if t.deployment == "brain_operator"
        ]
        assert len(brain_targets) == 1
        assert brain_targets[0].scope_id == "7367"

    def test_brain_operator_targets_staging_assistant_by_default(self, monkeypatch):
        uc = self._reload_unify_company(
            monkeypatch,
            "https://internal.example.com/v0",
        )
        brain_targets = [
            t for t in uc._MAPPING.targets if t.deployment == "brain_operator"
        ]
        assert len(brain_targets) == 1
        assert brain_targets[0].scope_id == "7367"

    def test_brain_operator_skipped_on_unknown_environment(self, monkeypatch):
        uc = self._reload_unify_company(
            monkeypatch,
            "http://127.0.0.1:8000/v0",
        )
        brain_targets = [
            t for t in uc._MAPPING.targets if t.deployment == "brain_operator"
        ]
        assert brain_targets == []

    def test_brain_operator_resolves_on_staging_assistant(self, monkeypatch):
        self._reload_unify_company(
            monkeypatch,
            "https://internal.example.com/v0",
            brain_operator_assistant_id="7367",
        )
        matched = resolve_from_deployments(assistant_id=7367)
        assert matched is not None
        assert _guidance_titles(matched) >= {"Brain operator role definition"}

        assert resolve_from_deployments(assistant_id=1406) is None

    def test_brain_operator_resolves_on_production_assistant_only(self, monkeypatch):
        self._reload_unify_company(
            monkeypatch,
            "https://api.unify.ai/v0",
            brain_operator_assistant_id="1406",
        )
        matched = resolve_from_deployments(assistant_id=1406)
        assert matched is not None
        assert _guidance_titles(matched) >= {"Brain operator role definition"}

        assert resolve_from_deployments(assistant_id=7367) is None


# ═══════════════════════════════════════════════════════════════════════════
# TestScopeRouting — org-wide, user-wide, assistant-specific coexistence
# ═══════════════════════════════════════════════════════════════════════════


class TestScopeRouting:
    """Verify that scoping lives entirely in DeploymentTarget."""

    def test_org_wide_catches_any_assistant_in_org(self, monkeypatch):
        from unity_deploy.assistant_deployments import deployment_types as dt

        v1 = _make_spec("v1", "Org-wide v1")
        monkeypatch.setattr(dt, "load_deployment", lambda d, n: v1)

        mapping = DeploymentMapping(
            targets=[DeploymentTarget(scope="org", scope_id="7", deployment="v1")],
        )
        register_client("test", mapping, Path("/fake"))

        assert resolve_from_deployments(org_id=7, assistant_id=83) is not None
        assert resolve_from_deployments(org_id=7, assistant_id=999) is not None
        assert resolve_from_deployments(org_id=99, assistant_id=83) is None

    def test_assistant_target_beats_org_target(self, monkeypatch):
        from unity_deploy.assistant_deployments import deployment_types as dt

        v0 = _make_spec("v0", "Personal v0")
        v1 = _make_spec("v1", "Org-wide v1")
        monkeypatch.setattr(
            dt,
            "load_deployment",
            lambda d, n: {"v0": v0, "v1": v1}[n],
        )

        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(scope="assistant", scope_id="378", deployment="v0"),
                DeploymentTarget(scope="org", scope_id="7", deployment="v1"),
            ],
        )
        register_client("test", mapping, Path("/fake"))

        personal = resolve_from_deployments(org_id=None, assistant_id=378)
        assert personal is not None
        assert personal.config.guidelines == "Personal v0"

        org_asst = resolve_from_deployments(org_id=7, assistant_id=83)
        assert org_asst is not None
        assert org_asst.config.guidelines == "Org-wide v1"

    def test_user_wide_catches_any_assistant_for_user(self, monkeypatch):
        from unity_deploy.assistant_deployments import deployment_types as dt

        v1 = _make_spec("v1", "User-wide v1")
        monkeypatch.setattr(dt, "load_deployment", lambda d, n: v1)

        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(scope="user", scope_id="user-abc", deployment="v1"),
            ],
        )
        register_client("test", mapping, Path("/fake"))

        result = resolve_from_deployments(user_id="user-abc", assistant_id=50)
        assert result is not None
        assert result.config.guidelines == "User-wide v1"

        assert resolve_from_deployments(user_id="other", assistant_id=50) is None


# ═══════════════════════════════════════════════════════════════════════════
# TestSeedLayers — shared seed data layering
# ═══════════════════════════════════════════════════════════════════════════


class TestSeedLayers:
    """Verify register_layer() and the merge behaviour in _spec_to_resolved."""

    def _register(self, monkeypatch):
        from unity_deploy.assistant_deployments import deployment_types as dt

        spec = _make_spec("v0", "Default v0")
        monkeypatch.setattr(dt, "load_deployment", lambda d, n: spec)

        mapping = DeploymentMapping(
            targets=[DeploymentTarget(scope="default", deployment="v0")],
        )
        register_client("test_client", mapping, Path("/fake"))

    # -- registration API --

    def test_register_layer_stores_on_entry(self, monkeypatch):
        self._register(monkeypatch)
        register_layer(
            "test_client",
            "org",
            "7",
            SeedLayer(contacts=[{"first_name": "Alice", "surname": "Smith"}]),
        )
        entry = _CLIENT_DEPLOYMENTS["test_client"]
        assert "org:7" in entry.layers
        assert len(entry.layers["org:7"].contacts) == 1

    def test_register_layer_requires_existing_client(self):
        with pytest.raises(KeyError, match="not registered"):
            register_layer(
                "nonexistent",
                "org",
                "1",
                SeedLayer(contacts=[{"first_name": "X"}]),
            )

    def test_register_layer_validates_scope(self, monkeypatch):
        self._register(monkeypatch)
        with pytest.raises(ValueError, match="Invalid scope"):
            register_layer("test_client", "galaxy", "1", SeedLayer())

    # -- contacts merge --

    def test_org_layer_contacts_merged(self, monkeypatch):
        self._register(monkeypatch)
        register_layer(
            "test_client",
            "org",
            "10",
            SeedLayer(contacts=[{"first_name": "Org", "surname": "Contact"}]),
        )
        result = resolve(org_id=10)
        names = [(c["first_name"], c["surname"]) for c in result.contacts]
        assert ("Org", "Contact") in names

    def test_user_layer_contacts_override_org(self, monkeypatch):
        self._register(monkeypatch)
        register_layer(
            "test_client",
            "org",
            "10",
            SeedLayer(
                contacts=[
                    {"first_name": "Shared", "surname": "Person", "email": "org@x.com"},
                ],
            ),
        )
        register_layer(
            "test_client",
            "user",
            "user-aaa",
            SeedLayer(
                contacts=[
                    {
                        "first_name": "Shared",
                        "surname": "Person",
                        "email": "user@x.com",
                    },
                ],
            ),
        )
        result = resolve(org_id=10, user_id="user-aaa")
        by_name = {f"{c['first_name']}|{c['surname']}": c for c in result.contacts}
        assert by_name["Shared|Person"]["email"] == "user@x.com"

    # -- guidance merge --

    def test_team_layer_guidance_added(self, monkeypatch):
        self._register(monkeypatch)
        team_dir = _write_test_guidance(
            "team-layer",
            title="Team tip",
            content="Extra guidance " + "x" * 50,
        )
        register_layer(
            "test_client",
            "team",
            "100",
            SeedLayer(guidance_dir=team_dir),
        )
        result = resolve(org_id=10, team_ids=[100])
        titles = _guidance_titles(result)
        assert "Team tip" in titles
        assert "v0 guide" in titles

    def test_guidance_overlay_wins_by_key(self, monkeypatch):
        self._register(monkeypatch)
        overlay_dir = _write_test_guidance(
            "user-overlay",
            title="v0 guide",
            content="Overridden guidance content " + "x" * 50,
        )
        register_layer(
            "test_client",
            "user",
            "user-aaa",
            SeedLayer(guidance_dir=overlay_dir),
        )
        result = resolve(org_id=10, user_id="user-aaa")
        source = collect_guidance_from_directories(result.guidance_dirs)
        assert source[slugify_key("v0 guide")]["content"].startswith("Overridden")

    # -- knowledge merge --

    def test_knowledge_deep_merge(self, monkeypatch):
        self._register(monkeypatch)
        register_layer(
            "test_client",
            "org",
            "10",
            SeedLayer(
                knowledge={
                    "Companies": {
                        "columns": {"name": "str"},
                        "seed_key": "name",
                        "rows": [{"name": "Acme"}],
                    },
                },
            ),
        )
        register_layer(
            "test_client",
            "user",
            "user-aaa",
            SeedLayer(
                knowledge={
                    "Companies": {
                        "columns": {"industry": "str"},
                        "rows": [{"name": "Acme", "industry": "Tech"}],
                    },
                },
            ),
        )
        result = resolve(org_id=10, user_id="user-aaa")
        tbl = result.knowledge["Companies"]
        assert "name" in tbl["columns"]
        assert "industry" in tbl["columns"]
        assert len(tbl["rows"]) == 1
        assert tbl["rows"][0]["industry"] == "Tech"

    # -- blacklist merge --

    def test_blacklist_overlay_wins_by_key(self, monkeypatch):
        self._register(monkeypatch)
        register_layer(
            "test_client",
            "org",
            "10",
            SeedLayer(
                blacklist_dir=_write_test_blacklist(
                    "org-layer",
                    medium="email",
                    contact_detail="spam@x",
                    reason="org",
                ),
            ),
        )
        register_layer(
            "test_client",
            "assistant",
            "99",
            SeedLayer(
                blacklist_dir=_write_test_blacklist(
                    "asst-layer",
                    medium="email",
                    contact_detail="spam@x",
                    reason="asst",
                ),
            ),
        )
        result = resolve(org_id=10, assistant_id=99)
        source = collect_blacklist_from_directories(result.blacklist_dirs)
        assert len(source) == 1
        assert source["email|spam@x"]["reason"] == "asst"

    # -- secrets merge --

    def test_secrets_layer_merged_with_spec(self, monkeypatch):
        from unity_deploy.assistant_deployments import deployment_types as dt

        spec = DeploymentSpec(
            name="v0",
            actor_config=ActorConfig(guidelines="G"),
            guidance_dir=_write_test_guidance(
                "code-key",
                title="g",
                content="c " + "x" * 50,
            ),
            secrets=[SecretEntry(name="CODE_KEY", value="from-spec", description="d")],
        )
        monkeypatch.setattr(dt, "load_deployment", lambda d, n: spec)

        mapping = DeploymentMapping(
            targets=[DeploymentTarget(scope="default", deployment="v0")],
        )
        register_client("sec_client", mapping, Path("/fake"))
        register_layer(
            "sec_client",
            "org",
            "10",
            SeedLayer(
                secrets=[
                    SecretEntry(name="ORG_KEY", value="org-val", description="org"),
                ],
            ),
        )
        result = resolve(org_id=10)
        names = {s.name for s in result.secrets}
        assert "CODE_KEY" in names
        assert "ORG_KEY" in names

    # -- scope order --

    def test_full_scope_order_org_team_user_assistant(self, monkeypatch):
        self._register(monkeypatch)
        register_layer(
            "test_client",
            "org",
            "10",
            SeedLayer(
                contacts=[
                    {"first_name": "Shared", "surname": "X", "level": "org"},
                ],
            ),
        )
        register_layer(
            "test_client",
            "team",
            "50",
            SeedLayer(
                contacts=[
                    {"first_name": "Shared", "surname": "X", "level": "team"},
                ],
            ),
        )
        register_layer(
            "test_client",
            "user",
            "user-aaa",
            SeedLayer(
                contacts=[
                    {"first_name": "Shared", "surname": "X", "level": "user"},
                ],
            ),
        )
        register_layer(
            "test_client",
            "assistant",
            "99",
            SeedLayer(
                contacts=[
                    {"first_name": "Shared", "surname": "X", "level": "asst"},
                ],
            ),
        )
        result = resolve(
            org_id=10,
            team_ids=[50],
            user_id="user-aaa",
            assistant_id=99,
        )
        assert len(result.contacts) == 1
        assert result.contacts[0]["level"] == "asst"

    # -- no layers = unchanged --

    def test_no_layers_returns_spec_data_only(self, monkeypatch):
        self._register(monkeypatch)
        result = resolve(org_id=10)
        assert result.config.guidelines == "Default v0"
        assert len(result.guidance_dirs) == 1
        assert result.contacts == []
