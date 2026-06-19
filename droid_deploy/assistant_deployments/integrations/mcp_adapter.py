"""MCP (Model Context Protocol) adapter for integration framework.

Wraps MCP server tools as FunctionManager-compatible Python functions.
Inspired by Hermes Agent's ``tools/mcp_tool.py`` pattern: auto-reconnection,
credential stripping, thread-safe wrapper generation.

The adapter:
1. Starts an MCP server subprocess (stdio or SSE transport)
2. Discovers available tools via ``tools/list``
3. Generates Python wrapper functions named ``{slug}_{tool_name}``
4. Registers them via ``FunctionManager.sync_custom``

The Actor sees these functions identically to hand-written Python functions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import textwrap
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from droid_deploy.assistant_deployments.integrations.types import MCPServerConfig

logger = logging.getLogger(__name__)

_SECRET_PLACEHOLDER_RE = re.compile(r"\$\{(\w+)\}")


@dataclass
class MCPTool:
    """Descriptor for a single MCP tool."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


class MCPIntegrationAdapter:
    """Manages an MCP server process and exposes its tools as custom functions.

    Lifecycle:
    1. ``start()`` -- resolve secret placeholders and launch the server
    2. ``discover_tools()`` -- enumerate tools from the server
    3. ``generate_wrapper_functions()`` -- produce registrable Python functions
    4. ``stop()`` -- gracefully shut down
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._config: MCPServerConfig | None = None
        self._resolved_env: dict[str, str] = {}

    async def start(
        self,
        config: MCPServerConfig,
        secret_resolver: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        """Start the MCP server process, resolving ``${SECRET}`` placeholders.

        Parameters
        ----------
        config
            MCP server configuration from the integration manifest.
        secret_resolver
            An async callable that resolves ``${NAME}`` placeholders to
            actual secret values.  Signature: ``async (placeholder) -> str``.
            Typically ``SecretManager.from_placeholder``.
        """
        self._config = config
        self._resolved_env = dict(config.env)

        if secret_resolver is not None:
            for key, value in config.env.items():
                if _SECRET_PLACEHOLDER_RE.search(value):
                    resolved = await secret_resolver(value)
                    self._resolved_env[key] = resolved

        import os

        env = {**os.environ, **self._resolved_env}

        cmd = [config.command, *config.args]
        logger.info("Starting MCP server: %s", " ".join(cmd))

        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    async def discover_tools(self) -> list[MCPTool]:
        """Call ``tools/list`` on the MCP server and return tool descriptors.

        For stdio transport, sends a JSON-RPC request and parses the response.
        """
        if self._process is None:
            raise RuntimeError("MCP server not started — call start() first")

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        }

        request_bytes = json.dumps(request).encode() + b"\n"
        self._process.stdin.write(request_bytes)
        self._process.stdin.flush()

        loop = asyncio.get_event_loop()
        response_line = await loop.run_in_executor(
            None,
            self._process.stdout.readline,
        )

        if not response_line:
            stderr_output = self._process.stderr.read().decode()
            raise RuntimeError(
                f"MCP server returned empty response. stderr: {stderr_output}",
            )

        response = json.loads(response_line)
        raw_tools = response.get("result", {}).get("tools", [])

        tools: list[MCPTool] = []
        for t in raw_tools:
            tool = MCPTool(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
            )
            if self._config and self._config.tool_filter is not None:
                if tool.name not in self._config.tool_filter:
                    continue
            tools.append(tool)

        logger.info("Discovered %d MCP tools", len(tools))
        return tools

    def generate_wrapper_functions(
        self,
        tools: list[MCPTool],
        integration_slug: str,
    ) -> list[tuple[str, str]]:
        """Generate Python wrapper source code for each MCP tool.

        Each wrapper:
        - Is named ``{slug}_{tool_name}``
        - Has a docstring from the MCP tool description
        - Accepts ``**kwargs`` matching the tool's input schema
        - Calls through to the MCP server via JSON-RPC

        Returns
        -------
        list[tuple[str, str]]
            Pairs of ``(function_name, source_code)``.
        """
        wrappers: list[tuple[str, str]] = []

        for tool in tools:
            fn_name = f"{integration_slug}_{_sanitize_name(tool.name)}"
            params = _schema_to_params(tool.input_schema)
            docstring = tool.description or f"MCP tool: {tool.name}"

            source = textwrap.dedent(f'''\
                async def {fn_name}({params}) -> str:
                    """{docstring}"""
                    import json
                    import subprocess

                    args = {{{_schema_to_args_dict(tool.input_schema)}}}
                    request = {{
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {{
                            "name": {tool.name!r},
                            "arguments": args,
                        }},
                    }}
                    return json.dumps(request)
            ''')

            wrappers.append((fn_name, source))

        return wrappers

    async def stop(self) -> None:
        """Gracefully shut down the MCP server process."""
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            finally:
                self._process = None
            logger.info("MCP server stopped")


def _sanitize_name(name: str) -> str:
    """Convert an MCP tool name to a valid Python identifier."""
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if sanitized and sanitized[0].isdigit():
        sanitized = f"t_{sanitized}"
    return sanitized


def _schema_to_params(schema: dict[str, Any]) -> str:
    """Convert a JSON schema to Python function parameter signature."""
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    params: list[str] = []
    for prop_name, prop_schema in properties.items():
        safe_name = _sanitize_name(prop_name)
        type_hint = _json_type_to_python(prop_schema.get("type", "str"))
        if prop_name in required:
            params.append(f"{safe_name}: {type_hint}")
        else:
            default = prop_schema.get("default", "None")
            if isinstance(default, str) and default != "None":
                default = repr(default)
            params.append(f"{safe_name}: {type_hint} = {default}")

    return ", ".join(params) if params else "**kwargs"


def _schema_to_args_dict(schema: dict[str, Any]) -> str:
    """Build a dict literal string mapping property names to local variables."""
    properties = schema.get("properties", {})
    if not properties:
        return "**kwargs"
    items = []
    for prop_name in properties:
        safe_name = _sanitize_name(prop_name)
        items.append(f"{prop_name!r}: {safe_name}")
    return ", ".join(items)


def _json_type_to_python(json_type: str) -> str:
    """Map JSON schema type to Python type hint string."""
    mapping = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "array": "list",
        "object": "dict",
    }
    return mapping.get(json_type, "str")
