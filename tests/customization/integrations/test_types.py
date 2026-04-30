"""Symbolic tests for integration manifest types and validation.

Tests the Pydantic models, field validators, and quality tiers.
"""

import pytest

from unity_deploy.customization.integrations.types import (
    Capability,
    DemoSiteConfig,
    IntegrationManifest,
    MCPServerConfig,
    QualityTier,
    SecretSchema,
)


class TestSecretSchema:
    def test_basic_creation(self):
        s = SecretSchema(name="API_KEY", description="API authentication key")
        assert s.name == "API_KEY"
        assert s.required is True
        assert s.sensitive is True

    def test_optional_fields(self):
        s = SecretSchema(
            name="ACCOUNT_ID",
            description="Account ID",
            required=False,
            sensitive=False,
        )
        assert s.required is False
        assert s.sensitive is False


class TestCapability:
    def test_basic_creation(self):
        cap = Capability(
            id="log_repair",
            name="Log Repair",
            description="Log a repair request",
        )
        assert cap.id == "log_repair"
        assert cap.functions == []
        assert cap.guidance == []
        assert cap.tier is None

    def test_with_functions_and_guidance(self):
        cap = Capability(
            id="log_repair",
            name="Log Repair",
            description="Log a repair",
            functions=["create_repair", "validate_category"],
            guidance=["repair_logging"],
            tier="browser",
        )
        assert len(cap.functions) == 2
        assert cap.tier == "browser"


class TestMCPServerConfig:
    def test_basic(self):
        cfg = MCPServerConfig(command="npx", args=["-y", "@salesforce/mcp-server"])
        assert cfg.command == "npx"
        assert cfg.transport == "stdio"
        assert cfg.tool_filter is None

    def test_with_env_placeholders(self):
        cfg = MCPServerConfig(
            command="uvx",
            env={"API_KEY": "${MY_SECRET}"},
            transport="sse",
            tool_filter=["search", "create"],
        )
        assert "${MY_SECRET}" in cfg.env["API_KEY"]
        assert len(cfg.tool_filter) == 2


class TestDemoSiteConfig:
    def test_basic(self):
        cfg = DemoSiteConfig(dir="demo_site", url_mapping="https://example.com")
        assert cfg.dir == "demo_site"


class TestQualityTier:
    def test_values(self):
        assert QualityTier.bronze.value == "bronze"
        assert QualityTier.platinum.value == "platinum"

    def test_ordering(self):
        tiers = [
            QualityTier.gold,
            QualityTier.bronze,
            QualityTier.platinum,
            QualityTier.silver,
        ]
        sorted_tiers = sorted(tiers, key=lambda t: t.value)
        assert sorted_tiers[0] == QualityTier.bronze


class TestIntegrationManifest:
    def test_minimal_manifest(self):
        m = IntegrationManifest(
            name="Test",
            slug="test",
            sector="test-sector",
            tier="api",
            description="A test integration",
        )
        assert m.slug == "test"
        assert m.quality == QualityTier.bronze
        assert m.version == "0.1.0"
        assert m.secrets == []
        assert m.capabilities == []

    def test_full_manifest(self):
        m = IntegrationManifest(
            name="NEC Housing",
            slug="nec_housing",
            sector="social-housing",
            tier="browser",
            quality=QualityTier.gold,
            description="NEC Housing integration",
            homepage="https://example.com",
            secrets=[SecretSchema(name="USER", description="Username")],
            capabilities=[
                Capability(id="repair", name="Repair", description="Log repairs"),
            ],
            demo_site=DemoSiteConfig(
                dir="demo_site",
                url_mapping="https://nec.example.com",
            ),
            tags=["housing"],
            requirements=["httpx"],
            venv_name="nec",
        )
        assert len(m.secrets) == 1
        assert m.demo_site is not None
        assert m.mcp is None

    def test_slug_validation_valid(self):
        m = IntegrationManifest(
            name="Test",
            slug="my-integration_v2",
            sector="test",
            tier="api",
            description="Test",
        )
        assert m.slug == "my-integration_v2"

    def test_slug_validation_invalid(self):
        with pytest.raises(ValueError, match="alphanumeric"):
            IntegrationManifest(
                name="Test",
                slug="bad slug!",
                sector="test",
                tier="api",
                description="Test",
            )

    def test_mcp_requires_mcp_tier(self):
        with pytest.raises(ValueError, match="tier='mcp'"):
            IntegrationManifest(
                name="Test",
                slug="test",
                sector="test",
                tier="api",
                description="Test",
                mcp=MCPServerConfig(command="npx"),
            )

    def test_mcp_allowed_on_mcp_tier(self):
        m = IntegrationManifest(
            name="Test",
            slug="test",
            sector="test",
            tier="mcp",
            description="Test",
            mcp=MCPServerConfig(command="npx", args=["-y", "server"]),
        )
        assert m.mcp is not None
