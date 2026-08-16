"""Settings derivation rules that downstream services depend on.

The Comms App, Adapters, and the AssistantSession controller all read
``SETTINGS.image_hash_blob`` to decide which Unity image hash to pull from
GCS.  These tests pin the naming convention so a typo there cannot
silently route staging traffic onto the production image (or vice versa).
"""

from __future__ import annotations

import pytest

from common.settings import SETTINGS, _get_deploy_env, _image_hash_blob_name


class TestSettingsFollowTheEnvironment:
    """``SETTINGS`` is imported once and read for the life of the process, so
    every value has to reflect the environment at the moment it is read.

    Held in instance state instead, configuration froze as the environment
    stood when whichever module imported ``common.settings`` first ran -- and
    under pytest that is alphabetical collection order, which no caller
    chooses. Ten adapter assertions comparing topic names against a suffix
    fixed by an unrelated module is what that cost.
    """

    def test_deploy_env_follows_a_change_made_after_import(self, monkeypatch):
        monkeypatch.setenv("DEPLOY_ENV", "staging")
        assert SETTINGS.deploy_env == "staging"
        monkeypatch.setenv("DEPLOY_ENV", "production")
        assert SETTINGS.deploy_env == "production"

    def test_derived_names_follow_the_environment(self, monkeypatch):
        """The suffix reaches names built from it, not just ``deploy_env``."""
        monkeypatch.setenv("DEPLOY_ENV", "staging")
        assert SETTINGS.env_suffix == "-staging"
        assert SETTINGS.org_topic(11) == "unity-org-11-staging"
        assert SETTINGS.assistant_topic("25") == "unity-25-staging"

        monkeypatch.delenv("DEPLOY_ENV", raising=False)
        assert SETTINGS.env_suffix == ""
        assert SETTINGS.org_topic(11) == "unity-org-11"
        assert SETTINGS.assistant_topic("25") == "unity-25"

    def test_plain_values_follow_the_environment(self, monkeypatch):
        monkeypatch.setenv("GCP_PROJECT_ID", "some-other-project")
        assert SETTINGS.gcp_project_id == "some-other-project"
        assert SETTINGS.image_registry.startswith(
            "us-central1-docker.pkg.dev/some-other-project/",
        )

    def test_settings_hold_no_environment_derived_state(self):
        """Assigning a setting must fail rather than half-work.

        A writable attribute would shadow the property for the rest of the
        process, which is the same bug in a smaller window: tests set the
        variable instead.
        """
        with pytest.raises(AttributeError):
            SETTINGS.deploy_env = "staging"


class TestImageHashBlobName:
    def test_production_uses_unsuffixed_blob(self):
        assert _image_hash_blob_name(deploy_env="production") == "image_hash.txt"

    def test_staging_uses_environment_suffixed_blob(self):
        assert _image_hash_blob_name(deploy_env="staging") == "image_hash_staging.txt"


class TestGetDeployEnv:
    def test_deploy_env_staging(self, monkeypatch):
        monkeypatch.setenv("DEPLOY_ENV", "staging")
        assert _get_deploy_env() == "staging"

    def test_deploy_env_defaults_to_production(self, monkeypatch):
        monkeypatch.delenv("DEPLOY_ENV", raising=False)
        assert _get_deploy_env() == "production"

    def test_legacy_staging_flag_is_ignored(self, monkeypatch):
        monkeypatch.delenv("DEPLOY_ENV", raising=False)
        monkeypatch.setenv("STAGING", "true")
        assert _get_deploy_env() == "production"
