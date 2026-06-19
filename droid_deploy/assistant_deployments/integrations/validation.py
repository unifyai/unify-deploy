"""Structural validation for integration packages.

Checks that the files referenced by a manifest actually exist on disk
and that cross-references (functions in capabilities, secret placeholders
in MCP env) are consistent. The same checks apply to generic packages,
client-specific real packages, and scenario-only mock packages.
"""

from __future__ import annotations

import re
from pathlib import Path

from droid_deploy.assistant_deployments.integrations.types import IntegrationManifest

_SECRET_PLACEHOLDER_RE = re.compile(r"\$\{(\w+)\}")


def validate_integration(manifest: IntegrationManifest, root: Path) -> list[str]:
    """Validate an integration package against its manifest.

    Returns a list of error strings (empty means valid).
    """
    errors: list[str] = []

    if not root.is_dir():
        errors.append(f"Integration root does not exist: {root}")
        return errors

    if root.name != manifest.slug:
        errors.append(
            f"Directory name '{root.name}' does not match manifest slug '{manifest.slug}'",
        )

    init_file = root / "__init__.py"
    if not init_file.is_file():
        errors.append(f"Missing required __init__.py in {root}")

    _validate_functions(manifest, root, errors)
    _validate_guidance(manifest, root, errors)
    _validate_scenarios(manifest, root, errors)
    _validate_demo_site(manifest, root, errors)
    _validate_mcp_secrets(manifest, errors)

    return errors


def _validate_functions(
    manifest: IntegrationManifest,
    root: Path,
    errors: list[str],
) -> None:
    """Check that function names referenced in capabilities have backing files."""
    functions_dir = root / "functions"
    declared_functions: set[str] = set()
    for cap in manifest.capabilities:
        declared_functions.update(cap.functions)

    if declared_functions and not functions_dir.is_dir():
        errors.append(
            f"Capabilities reference functions but functions/ dir is missing in {root}",
        )
        return

    if functions_dir.is_dir():
        available_py = {
            f.stem for f in functions_dir.glob("*.py") if f.name != "__init__.py"
        }
        available_names: set[str] = set()
        for py_file in functions_dir.glob("*.py"):
            if py_file.name == "__init__.py":
                continue
            try:
                content = py_file.read_text()
            except OSError:
                continue
            for match in re.finditer(r"(?:async\s+)?def\s+(\w+)\s*\(", content):
                available_names.add(match.group(1))
        available_names.update(available_py)

        for fn_name in declared_functions:
            if fn_name not in available_names:
                errors.append(
                    f"Capability references function '{fn_name}' but it was not found "
                    f"in {functions_dir}",
                )


def _validate_guidance(
    manifest: IntegrationManifest,
    root: Path,
    errors: list[str],
) -> None:
    """Check that guidance file stems referenced in capabilities exist."""
    guidance_dir = root / "guidance"
    declared_guidance: set[str] = set()
    for cap in manifest.capabilities:
        declared_guidance.update(cap.guidance)

    if declared_guidance and not guidance_dir.is_dir():
        errors.append(
            f"Capabilities reference guidance but guidance/ dir is missing in {root}",
        )
        return

    if guidance_dir.is_dir() and declared_guidance:
        available_stems = {f.stem for f in guidance_dir.glob("*.md")}
        for stem in declared_guidance:
            if stem not in available_stems:
                errors.append(
                    f"Capability references guidance '{stem}' but "
                    f"'{stem}.md' not found in {guidance_dir}",
                )


def _validate_demo_site(
    manifest: IntegrationManifest,
    root: Path,
    errors: list[str],
) -> None:
    """Check that the declared demo site directory exists."""
    if manifest.demo_site is not None:
        demo_dir = root / manifest.demo_site.dir
        if not demo_dir.is_dir():
            errors.append(
                f"demo_site.dir='{manifest.demo_site.dir}' declared but "
                f"directory not found at {demo_dir}",
            )


def _validate_scenarios(
    manifest: IntegrationManifest,
    root: Path,
    errors: list[str],
) -> None:
    """Check that scenario files declared by the manifest exist."""
    if not manifest.scenarios:
        return

    scenarios_dir = root / "scenarios"
    if not scenarios_dir.is_dir():
        errors.append(
            f"Manifest declares scenarios but scenarios/ dir is missing in {root}",
        )
        return

    for scenario_file in manifest.scenarios:
        if not (scenarios_dir / scenario_file).is_file():
            errors.append(
                f"Manifest references scenario '{scenario_file}' but it was not "
                f"found in {scenarios_dir}",
            )


def _validate_mcp_secrets(
    manifest: IntegrationManifest,
    errors: list[str],
) -> None:
    """Check that ${SECRET} placeholders in MCP env reference declared secrets."""
    if manifest.mcp is None:
        return

    declared_names = {s.name for s in manifest.secrets}
    for key, value in manifest.mcp.env.items():
        for match in _SECRET_PLACEHOLDER_RE.finditer(value):
            secret_name = match.group(1)
            if secret_name not in declared_names:
                errors.append(
                    f"MCP env var '{key}' references ${{{secret_name}}} "
                    f"but it is not declared in manifest.secrets",
                )
