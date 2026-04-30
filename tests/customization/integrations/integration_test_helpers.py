"""Reusable test helpers for integration function compliance and execution.

Auto-discovers all built-in integration packages that ship a ``functions/``
directory, extracts standalone function sources (stripping decorators via
AST), and provides AST-based compliance checks that run without any backend.

New integrations get automatic coverage: just add a ``functions/`` dir and
the parameterized tests pick it up.
"""

from __future__ import annotations

import ast
from pathlib import Path

_INTEGRATIONS_ROOT = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "unity_deploy"
    / "customization"
    / "integrations"
    / "packages"
)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_integration_function_dirs() -> list[tuple[str, Path]]:
    """Auto-discover built-in integrations that have a ``functions/`` dir.

    Returns
    -------
    list[tuple[str, Path]]
        ``(slug, functions_dir)`` pairs, sorted by slug.
    """
    results: list[tuple[str, Path]] = []
    for candidate in sorted(_INTEGRATIONS_ROOT.iterdir()):
        if not candidate.is_dir():
            continue
        manifest = candidate / "manifest.yaml"
        funcs_dir = candidate / "functions"
        if manifest.is_file() and funcs_dir.is_dir():
            results.append((candidate.name, funcs_dir))
    return results


def discover_function_files(funcs_dir: Path) -> list[Path]:
    """Return all non-private ``.py`` files in a functions directory."""
    return sorted(f for f in funcs_dir.glob("*.py") if not f.name.startswith("_"))


# ---------------------------------------------------------------------------
# Source extraction (for FunctionManager.add_functions)
# ---------------------------------------------------------------------------


def extract_standalone_sources(filepath: Path) -> list[str]:
    """Read a ``@custom_function()`` file and return bare function sources.

    Strips the module docstring, imports, and ``@custom_function()``
    decorators so each result is a clean function definition suitable
    for ``FunctionManager.add_functions(implementations=...)``.
    """
    source = filepath.read_text()
    tree = ast.parse(source)

    func_nodes = [
        n
        for n in ast.iter_child_nodes(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if not func_nodes:
        return []

    sources: list[str] = []
    for func_node in func_nodes:
        func_node.decorator_list = []
        sources.append(ast.unparse(func_node))
    return sources


def build_implementations_for_integration(
    funcs_dir: Path,
) -> list[str]:
    """Extract standalone sources from ALL ``.py`` files in an integration's
    ``functions/`` directory."""
    implementations: list[str] = []
    for py_file in discover_function_files(funcs_dir):
        implementations.extend(extract_standalone_sources(py_file))
    return implementations


# ---------------------------------------------------------------------------
# AST compliance checks
# ---------------------------------------------------------------------------


def collect_module_level_names(filepath: Path) -> set[str]:
    """Return names of all module-level variable assignments."""
    source = filepath.read_text()
    tree = ast.parse(source, filename=str(filepath))

    names: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def check_no_module_globals_in_functions(filepath: Path) -> list[str]:
    """Check that no ``@custom_function`` body references a module-level name.

    Returns a list of error strings (empty means compliant).
    """
    module_names = collect_module_level_names(filepath)
    if not module_names:
        return []

    source = filepath.read_text()
    tree = ast.parse(source, filename=str(filepath))

    errors: list[str] = []
    func_nodes = [
        n
        for n in ast.iter_child_nodes(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for fn_node in func_nodes:
        for node in ast.walk(fn_node):
            if (
                isinstance(node, ast.Name)
                and node.id in module_names
                and isinstance(node.ctx, ast.Load)
            ):
                errors.append(
                    f"{fn_node.name} in {filepath.name} references "
                    f"module-level variable '{node.id}'",
                )
    return errors


def check_all_functions_decorated(filepath: Path) -> list[str]:
    """Check that every top-level function has ``@custom_function()``."""
    source = filepath.read_text()
    tree = ast.parse(source, filename=str(filepath))

    errors: list[str] = []
    func_nodes = [
        n
        for n in ast.iter_child_nodes(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for fn_node in func_nodes:
        has_custom = False
        for dec in fn_node.decorator_list:
            dec_name = _decorator_name(dec)
            if dec_name and "custom_function" in dec_name:
                has_custom = True
                break
        if not has_custom:
            errors.append(
                f"{fn_node.name} in {filepath.name} missing "
                f"@custom_function() decorator",
            )
    return errors


def check_no_logging_or_print(filepath: Path) -> list[str]:
    """Check that no function body calls ``logger.*()`` or ``print()``."""
    source = filepath.read_text()
    tree = ast.parse(source, filename=str(filepath))

    errors: list[str] = []
    func_nodes = [
        n
        for n in ast.iter_child_nodes(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for fn_node in func_nodes:
        for node in ast.walk(fn_node):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(
                func.value,
                ast.Name,
            ):
                if func.value.id == "logger":
                    errors.append(
                        f"{fn_node.name} in {filepath.name} calls "
                        f"logger.{func.attr}()",
                    )
            if isinstance(func, ast.Name) and func.id == "print":
                errors.append(
                    f"{fn_node.name} in {filepath.name} calls print()",
                )
    return errors


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _decorator_name(dec: ast.AST) -> str | None:
    """Extract a decorator's name string (handles ``@name`` and ``@name()``)."""
    if isinstance(dec, ast.Name):
        return dec.id
    if isinstance(dec, ast.Call):
        return _decorator_name(dec.func)
    if isinstance(dec, ast.Attribute):
        return dec.attr
    return None
