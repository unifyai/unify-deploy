"""Multi-deployment registration and resolution integration tests.

Tests the full pipeline: DeploymentSpec -> register_deployment() /
register_all_deployments() -> resolve().  Verifies multi-deployment
isolation, scoped registration across user/org/team/assistant, and
correct cascade behaviour when multiple deployments are live.

Uses the same registry snapshot/restore pattern as test_resolve.py
so each test starts with clean global state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from unity_deploy.customization.configs.types.actor_config import ActorConfig
from unity_deploy.customization.clients import (
    _ORG_CONFIGS,
    _ORG_ENVIRONMENTS,
    _ORG_FUNCTION_DIRS,
    _ORG_VENV_DIRS,
    _ORG_CONTACTS,
    _ORG_GUIDANCE,
    _ORG_KNOWLEDGE,
    _ORG_BLACKLIST,
    _ORG_SECRETS,
    _TEAM_CONFIGS,
    _TEAM_ENVIRONMENTS,
    _TEAM_FUNCTION_DIRS,
    _TEAM_VENV_DIRS,
    _TEAM_CONTACTS,
    _TEAM_GUIDANCE,
    _TEAM_KNOWLEDGE,
    _TEAM_BLACKLIST,
    _TEAM_SECRETS,
    _USER_CONFIGS,
    _USER_ENVIRONMENTS,
    _USER_FUNCTION_DIRS,
    _USER_VENV_DIRS,
    _USER_CONTACTS,
    _USER_GUIDANCE,
    _USER_KNOWLEDGE,
    _USER_BLACKLIST,
    _USER_SECRETS,
    _ASSISTANT_CONFIGS,
    _ASSISTANT_ENVIRONMENTS,
    _ASSISTANT_FUNCTION_DIRS,
    _ASSISTANT_VENV_DIRS,
    _ASSISTANT_CONTACTS,
    _ASSISTANT_GUIDANCE,
    _ASSISTANT_KNOWLEDGE,
    _ASSISTANT_BLACKLIST,
    _ASSISTANT_SECRETS,
    resolve,
)
from unity_deploy.customization.deployment_types import (
    DeploymentMapping,
    DeploymentSpec,
    DeploymentTarget,
    GuidanceEntry,
    SecretEntry,
    register_deployment,
    resolve_deployment_name,
)

# ---------------------------------------------------------------------------
# Registry cleanup fixture
# ---------------------------------------------------------------------------

_ALL_DICTS = [
    _ORG_CONFIGS,
    _ORG_ENVIRONMENTS,
    _ORG_FUNCTION_DIRS,
    _ORG_VENV_DIRS,
    _ORG_CONTACTS,
    _ORG_GUIDANCE,
    _ORG_KNOWLEDGE,
    _ORG_BLACKLIST,
    _ORG_SECRETS,
    _TEAM_CONFIGS,
    _TEAM_ENVIRONMENTS,
    _TEAM_FUNCTION_DIRS,
    _TEAM_VENV_DIRS,
    _TEAM_CONTACTS,
    _TEAM_GUIDANCE,
    _TEAM_KNOWLEDGE,
    _TEAM_BLACKLIST,
    _TEAM_SECRETS,
    _USER_CONFIGS,
    _USER_ENVIRONMENTS,
    _USER_FUNCTION_DIRS,
    _USER_VENV_DIRS,
    _USER_CONTACTS,
    _USER_GUIDANCE,
    _USER_KNOWLEDGE,
    _USER_BLACKLIST,
    _USER_SECRETS,
    _ASSISTANT_CONFIGS,
    _ASSISTANT_ENVIRONMENTS,
    _ASSISTANT_FUNCTION_DIRS,
    _ASSISTANT_VENV_DIRS,
    _ASSISTANT_CONTACTS,
    _ASSISTANT_GUIDANCE,
    _ASSISTANT_KNOWLEDGE,
    _ASSISTANT_BLACKLIST,
    _ASSISTANT_SECRETS,
]


@pytest.fixture(autouse=True)
def _clean_registry():
    """Snapshot and restore all 36 registry dicts so tests don't leak."""
    saved = [dict(d) for d in _ALL_DICTS]
    for d in _ALL_DICTS:
        d.clear()
    yield
    for d, s in zip(_ALL_DICTS, saved):
        d.clear()
        d.update(s)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(
    name: str,
    guideline_text: str,
    *,
    secrets: list[SecretEntry] | None = None,
    function_dir: Path | None = None,
) -> DeploymentSpec:
    return DeploymentSpec(
        name=name,
        actor_config=ActorConfig(guidelines=guideline_text),
        guidance=[
            GuidanceEntry(
                title=f"{name} guide",
                content=f"Guidance content for {name}. " + "x" * 50,
            ),
        ],
        secrets=secrets or [],
        function_dir=function_dir,
    )


_FAKE_DIR_A = Path("/tmp/test_funcs_a")
_FAKE_DIR_B = Path("/tmp/test_funcs_b")


# ═══════════════════════════════════════════════════════════════════════════
# TestRegisterDeployment — unit tests for register_deployment()
# ═══════════════════════════════════════════════════════════════════════════


class TestRegisterDeployment:

    def test_register_for_user(self):
        spec = _make_spec("v1", "User deployment")
        register_deployment(spec, user_id="user-aaa")
        r = resolve(user_id="user-aaa")
        assert r.config.guidelines == "User deployment"
        assert len(r.guidance) == 1
        assert r.guidance[0]["title"] == "v1 guide"

    def test_register_for_org(self):
        spec = _make_spec("v0", "Org deployment")
        register_deployment(spec, org_id=10)
        r = resolve(org_id=10)
        assert r.config.guidelines == "Org deployment"

    def test_register_for_team(self):
        spec = _make_spec("v2", "Team deployment")
        register_deployment(spec, team_id=42)
        r = resolve(team_ids=[42])
        assert r.config.guidelines == "Team deployment"

    def test_register_for_assistant(self):
        spec = _make_spec("v3", "Assistant deployment")
        register_deployment(spec, assistant_id=99)
        r = resolve(assistant_id=99)
        assert r.config.guidelines == "Assistant deployment"

    def test_function_dir_omitted_when_none(self):
        spec = _make_spec("light", "No functions")
        register_deployment(spec, user_id="user-bbb")
        r = resolve(user_id="user-bbb")
        assert r.function_dirs == []

    def test_function_dir_passed_through(self):
        spec = _make_spec("heavy", "With functions", function_dir=_FAKE_DIR_A)
        register_deployment(spec, user_id="user-ccc")
        r = resolve(user_id="user-ccc")
        assert r.function_dirs == [_FAKE_DIR_A]

    def test_secrets_passed_through(self):
        secrets = [
            SecretEntry(name="KEY_A", value="val-a", description="Secret A"),
            SecretEntry(name="KEY_B", value="val-b", description="Secret B"),
        ]
        spec = _make_spec("sec", "With secrets", secrets=secrets)
        register_deployment(spec, user_id="user-ddd")
        r = resolve(user_id="user-ddd")
        secret_names = {s["name"] for s in r.secrets}
        assert "KEY_A" in secret_names
        assert "KEY_B" in secret_names

    def test_assistant_priority_over_user(self):
        """When both assistant_id and user_id are passed, assistant wins."""
        spec = _make_spec("v1", "Ast takes priority")
        register_deployment(spec, assistant_id=5, user_id="user-eee")
        assert 5 in _ASSISTANT_CONFIGS
        assert "user-eee" not in _USER_CONFIGS


# ═══════════════════════════════════════════════════════════════════════════
# TestRegisterAllDeployments — needs real filesystem for load_deployment,
# so we mock load_deployment and test the orchestration logic
# ═══════════════════════════════════════════════════════════════════════════


class TestRegisterAllDeployments:

    def test_user_and_default_org_both_registered(self, monkeypatch):
        from unity_deploy.customization import deployment_types as dt

        v0 = _make_spec("v0", "Org default v0")
        v1 = _make_spec("v1", "User v1")

        def fake_load(deployments_dir, name):
            return {"v0": v0, "v1": v1}[name]

        monkeypatch.setattr(dt, "load_deployment", fake_load)

        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(scope="user", scope_id="user-aaa", deployment="v1"),
                DeploymentTarget(scope="default", deployment="v0"),
            ],
        )
        loaded = dt.register_all_deployments(
            mapping,
            Path("/fake"),
            default_org_id=10,
        )

        assert "v0" in loaded and "v1" in loaded

        r_user = resolve(org_id=10, user_id="user-aaa")
        assert "User v1" in r_user.config.guidelines

        r_org = resolve(org_id=10, user_id="user-unknown")
        assert r_org.config.guidelines == "Org default v0"

    def test_team_target_registered(self, monkeypatch):
        from unity_deploy.customization import deployment_types as dt

        spec = _make_spec("team-deploy", "Team 42 deployment")

        monkeypatch.setattr(dt, "load_deployment", lambda d, n: spec)

        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(scope="team", scope_id="42", deployment="team-deploy"),
            ],
        )
        dt.register_all_deployments(mapping, Path("/fake"))

        r = resolve(team_ids=[42])
        assert r.config.guidelines == "Team 42 deployment"

    def test_assistant_target_registered(self, monkeypatch):
        from unity_deploy.customization import deployment_types as dt

        spec = _make_spec("ast-deploy", "Assistant 99 deployment")

        monkeypatch.setattr(dt, "load_deployment", lambda d, n: spec)

        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(
                    scope="assistant",
                    scope_id="99",
                    deployment="ast-deploy",
                ),
            ],
        )
        dt.register_all_deployments(mapping, Path("/fake"))

        r = resolve(assistant_id=99)
        assert r.config.guidelines == "Assistant 99 deployment"

    def test_default_skipped_without_ids(self, monkeypatch):
        from unity_deploy.customization import deployment_types as dt

        spec = _make_spec("v0", "Default")

        monkeypatch.setattr(dt, "load_deployment", lambda d, n: spec)

        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(scope="default", deployment="v0"),
            ],
        )
        dt.register_all_deployments(mapping, Path("/fake"))

        r = resolve()
        assert r.config == ActorConfig()

    def test_loaded_specs_returned(self, monkeypatch):
        from unity_deploy.customization import deployment_types as dt

        v0 = _make_spec("v0", "Zero")
        v1 = _make_spec("v1", "One")

        monkeypatch.setattr(dt, "load_deployment", lambda d, n: {"v0": v0, "v1": v1}[n])

        mapping = DeploymentMapping(
            targets=[
                DeploymentTarget(scope="user", scope_id="u1", deployment="v1"),
                DeploymentTarget(scope="default", deployment="v0"),
            ],
        )
        loaded = dt.register_all_deployments(
            mapping,
            Path("/fake"),
            default_org_id=1,
        )

        assert set(loaded.keys()) == {"v0", "v1"}
        assert loaded["v0"].name == "v0"
        assert loaded["v1"].name == "v1"


# ═══════════════════════════════════════════════════════════════════════════
# TestMultiDeploymentIsolation — the critical parallel deployment tests
# ═══════════════════════════════════════════════════════════════════════════


class TestMultiDeploymentIsolation:

    def test_user_gets_own_deployment_not_org_default(self):
        v0 = _make_spec("v0", "Org default v0")
        v1 = _make_spec("v1", "User A v1")

        register_deployment(v0, org_id=10)
        register_deployment(v1, user_id="user-A")

        r_a = resolve(org_id=10, user_id="user-A")
        assert "User A v1" in r_a.config.guidelines

        r_b = resolve(org_id=10, user_id="user-B")
        assert r_b.config.guidelines == "Org default v0"

    def test_two_users_different_deployments(self):
        v0 = _make_spec("v0", "Org default")
        v1 = _make_spec("v1", "User A custom")
        v2 = _make_spec("v2", "User B custom")

        register_deployment(v0, org_id=10)
        register_deployment(v1, user_id="user-A")
        register_deployment(v2, user_id="user-B")

        r_a = resolve(org_id=10, user_id="user-A")
        assert "User A custom" in r_a.config.guidelines

        r_b = resolve(org_id=10, user_id="user-B")
        assert "User B custom" in r_b.config.guidelines

        r_c = resolve(org_id=10, user_id="user-C")
        assert r_c.config.guidelines == "Org default"

    def test_team_deployment_does_not_leak_to_other_teams(self):
        dep_x = _make_spec("deploy-x", "Team 10 deployment")
        dep_y = _make_spec("deploy-y", "Team 20 deployment")

        register_deployment(dep_x, team_id=10)
        register_deployment(dep_y, team_id=20)

        r_10 = resolve(team_ids=[10])
        assert r_10.config.guidelines == "Team 10 deployment"

        r_20 = resolve(team_ids=[20])
        assert r_20.config.guidelines == "Team 20 deployment"

        r_none = resolve(team_ids=[99])
        assert r_none.config == ActorConfig()

    def test_assistant_overrides_user_deployment(self):
        v1 = _make_spec("v1", "User config")
        v2 = _make_spec("v2", "Assistant config")

        register_deployment(v1, user_id="user-A")
        register_deployment(v2, assistant_id=55)

        r = resolve(user_id="user-A", assistant_id=55)
        assert "Assistant config" in r.config.guidelines
        assert "User config" in r.config.guidelines
        assert len(r.guidance) == 2

    def test_function_dirs_isolated_per_scope(self):
        v0 = _make_spec("v0", "Org", function_dir=_FAKE_DIR_A)
        v1 = _make_spec("v1", "User", function_dir=_FAKE_DIR_B)

        register_deployment(v0, org_id=10)
        register_deployment(v1, user_id="user-A")

        r_a = resolve(org_id=10, user_id="user-A")
        assert r_a.function_dirs == [_FAKE_DIR_A, _FAKE_DIR_B]

        r_b = resolve(org_id=10, user_id="user-B")
        assert r_b.function_dirs == [_FAKE_DIR_A]

    def test_secrets_isolated_per_scope(self):
        org_secrets = [
            SecretEntry(name="ORG_KEY", value="org-val", description="Org secret"),
        ]
        user_secrets = [
            SecretEntry(name="USR_KEY", value="usr-val", description="User secret"),
        ]

        v0 = _make_spec("v0", "Org", secrets=org_secrets)
        v1 = _make_spec("v1", "User", secrets=user_secrets)

        register_deployment(v0, org_id=10)
        register_deployment(v1, user_id="user-A")

        r_a = resolve(org_id=10, user_id="user-A")
        a_names = {s["name"] for s in r_a.secrets}
        assert "ORG_KEY" in a_names
        assert "USR_KEY" in a_names

        r_b = resolve(org_id=10, user_id="user-B")
        b_names = {s["name"] for s in r_b.secrets}
        assert "ORG_KEY" in b_names
        assert "USR_KEY" not in b_names

    def test_guidance_isolated_per_scope(self):
        v0 = _make_spec("v0", "Org")
        v1 = _make_spec("v1", "User")

        register_deployment(v0, org_id=10)
        register_deployment(v1, user_id="user-A")

        r_a = resolve(org_id=10, user_id="user-A")
        a_titles = {g["title"] for g in r_a.guidance}
        assert "v0 guide" in a_titles
        assert "v1 guide" in a_titles

        r_b = resolve(org_id=10, user_id="user-B")
        b_titles = {g["title"] for g in r_b.guidance}
        assert "v0 guide" in b_titles
        assert "v1 guide" not in b_titles

    def test_org_team_user_assistant_full_cascade(self):
        """All four scopes registered — most specific wins on config."""
        spec_org = _make_spec("org-dep", "Org level")
        spec_team = _make_spec("team-dep", "Team level")
        spec_user = _make_spec("user-dep", "User level")
        spec_ast = _make_spec("ast-dep", "Assistant level")

        register_deployment(spec_org, org_id=1)
        register_deployment(spec_team, team_id=10)
        register_deployment(spec_user, user_id="user-X")
        register_deployment(spec_ast, assistant_id=77)

        r = resolve(org_id=1, team_ids=[10], user_id="user-X", assistant_id=77)
        assert "Assistant level" in r.config.guidelines
        assert len(r.guidance) == 4


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
