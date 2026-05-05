"""Symbolic AST compliance tests for integration functions.

Auto-discovers every integration with a ``functions/`` directory across all
three roots -- generic ``packages/``, private ``client_packages/``, and
opt-in ``mock_packages/`` -- and validates that every function file follows
the FunctionManager rules:

- No module-level globals referenced inside function bodies
- Every top-level function decorated with ``@custom_function()``
- No ``logger.*()`` or ``print()`` calls in function bodies
- Each extracted source parses as a single function at column 0

Mock packages are intentionally included here because the rules are purely
structural and apply to anything destined for FunctionManager regardless of
deploy activation. When a new integration package ships with a ``functions/``
dir under any root, these tests cover it automatically -- no manual
registration needed.
"""

from __future__ import annotations

import ast

import pytest

from tests.assistant_deployments.integrations.integration_test_helpers import (
    check_all_functions_decorated,
    check_no_logging_or_print,
    check_no_module_globals_in_functions,
    discover_all_function_dirs,
    discover_function_files,
    extract_standalone_sources,
)

_INTEGRATION_DIRS = discover_all_function_dirs(include_mock=True)


def _all_function_files() -> list[tuple[str, str, str]]:
    """Return ``(root_kind, slug, filepath_str)`` triples for parametrization."""
    results: list[tuple[str, str, str]] = []
    for root_kind, slug, funcs_dir in _INTEGRATION_DIRS:
        for py_file in discover_function_files(funcs_dir):
            results.append((root_kind, slug, str(py_file)))
    return results


_ALL_FILES = _all_function_files()
_ALL_FILE_IDS = [
    f"{root_kind}/{slug}/{f.rsplit('/', 1)[-1]}" for root_kind, slug, f in _ALL_FILES
]


@pytest.mark.parametrize(
    "root_kind,slug,filepath",
    _ALL_FILES,
    ids=_ALL_FILE_IDS,
)
class TestFunctionCompliance:
    """Run on every function file in every integration root."""

    def test_no_module_level_globals_in_functions(
        self,
        root_kind: str,
        slug: str,
        filepath: str,
    ):
        from pathlib import Path

        errors = check_no_module_globals_in_functions(Path(filepath))
        assert not errors, (
            f"[{root_kind}/{slug}] Module-level globals referenced in "
            f"function bodies:\n" + "\n".join(f"  - {e}" for e in errors)
        )

    def test_all_functions_have_custom_function_decorator(
        self,
        root_kind: str,
        slug: str,
        filepath: str,
    ):
        from pathlib import Path

        errors = check_all_functions_decorated(Path(filepath))
        assert (
            not errors
        ), f"[{root_kind}/{slug}] Functions missing @custom_function():\n" + "\n".join(
            f"  - {e}" for e in errors
        )

    def test_no_logging_or_print(
        self,
        root_kind: str,
        slug: str,
        filepath: str,
    ):
        from pathlib import Path

        errors = check_no_logging_or_print(Path(filepath))
        assert not errors, (
            f"[{root_kind}/{slug}] Logging/print calls in function bodies:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    def test_each_extracted_source_is_single_function(
        self,
        root_kind: str,
        slug: str,
        filepath: str,
    ):
        from pathlib import Path

        sources = extract_standalone_sources(Path(filepath))
        assert sources, f"[{root_kind}/{slug}] No functions found in {filepath}"
        for source in sources:
            tree = ast.parse(source)
            assert len(tree.body) == 1, (
                f"[{root_kind}/{slug}] Expected 1 top-level node, got "
                f"{len(tree.body)} in extracted source from {filepath}"
            )
            node = tree.body[0]
            assert isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef),
            ), (
                f"[{root_kind}/{slug}] Top-level node is "
                f"{type(node).__name__}, expected FunctionDef/AsyncFunctionDef"
            )
            assert node.col_offset == 0, (
                f"[{root_kind}/{slug}] Function '{node.name}' starts at "
                f"column {node.col_offset}, expected 0"
            )
