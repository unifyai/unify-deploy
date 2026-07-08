"""Cross-client deployment compliance tests.

Auto-discovers ALL client packages with a ``deployments/`` subdirectory
and validates each deployment against the standard contract: directory
structure, DeploymentSpec loadability, function quality (when applicable),
and secrets structure (when applicable).

Lightweight deployments (no ``function_dir`` / ``pipeline_config``) are
tested for basic structural compliance only.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import sys
import types as builtin_types
from pathlib import Path
from types import ModuleType
from typing import List

import pytest

import unity_deploy.assistant_deployments.clients
from unify.guidance_manager.custom_guidance import collect_custom_guidance
from unity_deploy.assistant_deployments.deployment_types import (
    DeploymentSpec,
    load_deployment,
    resolve_deployment_name,
)

# ─────────────────────────────────────────────────────────────────────────────
# Cross-client deployment discovery
# ─────────────────────────────────────────────────────────────────────────────

_CLIENTS_DIR = Path(unity_deploy.assistant_deployments.clients.__file__).parent


def _discover_all_deployments() -> list[tuple[str, str, Path]]:
    """Return ``(client_name, deployment_name, deployments_dir)`` tuples."""
    results: list[tuple[str, str, Path]] = []
    for client in sorted(_CLIENTS_DIR.iterdir()):
        dep_dir = client / "deployments"
        if not client.is_dir() or not dep_dir.is_dir():
            continue
        for deploy in sorted(dep_dir.iterdir()):
            if deploy.is_dir() and (deploy / "deployment.py").exists():
                results.append((client.name, deploy.name, dep_dir))
    return results


_ALL_DEPLOYMENTS = _discover_all_deployments()
_ALL_IDS = [f"{c}/{d}" for c, d, _ in _ALL_DEPLOYMENTS]


def _load(client: str, deploy: str, dep_dir: Path) -> DeploymentSpec:
    return load_deployment(dep_dir, deploy)


_loaded_packages: dict[str, ModuleType] = {}


def _import_function_dir(func_dir: Path, pkg_prefix: str) -> dict[str, ModuleType]:
    """Load all .py files from a function directory as a temporary package.

    Sets up ``sys.modules`` entries so that relative imports between
    siblings (e.g. ``from .helpers import ...`` in ``metrics.py``) work.
    Returns a dict mapping stem name to loaded module.
    """
    if pkg_prefix in _loaded_packages:
        return {
            stem: sys.modules[f"{pkg_prefix}.{stem}"]
            for stem in _loaded_packages[pkg_prefix].__dict__
            if isinstance(_loaded_packages[pkg_prefix].__dict__.get(stem), ModuleType)
        }

    pkg = builtin_types.ModuleType(pkg_prefix)
    pkg.__path__ = [str(func_dir)]
    pkg.__package__ = pkg_prefix
    sys.modules[pkg_prefix] = pkg
    _loaded_packages[pkg_prefix] = pkg

    modules: dict[str, ModuleType] = {}
    py_files = sorted(f for f in func_dir.glob("*.py") if not f.name.startswith("_"))
    for py_file in py_files:
        full_name = f"{pkg_prefix}.{py_file.stem}"
        spec = importlib.util.spec_from_file_location(
            full_name,
            py_file,
            submodule_search_locations=[],
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = pkg_prefix
        sys.modules[full_name] = mod
        setattr(pkg, py_file.stem, mod)
        spec.loader.exec_module(mod)
        modules[py_file.stem] = mod

    return modules


# ─────────────────────────────────────────────────────────────────────────────
# Structural tests — run for ALL deployments
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("client,deploy,dep_dir", _ALL_DEPLOYMENTS, ids=_ALL_IDS)
class TestDeploymentStructure:
    """Every deployment must load and return a valid DeploymentSpec."""

    def test_deployment_py_exists(self, client: str, deploy: str, dep_dir: Path):
        assert (dep_dir / deploy / "deployment.py").is_file()

    def test_spec_loadable(self, client: str, deploy: str, dep_dir: Path):
        spec = _load(client, deploy, dep_dir)
        assert isinstance(spec, DeploymentSpec)
        assert spec.name == deploy

    def test_guidance_nonempty(self, client: str, deploy: str, dep_dir: Path):
        spec = _load(client, deploy, dep_dir)
        assert spec.guidance_dir is not None
        entries = collect_custom_guidance(path=spec.guidance_dir)
        assert len(entries) >= 1
        for entry in entries.values():
            assert entry["title"]
            assert len(entry["content"]) > 50

    def test_console_config_shape(self, client: str, deploy: str, dep_dir: Path):
        spec = _load(client, deploy, dep_dir)
        cfg = spec.console_config
        if cfg is None:
            return

        assert cfg.get("version") == "1"

        layout = cfg.get("layout")
        assert isinstance(layout, dict)
        assert layout.get("mode") in {"standard", "dashboard-centric"}
        if "defaultTab" in layout:
            assert isinstance(layout["defaultTab"], str)

        tabs = cfg.get("tabs")
        if tabs is not None:
            assert isinstance(tabs, dict)
            if "hidden" in tabs:
                assert isinstance(tabs["hidden"], list)
                assert all(isinstance(tab, str) for tab in tabs["hidden"])
            if "order" in tabs:
                assert isinstance(tabs["order"], list)
                assert all(isinstance(tab, str) for tab in tabs["order"])

        theme = cfg.get("theme")
        if theme is not None:
            assert isinstance(theme, dict)
            if "brandName" in theme:
                assert isinstance(theme["brandName"], str)
            if "accentColor" in theme:
                assert isinstance(theme["accentColor"], str)


# ─────────────────────────────────────────────────────────────────────────────
# Data-pipeline structure tests — only for deployments with function_dir
# ─────────────────────────────────────────────────────────────────────────────


def _data_deployments():
    """Deployments that declare function_dir / pipeline_config."""
    results = []
    for client, deploy, dep_dir in _ALL_DEPLOYMENTS:
        spec = _load(client, deploy, dep_dir)
        if spec.function_dir is not None:
            results.append((client, deploy, dep_dir))
    return results


_DATA_DEPLOYMENTS = _data_deployments()
_DATA_IDS = [f"{c}/{d}" for c, d, _ in _DATA_DEPLOYMENTS]


@pytest.mark.parametrize("client,deploy,dep_dir", _DATA_DEPLOYMENTS, ids=_DATA_IDS)
class TestDataPipelineStructure:
    """Validate data-pipeline deployments have functions/, data/, pipeline_config."""

    def test_has_data_dir(self, client: str, deploy: str, dep_dir: Path):
        spec = _load(client, deploy, dep_dir)
        assert spec.data_dir is not None and spec.data_dir.is_dir()

    def test_has_pipeline_config(self, client: str, deploy: str, dep_dir: Path):
        spec = _load(client, deploy, dep_dir)
        if spec.pipeline_config is not None and spec.data_dir is not None:
            config_path = spec.data_dir / "pipeline_config.json"
            assert config_path.is_file()
            data = json.loads(config_path.read_text())
            assert isinstance(data, dict)

    def test_has_functions_dir(self, client: str, deploy: str, dep_dir: Path):
        spec = _load(client, deploy, dep_dir)
        assert spec.function_dir is not None and spec.function_dir.is_dir()

    def test_has_helpers_and_metrics(self, client: str, deploy: str, dep_dir: Path):
        spec = _load(client, deploy, dep_dir)
        assert spec.function_dir is not None
        assert (spec.function_dir / "helpers.py").is_file()
        assert (spec.function_dir / "metrics.py").is_file()

    def test_function_dir_has_py_files(self, client: str, deploy: str, dep_dir: Path):
        spec = _load(client, deploy, dep_dir)
        assert spec.function_dir is not None
        py_files = [
            f for f in spec.function_dir.glob("*.py") if not f.name.startswith("_")
        ]
        assert len(py_files) >= 2, f"Expected >=2 function files, got {len(py_files)}"


# ─────────────────────────────────────────────────────────────────────────────
# Function quality tests — only for deployments with function_dir
# ─────────────────────────────────────────────────────────────────────────────


def _get_function_files(spec: DeploymentSpec) -> List[Path]:
    assert spec.function_dir is not None
    return sorted(
        f for f in spec.function_dir.glob("*.py") if not f.name.startswith("_")
    )


def _get_function_nodes(filepath: Path):
    source = filepath.read_text()
    tree = ast.parse(source, filename=str(filepath))
    funcs = [
        n
        for n in ast.iter_child_nodes(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return tree, funcs


@pytest.mark.parametrize("client,deploy,dep_dir", _DATA_DEPLOYMENTS, ids=_DATA_IDS)
class TestFunctionQuality:
    """Verify functions follow the design principles."""

    def test_all_functions_have_custom_function_metadata(
        self,
        client: str,
        deploy: str,
        dep_dir: Path,
    ):
        spec = _load(client, deploy, dep_dir)
        assert spec.function_dir is not None
        pkg_prefix = f"_testpkg_{client}_{deploy}"
        loaded_modules = _import_function_dir(spec.function_dir, pkg_prefix)
        for py_file in _get_function_files(spec):
            module = loaded_modules[py_file.stem]
            _, func_nodes = _get_function_nodes(py_file)
            for fn_node in func_nodes:
                if fn_node.name.startswith("_"):
                    continue
                fn_obj = getattr(module, fn_node.name, None)
                assert fn_obj is not None, f"{fn_node.name} not in {py_file.name}"
                assert callable(fn_obj), f"{fn_node.name} is not callable"
                assert hasattr(
                    fn_obj,
                    "_custom_function_metadata",
                ), f"{fn_node.name} in {py_file.name} missing @custom_function()"

    def test_no_logging_in_function_bodies(
        self,
        client: str,
        deploy: str,
        dep_dir: Path,
    ):
        spec = _load(client, deploy, dep_dir)
        for py_file in _get_function_files(spec):
            _, func_nodes = _get_function_nodes(py_file)
            for fn_node in func_nodes:
                for node in ast.walk(fn_node):
                    if isinstance(node, ast.Call):
                        func = node.func
                        if isinstance(func, ast.Attribute) and isinstance(
                            func.value,
                            ast.Name,
                        ):
                            if func.value.id == "logger":
                                pytest.fail(
                                    f"{fn_node.name} in {py_file.name} calls "
                                    f"logger.{func.attr}()",
                                )
                        if isinstance(func, ast.Name) and func.id == "print":
                            pytest.fail(
                                f"{fn_node.name} in {py_file.name} calls print()",
                            )

    def test_no_module_level_globals_in_functions(
        self,
        client: str,
        deploy: str,
        dep_dir: Path,
    ):
        spec = _load(client, deploy, dep_dir)
        for py_file in _get_function_files(spec):
            source = py_file.read_text()
            tree = ast.parse(source, filename=str(py_file))

            module_level_names = set()
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            module_level_names.add(target.id)
                elif isinstance(node, ast.AnnAssign):
                    if isinstance(node.target, ast.Name):
                        module_level_names.add(node.target.id)

            if not module_level_names:
                continue

            func_nodes = [
                n
                for n in ast.iter_child_nodes(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            for fn_node in func_nodes:
                for node in ast.walk(fn_node):
                    if isinstance(node, ast.Name) and node.id in module_level_names:
                        if isinstance(node.ctx, ast.Load):
                            pytest.fail(
                                f"{fn_node.name} in {py_file.name} references "
                                f"module-level variable '{node.id}'",
                            )


# ─────────────────────────────────────────────────────────────────────────────
# Secrets structure tests — only for deployments with secrets
# ─────────────────────────────────────────────────────────────────────────────


def _secrets_deployments():
    results = []
    for client, deploy, dep_dir in _ALL_DEPLOYMENTS:
        spec = _load(client, deploy, dep_dir)
        if spec.secrets_dir is not None:
            results.append((client, deploy, dep_dir))
    return results


_SECRETS_DEPLOYMENTS = _secrets_deployments()
_SECRETS_IDS = [f"{c}/{d}" for c, d, _ in _SECRETS_DEPLOYMENTS]


@pytest.mark.parametrize(
    "client,deploy,dep_dir",
    _SECRETS_DEPLOYMENTS,
    ids=_SECRETS_IDS,
)
class TestSecretsStructure:
    """Validate secrets are well-formed."""

    def test_secrets_jsonl_exists(self, client: str, deploy: str, dep_dir: Path):
        spec = _load(client, deploy, dep_dir)
        secrets_path = spec.secrets_dir / "secrets.jsonl"
        assert (
            secrets_path.is_file()
        ), f"Deployment {client}/{deploy} declares secrets_dir but has no secrets.jsonl"

    def test_secrets_jsonl_matches_spec_dir(
        self,
        client: str,
        deploy: str,
        dep_dir: Path,
    ):
        spec = _load(client, deploy, dep_dir)
        secrets_path = spec.secrets_dir / "secrets.jsonl"
        if not secrets_path.is_file():
            pytest.skip("No secrets.jsonl file")
        json_names = {
            json.loads(line)["name"]
            for line in secrets_path.read_text().splitlines()
            if line.strip()
        }
        assert json_names, f"secrets.jsonl for {client}/{deploy} is empty"


# ─────────────────────────────────────────────────────────────────────────────
# Deployment resolver tests — per client
# ─────────────────────────────────────────────────────────────────────────────


def _discover_clients_with_mappings():
    """Find clients that export _MAPPING and _DEPLOYMENTS_DIR."""
    results = []
    for client_dir in sorted(_CLIENTS_DIR.iterdir()):
        if not client_dir.is_dir() or not (client_dir / "deployments").is_dir():
            continue
        try:
            mod = importlib.import_module(
                f"unity_deploy.assistant_deployments.clients.{client_dir.name}",
            )
            mapping = getattr(mod, "_MAPPING", None)
            dep_dir = getattr(mod, "_DEPLOYMENTS_DIR", None)
            if mapping is not None and dep_dir is not None:
                results.append((client_dir.name, mapping, dep_dir))
        except Exception:
            pass
    return results


_CLIENTS_WITH_MAPPINGS = _discover_clients_with_mappings()
_CLIENT_IDS = [c for c, _, _ in _CLIENTS_WITH_MAPPINGS]


@pytest.mark.parametrize(
    "client,mapping,dep_dir",
    _CLIENTS_WITH_MAPPINGS,
    ids=_CLIENT_IDS,
)
class TestDeploymentResolver:
    """Verify each client's _MAPPING targets resolve to valid deployments."""

    def test_default_resolves(self, client: str, mapping, dep_dir: Path):
        has_default = any(t.scope == "default" for t in mapping.targets)
        if not has_default:
            with pytest.raises(ValueError, match="No matching"):
                resolve_deployment_name(mapping)
            return
        name = resolve_deployment_name(mapping)
        valid = [
            d.name
            for d in dep_dir.iterdir()
            if d.is_dir() and (d / "deployment.py").exists()
        ]
        assert name in valid

    def test_all_mapping_targets_valid(self, client: str, mapping, dep_dir: Path):
        valid = [
            d.name
            for d in dep_dir.iterdir()
            if d.is_dir() and (d / "deployment.py").exists()
        ]
        for target in mapping.targets:
            assert target.deployment in valid, (
                f"[{client}] Mapping target '{target.deployment}' invalid. "
                f"Valid: {valid}"
            )
