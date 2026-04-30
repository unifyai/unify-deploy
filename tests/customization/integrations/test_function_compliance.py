"""Symbolic AST compliance tests for integration functions.

Auto-discovers ALL built-in integrations with a ``functions/`` directory
and validates that every function file follows the FunctionManager rules:

- No module-level globals referenced inside function bodies
- Every top-level function decorated with ``@custom_function()``
- No ``logger.*()`` or ``print()`` calls in function bodies
- Each extracted source parses as a single function at column 0

When someone adds a new integration package with a ``functions/`` dir,
these tests cover it automatically -- no manual registration needed.
"""

from __future__ import annotations

import ast

import pytest

from tests.customization.integrations.integration_test_helpers import (
    check_all_functions_decorated,
    check_no_logging_or_print,
    check_no_module_globals_in_functions,
    discover_function_files,
    discover_integration_function_dirs,
    extract_standalone_sources,
)

_INTEGRATION_DIRS = discover_integration_function_dirs()
_INTEGRATION_IDS = [slug for slug, _ in _INTEGRATION_DIRS]


def _all_function_files() -> list[tuple[str, str]]:
    """Return ``(slug, filepath_str)`` pairs for parametrization."""
    results: list[tuple[str, str]] = []
    for slug, funcs_dir in _INTEGRATION_DIRS:
        for py_file in discover_function_files(funcs_dir):
            results.append((slug, str(py_file)))
    return results


_ALL_FILES = _all_function_files()
_ALL_FILE_IDS = [f"{slug}/{f.rsplit('/', 1)[-1]}" for slug, f in _ALL_FILES]


@pytest.mark.parametrize("slug,filepath", _ALL_FILES, ids=_ALL_FILE_IDS)
class TestFunctionCompliance:
    """Run on every function file in every built-in integration."""

    def test_no_module_level_globals_in_functions(
        self,
        slug: str,
        filepath: str,
    ):
        from pathlib import Path

        errors = check_no_module_globals_in_functions(Path(filepath))
        assert not errors, (
            f"[{slug}] Module-level globals referenced in function bodies:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    def test_all_functions_have_custom_function_decorator(
        self,
        slug: str,
        filepath: str,
    ):
        from pathlib import Path

        errors = check_all_functions_decorated(Path(filepath))
        assert (
            not errors
        ), f"[{slug}] Functions missing @custom_function():\n" + "\n".join(
            f"  - {e}" for e in errors
        )

    def test_no_logging_or_print(self, slug: str, filepath: str):
        from pathlib import Path

        errors = check_no_logging_or_print(Path(filepath))
        assert (
            not errors
        ), f"[{slug}] Logging/print calls in function bodies:\n" + "\n".join(
            f"  - {e}" for e in errors
        )

    def test_each_extracted_source_is_single_function(
        self,
        slug: str,
        filepath: str,
    ):
        from pathlib import Path

        sources = extract_standalone_sources(Path(filepath))
        assert sources, f"[{slug}] No functions found in {filepath}"
        for source in sources:
            tree = ast.parse(source)
            assert len(tree.body) == 1, (
                f"[{slug}] Expected 1 top-level node, got {len(tree.body)} "
                f"in extracted source from {filepath}"
            )
            node = tree.body[0]
            assert isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef),
            ), (
                f"[{slug}] Top-level node is {type(node).__name__}, "
                f"expected FunctionDef/AsyncFunctionDef"
            )
            assert node.col_offset == 0, (
                f"[{slug}] Function '{node.name}' starts at column "
                f"{node.col_offset}, expected 0"
            )
