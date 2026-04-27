"""Symbolic tests for demo site aggregation."""

import pytest
from pathlib import Path

import yaml

from unity_deploy.customization.integrations.aggregate_demo_sites import (
    aggregate_demo_sites,
)


@pytest.fixture
def integration_with_demo(tmp_path: Path) -> Path:
    """Create an integration package with a demo site."""
    search_path = tmp_path / "integrations"
    search_path.mkdir()

    pkg = search_path / "test_housing"
    pkg.mkdir()
    (pkg / "manifest.yaml").write_text(
        yaml.dump(
            {
                "name": "Test Housing",
                "slug": "test_housing",
                "sector": "housing",
                "tier": "browser",
                "description": "Test",
                "demo_site": {
                    "dir": "demo_site",
                    "url_mapping": "https://test.example.com",
                },
            },
        ),
    )

    demo = pkg / "demo_site"
    demo.mkdir()
    (demo / "server.js").write_text("// mock server")
    (demo / "index.html").write_text("<html>Mock</html>")

    return search_path


@pytest.fixture
def integration_without_demo(tmp_path: Path) -> Path:
    """Create an integration package without a demo site."""
    search_path = tmp_path / "integrations2"
    search_path.mkdir()

    pkg = search_path / "test_api"
    pkg.mkdir()
    (pkg / "manifest.yaml").write_text(
        yaml.dump(
            {
                "name": "Test API",
                "slug": "test_api",
                "sector": "test",
                "tier": "api",
                "description": "No demo site",
            },
        ),
    )

    return search_path


class TestAggregateDemoSites:
    def test_copies_demo_site(self, integration_with_demo, tmp_path):
        target = tmp_path / "demo-sites"
        result = aggregate_demo_sites([integration_with_demo], target)

        assert "test_housing" in result
        assert (target / "test_housing" / "server.js").is_file()
        assert (target / "test_housing" / "index.html").is_file()

    def test_skips_integration_without_demo(self, integration_without_demo, tmp_path):
        target = tmp_path / "demo-sites"
        result = aggregate_demo_sites([integration_without_demo], target)
        assert result == {}

    def test_creates_target_dir(self, integration_with_demo, tmp_path):
        target = tmp_path / "new" / "nested" / "demo-sites"
        result = aggregate_demo_sites([integration_with_demo], target)
        assert target.is_dir()
        assert len(result) == 1

    def test_replaces_existing(self, integration_with_demo, tmp_path):
        target = tmp_path / "demo-sites"
        target.mkdir()
        existing = target / "test_housing"
        existing.mkdir()
        (existing / "old_file.txt").write_text("old content")

        aggregate_demo_sites([integration_with_demo], target)

        assert not (target / "test_housing" / "old_file.txt").exists()
        assert (target / "test_housing" / "server.js").is_file()

    def test_multiple_search_paths(
        self,
        integration_with_demo,
        integration_without_demo,
        tmp_path,
    ):
        target = tmp_path / "demo-sites"
        result = aggregate_demo_sites(
            [integration_with_demo, integration_without_demo],
            target,
        )
        assert len(result) == 1
        assert "test_housing" in result

    def test_nonexistent_search_path(self, tmp_path):
        target = tmp_path / "demo-sites"
        result = aggregate_demo_sites([Path("/nonexistent")], target)
        assert result == {}
