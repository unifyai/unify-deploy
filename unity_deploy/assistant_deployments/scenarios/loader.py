"""Load scenario specs from integration packages or standalone YAML files.

Scenario discovery mirrors the integration roots: generic ``packages/`` and
private ``client_packages/`` are searched by default, and opt-in
``mock_packages/`` is only searched when the caller explicitly asks for it.
This keeps deploy-time resolution hermetic while still letting scenario E2Es
opt into mock-rooted scenarios through ``include_mock_packages=True``.

Also hosts :func:`materialise_scenario_activations`, which expands
:class:`~unity_deploy.assistant_deployments.scenarios.types.ScenarioActivation`
declarations from a deployment / seed-layer into concrete
:class:`ScenarioSpec` instances by substituting placeholder fields in
generic templates.  This eliminates the need for per-client wrapper
packages whose only purpose is to copy a template and fill in
``assistant_id`` / ``client`` / ``deployment``.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path
from typing import Iterable

import yaml
from unity.secret_manager.types import Secret
from unity_deploy.assistant_deployments.integrations.discovery import (
    _BUILTIN_DIR,
    _CLIENT_DIR,
    _MOCK_DIR,
)
from unity_deploy.assistant_deployments.scenarios.types import (
    ScenarioActivation,
    ScenarioSpec,
)

logger = logging.getLogger(__name__)

_SCENARIOS_DIR = "scenarios"
_REPLACE_ME = "REPLACE_ME"
_PLACEHOLDER_TASK_ID_PREFIX = "REPLACE_ME"


def load_scenario(path: Path | str) -> ScenarioSpec:
    """Load a single scenario YAML file."""
    scenario_path = Path(path)
    raw = yaml.safe_load(scenario_path.read_text()) or {}
    return ScenarioSpec.model_validate(raw)


def load_scenarios(root: Path | str) -> list[ScenarioSpec]:
    """Load every scenario YAML file under an integration package root."""
    scenarios_dir = Path(root) / _SCENARIOS_DIR
    if not scenarios_dir.is_dir():
        return []

    specs: list[ScenarioSpec] = []
    for file in sorted([*scenarios_dir.glob("*.yaml"), *scenarios_dir.glob("*.yml")]):
        specs.append(load_scenario(file))
    return specs


def find_integration_root(
    slug: str,
    *,
    include_mock_packages: bool = False,
    search_paths: list[Path] | None = None,
) -> Path | None:
    """Return the first integration package root matching *slug*.

    The default search order is generic ``packages/`` then private
    ``client_packages/``. Mock packages are only consulted when
    ``include_mock_packages=True``.
    """
    paths = list(search_paths or [_BUILTIN_DIR, _CLIENT_DIR])
    if include_mock_packages and _MOCK_DIR not in paths:
        paths.append(_MOCK_DIR)

    for base in paths:
        candidate = base / slug
        if (candidate / "manifest.yaml").is_file():
            return candidate
    return None


def load_scenarios_from_integration(
    slug: str,
    *,
    include_mock_packages: bool = False,
    search_paths: list[Path] | None = None,
) -> list[ScenarioSpec]:
    """Load scenario specs bundled with an integration package.

    Mock-rooted scenarios are only returned when ``include_mock_packages``
    is true, matching deploy-time activation behavior.
    """
    root = find_integration_root(
        slug,
        include_mock_packages=include_mock_packages,
        search_paths=search_paths,
    )
    if root is None:
        return []
    return load_scenarios(root)


# ---------------------------------------------------------------------------
# Scenario activation — template instantiation at deploy-resolution time
# ---------------------------------------------------------------------------


def find_scenario_template(
    template_id: str,
    *,
    search_paths: list[Path] | None = None,
    include_mock_packages: bool = False,
) -> Path | None:
    """Resolve ``<package_slug>/<filename_stem>`` to an on-disk YAML path.

    Searches ``packages/``, ``client_packages/``, and (optionally)
    ``mock_packages/`` in that order.  Returns ``None`` if no match.
    """
    if "/" not in template_id:
        return None
    slug, _, stem = template_id.partition("/")
    if not slug or not stem:
        return None

    paths = list(search_paths or [_BUILTIN_DIR, _CLIENT_DIR])
    if include_mock_packages and _MOCK_DIR not in paths:
        paths.append(_MOCK_DIR)

    for base in paths:
        for ext in (".yaml", ".yml"):
            candidate = base / slug / _SCENARIOS_DIR / f"{stem}{ext}"
            if candidate.is_file():
                return candidate
    return None


def materialise_scenario_activations(
    activations: Iterable[ScenarioActivation],
    *,
    client_slug: str,
    deployment_name: str,
    search_paths: list[Path] | None = None,
    include_mock_packages: bool = False,
) -> tuple[list[ScenarioSpec], list[Secret]]:
    """Materialise activations into concrete scenarios + env-var overlay.

    Each :class:`ScenarioActivation` names a generic scenario template and
    a set of substitutions.  This function:

    1. Resolves the template path via :func:`find_scenario_template`.
    2. Loads + deep-copies the template YAML.
    3. Substitutes ``client``, ``deployment``, ``scenario_id`` (default-derived
       from ``client + template_stem`` unless overridden), every
       ``tasks[*].target.assistant_id``, every ``tasks[*].target.scenario_id``
       (kept consistent with the substituted ``scenario_id``), every
       ``tasks[*].enabled``, and (when present) every
       ``tasks[*].activation.task_description``.  Empty strings and any
       value starting with ``REPLACE_ME`` are treated as placeholders.
    4. Rejects any leftover ``REPLACE_ME`` tokens with a clear error
       pointing at the field path.
    5. Validates the substituted dict as :class:`ScenarioSpec`.
    6. Builds an env-var overlay from ``object_intervals_override`` and
       ``config_overrides``, returned as a list of :class:`Secret`
       entries the caller merges into the resolved assistant deployment.

    Returns
    -------
    tuple[list[ScenarioSpec], list[Secret]]
        Concrete scenarios ready to feed into the offline task lane,
        and a list of env-var Secrets to merge into the resolved
        assistant deployment's secrets bundle.

    Raises
    ------
    FileNotFoundError
        When a referenced template cannot be located on disk.
    ValueError
        When an activation produces a duplicate ``scenario_id`` against
        another activation in the same call, or when leftover
        ``REPLACE_ME`` tokens remain after substitution, or when the
        substituted scenario fails :class:`ScenarioSpec` validation.
    """
    concrete: list[ScenarioSpec] = []
    secrets: list[Secret] = []
    seen_scenario_ids: set[str] = set()

    for activation in activations:
        slug, _, stem = activation.scenario_template.partition("/")
        template_path = find_scenario_template(
            activation.scenario_template,
            search_paths=search_paths,
            include_mock_packages=include_mock_packages,
        )
        if template_path is None:
            roots = [str(p) for p in (search_paths or [_BUILTIN_DIR, _CLIENT_DIR])]
            if include_mock_packages:
                roots.append(str(_MOCK_DIR))
            raise FileNotFoundError(
                f"ScenarioActivation template "
                f"{activation.scenario_template!r} not found.  Looked under "
                f"<root>/{slug}/scenarios/{stem}.{{yaml,yml}} for roots: "
                f"{roots}",
            )

        raw = yaml.safe_load(template_path.read_text()) or {}
        if not isinstance(raw, dict):
            raise ValueError(
                f"Template {template_path} is not a YAML mapping at the root.",
            )
        materialised = deepcopy(raw)

        client = activation.client_override or client_slug
        deployment = activation.deployment_override or deployment_name
        scenario_id = activation.scenario_id_override or f"{client}_{stem}"

        if scenario_id in seen_scenario_ids:
            raise ValueError(
                f"Duplicate scenario_id {scenario_id!r} after activation of "
                f"template {activation.scenario_template!r} for "
                f"{client}/{deployment}.  Add scenario_id_override to "
                f"disambiguate when activating the same template multiple "
                f"times in one call.",
            )
        seen_scenario_ids.add(scenario_id)

        # Top-level substitutions.
        materialised["scenario_id"] = scenario_id
        materialised["client"] = client
        materialised["deployment"] = deployment

        # Per-task substitutions.  Iterate every task entry — templates
        # may declare multiple tasks for the same scenario.
        default_task_description = (
            activation.task_description_override
            or f"Scheduled {slug} sync for {client}/{deployment}."
        )
        for idx, task in enumerate(materialised.get("tasks", [])):
            if not isinstance(task, dict):
                continue
            target = task.setdefault("target", {})
            target["assistant_id"] = activation.assistant_id
            # ScenarioSpec's validator requires task.target.scenario_id ==
            # spec.scenario_id, so always substitute regardless of the
            # template's literal value.
            target["scenario_id"] = scenario_id

            task["enabled"] = activation.tasks_enabled

            # Synthesise per-activation task ids when the template uses
            # a placeholder.  Otherwise leave the template's id alone —
            # multiple activations of the same template under different
            # scenario_ids won't collide because tasks are scoped to
            # their owning scenario.
            current_id = task.get("id", "")
            if not isinstance(current_id, str) or _is_placeholder(current_id):
                task["id"] = (
                    f"{scenario_id}_tick_{idx}" if idx else f"{scenario_id}_tick"
                )

            activation_block = task.setdefault("activation", {})
            current_desc = activation_block.get("task_description", "")
            if (
                activation.task_description_override is not None
                or not isinstance(current_desc, str)
                or _is_placeholder(current_desc)
            ):
                activation_block["task_description"] = default_task_description

        # Reject any leftover REPLACE_ME tokens.  Empty-string placeholders
        # at known fields have already been substituted; anything still
        # carrying REPLACE_ME is an unhandled placeholder.
        leaks = _find_replace_me(materialised)
        if leaks:
            raise ValueError(
                f"Activation of {activation.scenario_template!r} for "
                f"{client}/{deployment} left REPLACE_ME tokens "
                f"unsubstituted at:\n  - "
                + "\n  - ".join(leaks)
                + "\nAdd the corresponding override field on "
                "ScenarioActivation, or update the platform template.",
            )

        try:
            spec = ScenarioSpec.model_validate(materialised)
        except Exception as e:  # noqa: BLE001
            raise ValueError(
                f"Activation of {activation.scenario_template!r} for "
                f"{client}/{deployment} produced an invalid ScenarioSpec: "
                f"{e}",
            ) from e
        concrete.append(spec)

        # Env overlay — surfaced via Secret entries the caller merges
        # into the resolved assistant deployment's secrets bundle.
        if activation.object_intervals_override:
            env_name = f"{slug.upper()}_SYNC_OBJECT_INTERVALS"
            value = ",".join(
                f"{k}:{int(v)}" for k, v in activation.object_intervals_override.items()
            )
            secrets.append(
                Secret(
                    name=env_name,
                    value=value,
                    description=(
                        f"Per-object sync cadence for {slug}, materialised "
                        f"from ScenarioActivation.object_intervals_override "
                        f"of {scenario_id}."
                    ),
                ),
            )
        for env_name, env_value in (activation.config_overrides or {}).items():
            secrets.append(
                Secret(
                    name=env_name,
                    value=str(env_value),
                    description=(
                        f"Config override from ScenarioActivation."
                        f"config_overrides of {scenario_id}."
                    ),
                ),
            )

    return concrete, secrets


def _is_placeholder(value: str) -> bool:
    """A field is a placeholder if it's empty or starts with REPLACE_ME."""
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    return not stripped or stripped.startswith(_REPLACE_ME)


def _find_replace_me(node, path: list[str] | None = None) -> list[str]:
    """Walk a nested dict/list and return paths where REPLACE_ME leaks."""
    path = path or []
    leaks: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            leaks.extend(_find_replace_me(v, path + [str(k)]))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            leaks.extend(_find_replace_me(v, path + [f"[{i}]"]))
    elif isinstance(node, str) and _REPLACE_ME in node:
        leaks.append(".".join(path) + f" = {node!r}")
    return leaks
