"""Private integration framework for unity-deploy.

Provides manifest-driven integration discovery, loading, and aggregation
for third-party platform connectors. Integrations are deployment assets
that live in ``unity_deploy/customization/integrations/packages/<slug>/``
and can be activated per deployment or seed layer.

Three connector tiers are supported:

- **MCP** (Tier 1): Model Context Protocol servers wrapped as FunctionManager entries.
- **API** (Tier 2): Direct REST/GraphQL API wrappers in isolated venvs.
- **Browser** (Tier 3): Browser automation via ComputerPrimitives with demo site mocks.
"""

from unity_deploy.customization.integrations.types import (
    Capability,
    DemoSiteConfig,
    IntegrationManifest,
    MCPServerConfig,
    QualityTier,
    SecretSchema,
)

__all__ = [
    "Capability",
    "DemoSiteConfig",
    "IntegrationManifest",
    "MCPServerConfig",
    "QualityTier",
    "SecretSchema",
]
