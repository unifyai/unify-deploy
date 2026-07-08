"""Symbolic tests for integration package validation."""

import json
import pytest
from pathlib import Path

from unify.guidance_manager.custom_guidance import GUIDANCE_JSONL_FILENAME


from unity_deploy.assistant_deployments.integrations.types import (
    Capability,
    DemoSiteConfig,
    IntegrationManifest,
    MCPServerConfig,
    SecretSchema,
)
from unity_deploy.assistant_deployments.integrations.validation import (
    validate_integration,
)


@pytest.fixture
def valid_package(tmp_path: Path) -> tuple[IntegrationManifest, Path]:
    """Create a fully valid integration package."""
    root = tmp_path / "test_pkg"
    root.mkdir()
    (root / "__init__.py").write_text("")

    functions_dir = root / "functions"
    functions_dir.mkdir()
    (functions_dir / "helpers.py").write_text(
        "async def do_something(): pass\n",
    )

    guidance_dir = root / "guidance"
    guidance_dir.mkdir()
    (guidance_dir / GUIDANCE_JSONL_FILENAME).write_text(
        json.dumps(
            {
                "key": "how_to",
                "title": "How To",
                "content": "Do the thing.",
            },
        )
        + "\n",
    )

    demo_dir = root / "demo_site"
    demo_dir.mkdir()
    (demo_dir / "server.js").write_text("// mock")

    manifest = IntegrationManifest(
        name="Test",
        slug="test_pkg",
        sector="test",
        tier="browser",
        description="Test integration",
        capabilities=[
            Capability(
                id="test_cap",
                name="Test Capability",
                description="Tests things",
                functions=["do_something"],
                guidance=["how_to"],
            ),
        ],
        demo_site=DemoSiteConfig(
            dir="demo_site",
            url_mapping="https://test.example.com",
        ),
    )

    return manifest, root


class TestValidateIntegration:
    def test_valid_package_no_errors(self, valid_package):
        manifest, root = valid_package
        errors = validate_integration(manifest, root)
        assert errors == []

    def test_missing_root_dir(self):
        manifest = IntegrationManifest(
            name="Test",
            slug="test",
            sector="test",
            tier="api",
            description="Test",
        )
        errors = validate_integration(manifest, Path("/nonexistent"))
        assert len(errors) == 1
        assert "does not exist" in errors[0]

    def test_slug_mismatch(self, valid_package):
        manifest, root = valid_package
        wrong_root = root.parent / "wrong_name"
        wrong_root.mkdir()
        (wrong_root / "__init__.py").write_text("")
        errors = validate_integration(manifest, wrong_root)
        assert any("does not match" in e for e in errors)

    def test_missing_init(self, tmp_path):
        root = tmp_path / "no_init"
        root.mkdir()

        manifest = IntegrationManifest(
            name="Test",
            slug="no_init",
            sector="test",
            tier="api",
            description="Test",
        )
        errors = validate_integration(manifest, root)
        assert any("__init__.py" in e for e in errors)

    def test_missing_functions_dir(self, tmp_path):
        root = tmp_path / "missing_fn"
        root.mkdir()
        (root / "__init__.py").write_text("")

        manifest = IntegrationManifest(
            name="Test",
            slug="missing_fn",
            sector="test",
            tier="api",
            description="Test",
            capabilities=[
                Capability(
                    id="test",
                    name="Test",
                    description="Test",
                    functions=["some_function"],
                ),
            ],
        )
        errors = validate_integration(manifest, root)
        assert any("functions/ dir is missing" in e for e in errors)

    def test_missing_guidance_file(self, tmp_path):
        root = tmp_path / "missing_guide"
        root.mkdir()
        (root / "__init__.py").write_text("")
        guidance_dir = root / "guidance"
        guidance_dir.mkdir()
        (guidance_dir / "guidance.jsonl").write_text(
            '{"key": "other", "title": "Other", "content": "x"}\n',
        )

        manifest = IntegrationManifest(
            name="Test",
            slug="missing_guide",
            sector="test",
            tier="api",
            description="Test",
            capabilities=[
                Capability(
                    id="test",
                    name="Test",
                    description="Test",
                    guidance=["nonexistent_guide"],
                ),
            ],
        )
        errors = validate_integration(manifest, root)
        assert any("nonexistent_guide" in e for e in errors)

    def test_missing_demo_site_dir(self, tmp_path):
        root = tmp_path / "missing_demo"
        root.mkdir()
        (root / "__init__.py").write_text("")

        manifest = IntegrationManifest(
            name="Test",
            slug="missing_demo",
            sector="test",
            tier="browser",
            description="Test",
            demo_site=DemoSiteConfig(
                dir="nonexistent_demo",
                url_mapping="https://example.com",
            ),
        )
        errors = validate_integration(manifest, root)
        assert any("nonexistent_demo" in e for e in errors)

    def test_mcp_secret_placeholder_validation(self, tmp_path):
        root = tmp_path / "mcp_test"
        root.mkdir()
        (root / "__init__.py").write_text("")

        manifest = IntegrationManifest(
            name="Test",
            slug="mcp_test",
            sector="test",
            tier="mcp",
            description="Test",
            secrets=[SecretSchema(name="DECLARED_KEY", description="The key")],
            mcp=MCPServerConfig(
                command="npx",
                env={"API_KEY": "${UNDECLARED_SECRET}"},
            ),
        )
        errors = validate_integration(manifest, root)
        assert any("UNDECLARED_SECRET" in e for e in errors)

    def test_mcp_declared_secret_no_error(self, tmp_path):
        root = tmp_path / "mcp_ok"
        root.mkdir()
        (root / "__init__.py").write_text("")

        manifest = IntegrationManifest(
            name="Test",
            slug="mcp_ok",
            sector="test",
            tier="mcp",
            description="Test",
            secrets=[SecretSchema(name="MY_KEY", description="The key")],
            mcp=MCPServerConfig(
                command="npx",
                env={"API_KEY": "${MY_KEY}"},
            ),
        )
        errors = validate_integration(manifest, root)
        mcp_errors = [e for e in errors if "secret" in e.lower() or "SECRET" in e]
        assert len(mcp_errors) == 0
