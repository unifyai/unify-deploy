"""Integration manifest schema and quality tiers.

Every integration package must include a ``manifest.yaml`` that conforms
to :class:`IntegrationManifest`.  The manifest is the single source of
truth for declarative metadata — what the integration provides, what it
requires, and how it connects to external systems.

Inspired by Home Assistant's ``manifest.json`` and OpenClaw's skill
frontmatter.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class QualityTier(str, Enum):
    """Maturity classification for integration packages.

    Higher tiers unlock more features in the catalog UI and landing pages.
    """

    bronze = "bronze"
    silver = "silver"
    gold = "gold"
    platinum = "platinum"


class SecretSchema(BaseModel):
    """Declares a secret an integration requires at runtime.

    The actual secret *value* is never stored in the manifest — only the
    schema.  Values come from ``SecretManager`` / ``.secrets.json``.
    """

    name: str = Field(
        ...,
        description="Environment-style key, e.g. 'NEC_HOUSING_USERNAME'.",
    )
    description: str = Field(
        ...,
        description="Human-readable purpose shown in the secrets UI.",
    )
    required: bool = Field(
        default=True,
        description="Whether the integration fails without this secret.",
    )
    sensitive: bool = Field(
        default=True,
        description="If True the value is masked in any UI display.",
    )


class Capability(BaseModel):
    """A discrete unit of functionality an integration exposes.

    Capabilities map to function + guidance combinations that the Actor
    can discover and use.
    """

    id: str = Field(
        ...,
        description="Machine-readable identifier, e.g. 'log_repair'.",
    )
    name: str = Field(
        ...,
        description="Human-readable capability name.",
    )
    description: str = Field(
        ...,
        description="What this capability does.",
    )
    functions: list[str] = Field(
        default_factory=list,
        description="Function names in the integration's functions/ directory.",
    )
    guidance: list[str] = Field(
        default_factory=list,
        description="Guidance file stems in the integration's guidance/ directory.",
    )
    tier: Literal["mcp", "api", "browser"] | None = Field(
        default=None,
        description="Per-capability tier override (inherits from manifest if None).",
    )


class MCPServerConfig(BaseModel):
    """Configuration for launching an MCP server subprocess.

    Environment variables support ``${SECRET_NAME}`` placeholders that are
    resolved via SecretManager at startup.
    """

    command: str = Field(
        ...,
        description="Executable to run, e.g. 'npx' or 'uvx'.",
    )
    args: list[str] = Field(
        default_factory=list,
        description="Command-line arguments.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables; supports ${SECRET_NAME} placeholders.",
    )
    transport: Literal["stdio", "sse"] = Field(
        default="stdio",
        description="MCP transport protocol.",
    )
    tool_filter: list[str] | None = Field(
        default=None,
        description="Whitelist of tool names to expose (None = all).",
    )


class DemoSiteConfig(BaseModel):
    """Configuration for a mock demo site bundled with the integration."""

    dir: str = Field(
        ...,
        description="Subdirectory name within the integration package containing the demo site.",
    )
    url_mapping: str = Field(
        ...,
        description="Real URL to intercept via Playwright context.route().",
    )


class IntegrationManifest(BaseModel):
    """Declarative metadata for an integration package.

    Loaded from ``manifest.yaml`` in each integration directory.
    Drives discovery, validation, UI generation, and content pipelines.
    """

    name: str = Field(
        ...,
        description="Human-readable integration name.",
    )
    slug: str = Field(
        ...,
        description="Machine-readable identifier; must match the directory name.",
    )
    version: str = Field(
        default="0.1.0",
        description="Semantic version of this integration.",
    )
    sector: str = Field(
        ...,
        description="Industry vertical, e.g. 'social-housing', 'fleet-management'.",
    )
    tier: Literal["mcp", "api", "browser"] = Field(
        ...,
        description="Primary connector tier.",
    )
    quality: QualityTier = Field(
        default=QualityTier.bronze,
        description="Maturity classification.",
    )
    description: str = Field(
        ...,
        description="Multi-sentence description of the integration.",
    )
    homepage: str | None = Field(
        default=None,
        description="URL to the external platform's homepage.",
    )

    secrets: list[SecretSchema] = Field(
        default_factory=list,
        description="Secrets this integration requires.",
    )
    capabilities: list[Capability] = Field(
        default_factory=list,
        description="Discrete capabilities the integration exposes.",
    )
    mcp: MCPServerConfig | None = Field(
        default=None,
        description="MCP server configuration (Tier 1 integrations).",
    )
    demo_site: DemoSiteConfig | None = Field(
        default=None,
        description="Demo site mock configuration (typically Tier 3).",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Freeform tags for search and filtering.",
    )

    requirements: list[str] = Field(
        default_factory=list,
        description="pip packages needed at runtime (like HA's requirements).",
    )
    venv_name: str | None = Field(
        default=None,
        description="Custom venv for isolated dependencies.",
    )

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, v: str) -> str:
        if not v.replace("_", "").replace("-", "").isalnum():
            raise ValueError(
                f"slug must be alphanumeric with hyphens/underscores, got '{v}'",
            )
        return v

    @field_validator("mcp")
    @classmethod
    def _mcp_requires_tier(
        cls,
        v: MCPServerConfig | None,
        info,
    ) -> MCPServerConfig | None:
        if v is not None and info.data.get("tier") != "mcp":
            raise ValueError("mcp config is only valid when tier='mcp'")
        return v
