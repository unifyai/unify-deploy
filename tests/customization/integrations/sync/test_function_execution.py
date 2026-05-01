"""FunctionManager registration and live callable execution tests.

Auto-discovers every integration with a ``functions/`` directory across the
generic, client, and mock roots, registers their functions via
``FunctionManager.add_functions``, and executes representative functions
in mock mode to verify they work in FunctionManager's isolated exec
namespace.

Mocks are deliberately included here because they are exactly the surface
that scenario E2Es and offline task activations register at runtime, so we
must guarantee they survive AST extraction and decorator stripping just
like any production connector. The full Unity backend is required because
``FunctionManager`` resolves manager contexts; ``sync/conftest.py``
installs an explicit per-test base context for real manager instances.

To add execution coverage for a new integration, add an entry to
``EXECUTION_CONFIG`` below. Functions that import third-party packages
must also have a matching ``_INTEGRATION_VENVS`` entry.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from tests.helpers import _handle_project
from tests.customization.integrations.integration_test_helpers import (
    build_implementations_for_integration,
    discover_all_function_dirs,
)
from unity.common.context_registry import ContextRegistry
from unity.function_manager.execution_env import create_base_globals
from unity.function_manager.function_manager import FunctionManager

# ---------------------------------------------------------------------------
# Per-integration execution configuration
#
# Each entry specifies a representative function to call and the expected
# return shape. Integrations without an entry still get registration-only
# testing (add_functions with no errors).
#
# To add coverage for a new integration, add an entry here.
# ---------------------------------------------------------------------------

EXECUTION_CONFIG: Dict[str, Dict[str, Any]] = {
    "github": {
        "representative_function": "get_user",
        "call_kwargs": {"username": "octocat", "mock": True},
        "expected_keys": {"login", "id", "name"},
    },
    "client_alpha_repairs_mock": {
        "representative_function": "fetch_repairs_snapshot",
        "call_kwargs": {"tick": 0},
        "expected_keys": {"schema_version", "tables", "metadata"},
    },
    "clientepsilon_homes_compliance_mock": {
        "representative_function": "fetch_compliance_snapshot",
        "call_kwargs": {"tick": 0},
        "expected_keys": {"schema_version", "tables", "metadata"},
    },
}


# ---------------------------------------------------------------------------
# Discovery -- all roots including mocks because mock connectors are the
# exact surface that FunctionManager registers when scenario tests or
# offline task activations run them.
# ---------------------------------------------------------------------------

_INTEGRATION_DIRS = discover_all_function_dirs(include_mock=True)
_INTEGRATION_PARAMS = [
    (slug, funcs_dir) for _root, slug, funcs_dir in _INTEGRATION_DIRS
]
_INTEGRATION_IDS = [f"{root_kind}/{slug}" for root_kind, slug, _ in _INTEGRATION_DIRS]

_FM_CONTEXTS = (
    "Functions/VirtualEnvs",
    "Functions/Compositional",
    "Functions/Primitives",
    "Functions/Meta",
)

_INTEGRATION_VENVS: Dict[str, str] = {
    "github": """
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "github-integration-test"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "httpx",
]
""".strip(),
    "clientepsilon_homes_compliance": """
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "clientepsilon-homes-compliance-test"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "httpx",
]
""".strip(),
    "clientepsilon_homes_compliance_mock": """
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "clientepsilon-homes-compliance-mock-test"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "httpx",
    "pydantic",
    "pypdf",
]
""".strip(),
    "client_alpha_repairs_mock": """
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "client-alpha-repairs-mock-test"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []
""".strip(),
}


def _add_integration_functions(
    fm: FunctionManager,
    slug: str,
    funcs_dir,
) -> dict[str, str]:
    implementations = build_implementations_for_integration(funcs_dir)
    assert implementations, f"No implementations found for '{slug}'"

    venv = _INTEGRATION_VENVS.get(slug)
    venv_id = fm.add_venv(venv=venv) if venv is not None else None
    return fm.add_functions(
        implementations=implementations,
        overwrite=True,
        venv_id=venv_id,
    )


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


@pytest.mark.requires_orchestra
@pytest.mark.parametrize(
    "slug,funcs_dir",
    _INTEGRATION_PARAMS,
    ids=_INTEGRATION_IDS,
)
class TestFunctionRegistration:
    """Verify that integration functions register without errors."""

    @_handle_project
    def test_add_functions_no_errors(self, slug, funcs_dir):
        for ctx in _FM_CONTEXTS:
            ContextRegistry.forget(FunctionManager, ctx)

        fm = FunctionManager()
        results = _add_integration_functions(fm, slug, funcs_dir)
        errors = {k: v for k, v in results.items() if str(v).startswith("error")}
        assert not errors, f"Registration errors for '{slug}':\n" + "\n".join(
            f"  {k}: {v}" for k, v in errors.items()
        )


# ---------------------------------------------------------------------------
# Live callable execution tests
# ---------------------------------------------------------------------------


@pytest.mark.requires_orchestra
@pytest.mark.parametrize(
    "slug,funcs_dir",
    _INTEGRATION_PARAMS,
    ids=_INTEGRATION_IDS,
)
class TestFunctionExecution:
    """Execute a representative function from each configured integration."""

    @pytest.mark.asyncio
    @_handle_project
    async def test_representative_function_executes(self, slug, funcs_dir):
        config = EXECUTION_CONFIG.get(slug)
        if config is None:
            pytest.skip(
                f"No EXECUTION_CONFIG entry for '{slug}' -- "
                f"add one to test_function_execution.py to enable "
                f"live execution testing",
            )

        for ctx in _FM_CONTEXTS:
            ContextRegistry.forget(FunctionManager, ctx)

        fm = FunctionManager()
        _add_integration_functions(fm, slug, funcs_dir)

        func_name = config["representative_function"]
        ns = create_base_globals()
        callables = fm.filter_functions(
            filter=f"name == '{func_name}'",
            limit=1,
            _return_callable=True,
            _namespace=ns,
        )
        assert (
            len(callables) == 1
        ), f"Expected 1 callable for '{func_name}', got {len(callables)}"
        assert func_name in ns and callable(
            ns[func_name],
        ), f"'{func_name}' not injected into sandbox namespace"

        out = await ns[func_name](**config["call_kwargs"])

        assert isinstance(
            out,
            dict,
        ), f"Expected dict from '{func_name}', got {type(out).__name__}"
        expected_keys = config.get("expected_keys", set())
        for key in expected_keys:
            assert key in out, (
                f"Missing key '{key}' in result from '{func_name}': "
                f"{list(out.keys())}"
            )
