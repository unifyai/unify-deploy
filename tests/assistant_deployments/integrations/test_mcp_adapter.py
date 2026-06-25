"""Symbolic tests for MCP adapter wrapper generation.

Tests the code generation and helper functions without requiring
an actual MCP server process.
"""

from unity_deploy.assistant_deployments.integrations.mcp_adapter import (
    MCPIntegrationAdapter,
    MCPTool,
    _json_type_to_python,
    _sanitize_name,
    _schema_to_params,
)


class TestSanitizeName:
    def test_clean_name(self):
        assert _sanitize_name("search_documents") == "search_documents"

    def test_replaces_special_chars(self):
        assert _sanitize_name("foo-bar.baz") == "foo_bar_baz"

    def test_numeric_prefix(self):
        assert _sanitize_name("3d_model") == "t_3d_model"

    def test_empty_string(self):
        assert _sanitize_name("") == ""


class TestJsonTypeToPython:
    def test_string(self):
        assert _json_type_to_python("string") == "str"

    def test_integer(self):
        assert _json_type_to_python("integer") == "int"

    def test_number(self):
        assert _json_type_to_python("number") == "float"

    def test_boolean(self):
        assert _json_type_to_python("boolean") == "bool"

    def test_array(self):
        assert _json_type_to_python("array") == "list"

    def test_object(self):
        assert _json_type_to_python("object") == "dict"

    def test_unknown_defaults_to_str(self):
        assert _json_type_to_python("custom") == "str"


class TestSchemaToParams:
    def test_empty_schema(self):
        assert _schema_to_params({}) == "**kwargs"

    def test_required_params(self):
        schema = {
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query", "limit"],
        }
        params = _schema_to_params(schema)
        assert "query: str" in params
        assert "limit: int" in params

    def test_optional_params(self):
        schema = {
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        }
        params = _schema_to_params(schema)
        assert "query: str" in params
        assert "limit: int = 10" in params


class TestGenerateWrapperFunctions:
    def test_generates_correct_name(self):
        adapter = MCPIntegrationAdapter()
        tools = [MCPTool(name="search", description="Search docs")]
        wrappers = adapter.generate_wrapper_functions(tools, "salesforce")
        assert len(wrappers) == 1
        fn_name, source = wrappers[0]
        assert fn_name == "salesforce_search"

    def test_includes_docstring(self):
        adapter = MCPIntegrationAdapter()
        tools = [MCPTool(name="query", description="Run a SOQL query")]
        wrappers = adapter.generate_wrapper_functions(tools, "sf")
        _, source = wrappers[0]
        assert "Run a SOQL query" in source

    def test_multiple_tools(self):
        adapter = MCPIntegrationAdapter()
        tools = [
            MCPTool(name="search", description="Search"),
            MCPTool(name="create", description="Create"),
            MCPTool(name="delete", description="Delete"),
        ]
        wrappers = adapter.generate_wrapper_functions(tools, "crm")
        assert len(wrappers) == 3
        names = {w[0] for w in wrappers}
        assert names == {"crm_search", "crm_create", "crm_delete"}

    def test_with_input_schema(self):
        adapter = MCPIntegrationAdapter()
        tools = [
            MCPTool(
                name="search",
                description="Search",
                input_schema={
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            ),
        ]
        wrappers = adapter.generate_wrapper_functions(tools, "test")
        _, source = wrappers[0]
        assert "query: str" in source

    def test_sanitizes_tool_names(self):
        adapter = MCPIntegrationAdapter()
        tools = [MCPTool(name="get-user.info", description="Get user")]
        wrappers = adapter.generate_wrapper_functions(tools, "api")
        fn_name, _ = wrappers[0]
        assert fn_name == "api_get_user_info"
