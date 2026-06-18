"""Settings derivation rules that downstream services depend on.

The Comms App, Adapters, and the AssistantSession controller all read
``SETTINGS.image_hash_blob`` to decide which Droid image hash to pull from
GCS.  These tests pin the naming convention so a typo there cannot
silently route staging traffic onto the production image (or vice versa).
"""

from __future__ import annotations

from common.settings import _get_deploy_env, _image_hash_blob_name


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
