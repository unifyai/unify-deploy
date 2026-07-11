"""Reusable test helpers for integration function compliance and execution.

The unity-deploy integration framework intentionally separates packages by
ownership and runtime use. Tests follow the same separation:

* ``packages/`` holds reusable platform/provider connectors (the
  ``"generic"`` root). These are always discovered.
* ``client_packages/`` holds private client-specific connectors or
  compositions (the ``"client"`` root). Always discovered for compliance,
  symbolic, and registration tests.
* ``mock_packages/`` holds opt-in deterministic test doubles used by
  demos and pilot E2E flows (the ``"mock"`` root). Only discovered
  when callers explicitly request it.

These helpers extract standalone function sources (stripping decorators
via AST) and run AST-only compliance checks without any backend.
"""

from __future__ import annotations

import ast
from pathlib import Path

_INTEGRATIONS_PARENT = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "unify_deploy"
    / "assistant_deployments"
    / "integrations"
)
_GENERIC_DIR = _INTEGRATIONS_PARENT / "packages"
_CLIENT_DIR = _INTEGRATIONS_PARENT / "client_packages"
_MOCK_DIR = _INTEGRATIONS_PARENT / "mock_packages"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _discover_in_root(root: Path) -> list[tuple[str, Path]]:
    """Return ``(slug, functions_dir)`` pairs for every package under ``root``."""
    results: list[tuple[str, Path]] = []
    if not root.is_dir():
        return results
    for candidate in sorted(root.iterdir()):
        if not candidate.is_dir():
            continue
        manifest = candidate / "manifest.yaml"
        funcs_dir = candidate / "functions"
        if manifest.is_file() and funcs_dir.is_dir():
            results.append((candidate.name, funcs_dir))
    return results


def discover_integration_function_dirs() -> list[tuple[str, Path]]:
    """Auto-discover generic ``packages/`` integrations with a ``functions/`` dir.

    Only the generic root is returned. Client and mock roots are intentionally
    excluded because they have different ownership/runtime expectations and
    must be opted in to via :func:`discover_client_function_dirs`,
    :func:`discover_mock_function_dirs`, or :func:`discover_all_function_dirs`.
    """
    return _discover_in_root(_GENERIC_DIR)


def discover_client_function_dirs() -> list[tuple[str, Path]]:
    """Auto-discover ``client_packages/`` integrations with a ``functions/`` dir."""
    return _discover_in_root(_CLIENT_DIR)


def discover_mock_function_dirs() -> list[tuple[str, Path]]:
    """Auto-discover ``mock_packages/`` integrations with a ``functions/`` dir.

    Mock packages are opt-in: production deploy resolution does not include
    them by default. Tests that need mock-connector execution coverage must
    explicitly call this helper or :func:`discover_all_function_dirs` with
    ``include_mock=True``.
    """
    return _discover_in_root(_MOCK_DIR)


def discover_all_function_dirs(
    *,
    include_mock: bool = False,
) -> list[tuple[str, str, Path]]:
    """Discover function dirs across roots.

    Parameters
    ----------
    include_mock:
        Whether to also include ``mock_packages/`` integrations. Mock
        discovery is gated because mock packages mirror production schemas
        but should never be activated by deploy resolution.

    Returns
    -------
    list[tuple[str, str, pathlib.Path]]
        ``(root_kind, slug, functions_dir)`` triples where ``root_kind`` is
        one of ``"generic"``, ``"client"``, or ``"mock"``. Stable order:
        generic -> client -> mock, each sorted by slug.
    """
    results: list[tuple[str, str, Path]] = []
    for slug, funcs_dir in discover_integration_function_dirs():
        results.append(("generic", slug, funcs_dir))
    for slug, funcs_dir in discover_client_function_dirs():
        results.append(("client", slug, funcs_dir))
    if include_mock:
        for slug, funcs_dir in discover_mock_function_dirs():
            results.append(("mock", slug, funcs_dir))
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
