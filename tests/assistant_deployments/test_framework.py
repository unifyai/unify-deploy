"""Tests for assistant deployment framework infrastructure.

Covers ActorConfig model behavior, environment reconstruction helpers,
secrets file merge order, custom function collection, hash behavior, and
the integration registry sync helper.
"""

from __future__ import annotations

import json

import pytest

from unity_deploy.assistant_deployments.configs.types.actor_config import ActorConfig
from unity_deploy.assistant_deployments.environments.reconstruct import (
    parse_env_path,
    write_files_to_package,
    import_and_resolve,
)
from unify.integration_registry.custom_integration_registry import (
    collect_integration_registry_from_rows,
    compute_custom_integration_registry_hash,
)
from unity_deploy.assistant_deployments.secrets_file import load_secrets

# ---------------------------------------------------------------------------
# 1. ActorConfig model
# ---------------------------------------------------------------------------


class TestActorConfig:
    def test_default_all_none(self):
        cfg = ActorConfig()
        assert cfg.can_compose is None
        assert cfg.model is None
        assert cfg.guidelines is None

    def test_to_post_json_excludes_none(self):
        cfg = ActorConfig(model="m")
        d = cfg.to_post_json()
        assert d == {"model": "m"}

    def test_to_post_json_empty_for_default(self):
        assert ActorConfig().to_post_json() == {}

    def test_all_fields_round_trip(self):
        cfg = ActorConfig(
            can_compose=True,
            can_store=False,
            timeout=60.0,
            model="m",
            prompt_caching=["system"],
            guidelines="G",
        )
        d = cfg.to_post_json()
        assert ActorConfig(**d) == cfg


# ---------------------------------------------------------------------------
# 2. Environment reconstruction helpers
# ---------------------------------------------------------------------------


class TestEnvironmentReconstruct:
    def test_parse_env_path_valid(self):
        mod, attr = parse_env_path("my_module:my_attr")
        assert mod == "my_module"
        assert attr == "my_attr"

    def test_parse_env_path_invalid_no_colon(self):
        with pytest.raises(ValueError, match="must be"):
            parse_env_path("no_colon_here")

    def test_parse_env_path_empty_parts(self):
        with pytest.raises(ValueError, match="non-empty"):
            parse_env_path(":attr")

    def test_write_files_and_import(self, tmp_path):
        pkg_dir = write_files_to_package(
            environment_id=999,
            files={"hello.py": "GREETING = 'world'"},
            root=tmp_path,
        )
        assert (pkg_dir / "hello.py").exists()

        result = import_and_resolve(
            pkg_dir=pkg_dir,
            module_name="hello",
            attr_name="GREETING",
        )
        assert result == "world"

    def test_write_files_only_rewrites_on_change(self, tmp_path):
        files = {"test.py": "X = 1"}
        pkg_dir = write_files_to_package(
            environment_id=1,
            files=files,
            root=tmp_path,
        )
        mtime1 = (pkg_dir / "test.py").stat().st_mtime_ns
        write_files_to_package(
            environment_id=1,
            files=files,
            root=tmp_path,
        )
        mtime2 = (pkg_dir / "test.py").stat().st_mtime_ns
        assert mtime1 == mtime2


# ---------------------------------------------------------------------------
# 3. Integration registry sync with empty data
# ---------------------------------------------------------------------------


class TestIntegrationRegistrySync:
    def test_noop_with_empty_registry(self):
        from unify.integration_registry import sync_custom_integration_registry

        result = sync_custom_integration_registry(source_registry={})
        assert result is False


# ---------------------------------------------------------------------------
# 4. Secrets file: merge order (org → team → user → assistant)
# ---------------------------------------------------------------------------


class TestSecretsAssistantLevel:
    def test_assistant_overrides_user_and_org(self, tmp_path):
        f = tmp_path / ".secrets.json"
        f.write_text(
            json.dumps(
                {
                    "org": {"1": {"K": {"value": "org", "description": "d"}}},
                    "user": {"u1": {"K": {"value": "user", "description": "d"}}},
                    "assistant": {"10": {"K": {"value": "asst", "description": "d"}}},
                },
            ),
        )
        result = load_secrets(org_id=1, user_id="u1", assistant_id=10, path=f)
        assert len(result) == 1
        assert result[0]["value"] == "asst"

    def test_team_secrets_override_org(self, tmp_path):
        f = tmp_path / ".secrets.json"
        f.write_text(
            json.dumps(
                {
                    "org": {"1": {"K": {"value": "org", "description": "d"}}},
                    "team": {"100": {"K": {"value": "team", "description": "d"}}},
                },
            ),
        )
        result = load_secrets(org_id=1, team_ids=[100], path=f)
        assert len(result) == 1
        assert result[0]["value"] == "team"

    def test_user_overrides_team_secrets(self, tmp_path):
        f = tmp_path / ".secrets.json"
        f.write_text(
            json.dumps(
                {
                    "team": {"100": {"K": {"value": "team", "description": "d"}}},
                    "user": {"u1": {"K": {"value": "user", "description": "d"}}},
                },
            ),
        )
        result = load_secrets(team_ids=[100], user_id="u1", path=f)
        assert len(result) == 1
        assert result[0]["value"] == "user"

    def test_multi_team_secrets_higher_id_wins(self, tmp_path):
        f = tmp_path / ".secrets.json"
        f.write_text(
            json.dumps(
                {
                    "team": {
                        "100": {"K": {"value": "t100", "description": "d"}},
                        "200": {"K": {"value": "t200", "description": "d"}},
                    },
                },
            ),
        )
        result = load_secrets(team_ids=[200, 100], path=f)
        assert len(result) == 1
        assert result[0]["value"] == "t200"

    def test_secrets_merge_org_team_user_assistant(self, tmp_path):
        f = tmp_path / ".secrets.json"
        f.write_text(
            json.dumps(
                {
                    "org": {
                        "1": {
                            "SHARED": {"value": "org", "description": "d"},
                            "ORG_ONLY": {"value": "org_v", "description": "d"},
                        },
                    },
                    "team": {
                        "100": {
                            "SHARED": {"value": "team", "description": "d"},
                            "TEAM_ONLY": {"value": "team_v", "description": "d"},
                        },
                    },
                    "user": {
                        "u1": {
                            "SHARED": {"value": "user", "description": "d"},
                        },
                    },
                    "assistant": {
                        "10": {
                            "SHARED": {"value": "asst", "description": "d"},
                        },
                    },
                },
            ),
        )
        result = load_secrets(
            org_id=1,
            team_ids=[100],
            user_id="u1",
            assistant_id=10,
            path=f,
        )
        by_name = {s["name"]: s["value"] for s in result}
        assert by_name["SHARED"] == "asst"
        assert by_name["ORG_ONLY"] == "org_v"
        assert by_name["TEAM_ONLY"] == "team_v"

    def test_empty_team_ids_skips_team_level(self, tmp_path):
        f = tmp_path / ".secrets.json"
        f.write_text(
            json.dumps(
                {
                    "org": {"1": {"K": {"value": "org", "description": "d"}}},
                    "team": {"100": {"K": {"value": "team", "description": "d"}}},
                },
            ),
        )
        result = load_secrets(org_id=1, team_ids=[], path=f)
        assert result[0]["value"] == "org"


# ---------------------------------------------------------------------------
# 5. Custom function collection from directories
# ---------------------------------------------------------------------------


class TestCustomFunctionCollection:
    def test_collect_from_empty_dir(self, tmp_path):
        from unify.function_manager.custom_functions import (
            collect_custom_functions,
        )

        fn_dir = tmp_path / "functions"
        fn_dir.mkdir()
        result = collect_custom_functions(directory=fn_dir)
        assert result == {}

    def test_collect_ignores_underscore_prefixed_files(self, tmp_path):
        from unify.function_manager.custom_functions import (
            collect_custom_functions,
        )

        fn_dir = tmp_path / "functions"
        fn_dir.mkdir()
        (fn_dir / "_private.py").write_text(
            "from unify.function_manager.custom import custom_function\n"
            "@custom_function()\n"
            "async def hidden() -> int:\n"
            "    return 1\n",
        )
        result = collect_custom_functions(directory=fn_dir)
        assert "hidden" not in result


# ---------------------------------------------------------------------------
# 6. Hash behavior edge cases
# ---------------------------------------------------------------------------


class TestHashEdgeCases:
    def test_registry_hash_with_json_fields(self):
        rows = collect_integration_registry_from_rows(
            [
                {
                    "slug": "github",
                    "label": "GitHub",
                    "tags_json": '["git"]',
                },
            ],
        )
        assert rows["github"]["custom_hash"]
        assert len(rows["github"]["custom_hash"]) == 16

    def test_aggregate_registry_hash_single_record(self):
        rows = collect_integration_registry_from_rows(
            [{"slug": "only", "label": "Only"}],
        )
        h = compute_custom_integration_registry_hash(source_registry=rows)
        assert len(h) == 16

    def test_registry_hash_changes_when_value_changes(self):
        base = collect_integration_registry_from_rows(
            [{"slug": "app", "label": "A", "description": "one"}],
        )
        changed = collect_integration_registry_from_rows(
            [{"slug": "app", "label": "A", "description": "two"}],
        )
        assert base["app"]["custom_hash"] != changed["app"]["custom_hash"]
