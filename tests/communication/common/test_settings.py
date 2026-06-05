"""Settings derivation rules that downstream services depend on.

The Comms App, Adapters, and the AssistantSession controller all read
``SETTINGS.image_hash_blob`` to decide which Unity image hash to pull from
GCS.  These tests pin the naming convention so a typo there cannot
silently route preview traffic back onto the canonical staging image.
"""

from __future__ import annotations

from common.settings import _image_hash_blob_name


class TestImageHashBlobName:
    def test_production_uses_unsuffixed_blob(self):
        assert (
            _image_hash_blob_name(
                deploy_env="production",
                env_suffix="",
                branch_tag="",
            )
            == "image_hash.txt"
        )

    def test_staging_uses_environment_suffixed_blob(self):
        assert (
            _image_hash_blob_name(
                deploy_env="staging",
                env_suffix="-staging",
                branch_tag="",
            )
            == "image_hash_staging.txt"
        )

    def test_branch_tag_appends_slug_to_staging_blob(self):
        assert (
            _image_hash_blob_name(
                deploy_env="staging",
                env_suffix="-staging",
                branch_tag="myslug",
            )
            == "image_hash_staging_myslug.txt"
        )

    def test_branch_tag_on_production_appends_slug_to_base_blob(self):
        assert (
            _image_hash_blob_name(
                deploy_env="production",
                env_suffix="",
                branch_tag="myslug",
            )
            == "image_hash_myslug.txt"
        )
