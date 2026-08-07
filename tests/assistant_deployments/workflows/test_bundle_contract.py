"""Every authored workflow bundle honours the authoring contract.

A bundle is **read before it is run**: the seed publishes its manifest and
every artifact's substance verbatim to the public-read Builtins project,
where Console renders it to someone deciding whether to install. So the
contract these pin is mostly about the writing — copy that explains itself,
params a human can answer, requirement slugs the gallery can actually
resolve — and about the identity rules that make an install reconcilable.

Parametrised over the shelf, so authoring a bundle is what puts it under
test. The rules are ``.agents/rules/workflow-bundle-authoring.md``; this
file is that rule made executable, and the two must move together.

Deliberately here rather than in unify. unify owns the *lifecycle* proof —
install, hold, arm, reconcile, uninstall against real managers — which
needs the managers. What this repo owns is the bundles themselves, and a
malformed one should fail in the repo that authored it, on a test that
needs no backend at all.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

WORKFLOWS_DIR = (
    Path(__file__).resolve().parents[3]
    / "unify_deploy"
    / "assistant_deployments"
    / "workflows"
)

# Console's own vocabulary for the shelf: `WorkflowCategory` in
# `src/types/workflows.ts` and the keys of `WORKFLOW_TILE_ICONS` in
# `src/components/Workflows/WorkflowTileIcon.tsx`. A value outside these
# renders an uncategorised card with the fallback glyph, which is a silent
# authoring mistake rather than a visible one.
CATEGORIES = {"comms", "growth", "ops", "build"}
ICON_IDS = {
    "briefing",
    "comet",
    "signal",
    "radar",
    "beam",
    "scope",
    "scales",
    "report",
    "funnel",
    "rocket",
    "pulsar",
    "notes",
}

PARAM_TYPES = {"text", "textarea", "number", "select", "boolean"}

# A provider app slug: lowercase, underscore-separated, the id space the
# integrations gallery keys on (`canonical_app_slug`, itself the slugified
# display name — "Google Drive" -> google_drive). An OAuth alias like
# `google` is valid upstream and invisible here.
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)*$")

# Anything that looks like a credential. Requirements are declared; secrets
# are never carried.
SECRET_HINTS = re.compile(
    r"(api[_-]?key|secret|password|token|credential|private[_-]?key)\s*[:=]\s*['\"][^'\"]{8,}",
    re.IGNORECASE,
)


def _bundle_dirs() -> List[Path]:
    if not WORKFLOWS_DIR.is_dir():
        return []
    return sorted(
        path
        for path in WORKFLOWS_DIR.iterdir()
        if path.is_dir() and (path / "manifest.yaml").exists()
    )


BUNDLES = _bundle_dirs()
SLUGS = [path.name for path in BUNDLES]

pytestmark = pytest.mark.skipif(not SLUGS, reason="no workflow bundles authored yet")


def _manifest(slug: str) -> Dict[str, Any]:
    return yaml.safe_load((WORKFLOWS_DIR / slug / "manifest.yaml").read_text()) or {}


def _jsonl(slug: str, surface: str) -> List[Dict[str, Any]]:
    path = WORKFLOWS_DIR / slug / surface / f"{surface}.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _sentences(text: str) -> int:
    return len(
        [part for part in re.split(r"[.!?](?:\s|$)", text.strip()) if part.strip()],
    )


@pytest.mark.parametrize("slug", SLUGS)
def test_identity_matches_the_directory(slug: str):
    """The slug is stamped as ``managed_by`` on every planted row, so a
    rename is an identity migration for every installation rather than a
    directory move. unify's loader refuses a mismatch; catching it here
    means the author sees it without running the other repo."""
    manifest = _manifest(slug)
    assert manifest.get("slug") == slug
    assert manifest.get("name"), "a bundle needs a human name for the card"


@pytest.mark.parametrize("slug", SLUGS)
def test_version_stays_below_one_until_the_staging_walk(slug: str):
    """1.0.0 means the staging acceptance walk proved the workflow's job
    actually runs end to end. Nothing else earns it, so a bundle that has
    not had that walk must not claim it."""
    version = str(_manifest(slug).get("version") or "")
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"{slug}: version must be semver"
    assert version.startswith("0."), (
        f"{slug}: version {version} claims a completed staging walk. Promote to "
        "1.0.0 only after proving the job runs end to end."
    )


@pytest.mark.parametrize("slug", SLUGS)
def test_the_card_and_the_page_are_both_written(slug: str):
    """``description`` is the card — one sentence. ``about`` is the page
    someone reads before installing, and the bar is that a new reader must
    not come away understanding nothing."""
    manifest = _manifest(slug)

    description = str(manifest.get("description") or "").strip()
    assert description, f"{slug}: no description"
    assert (
        _sentences(description) == 1
    ), f"{slug}: description is the card — one sentence"
    assert (
        len(description) <= 220
    ), f"{slug}: description reads as a paragraph, not a card"

    about = str(manifest.get("about") or "").strip()
    paragraphs = [part for part in about.split("\n\n") if part.strip()]
    assert len(paragraphs) >= 3, (
        f"{slug}: about has {len(paragraphs)} paragraph(s). It must cover what the "
        "workflow does, when it runs, what arrives, and how its settings shape it."
    )
    assert len(about) >= 600, f"{slug}: about is too thin to decide from"
    assert about != description


@pytest.mark.parametrize("slug", SLUGS)
def test_the_shelf_can_render_it(slug: str):
    manifest = _manifest(slug)
    assert manifest.get("category") in CATEGORIES, f"{slug}: unknown category"
    assert manifest.get("icon_id") in ICON_IDS, f"{slug}: unknown icon_id"


@pytest.mark.parametrize("slug", SLUGS)
def test_requirements_name_gallery_apps_and_carry_no_secrets(slug: str):
    """The resolver asks the gallery for a live connection first, so a
    gallery app's "not connected" means press Connect.

    Declaring ``required_secrets`` for one makes the UI offer a pasted
    credential instead of the connect button that actually works — the
    shipped Gmail bug. Only a BYOD provider with no gallery row and no
    native package (``kind: workspace``) has a secret as its signal.
    """
    for requirement in _manifest(slug).get("requirements") or []:
        assert isinstance(requirement, dict), f"{slug}: requirements are mappings"
        app = str(requirement.get("slug") or "")
        assert SLUG_PATTERN.fullmatch(app), (
            f"{slug}: requirement slug {app!r} is not a provider app slug. Use the "
            "gallery's id space (gmail, google_drive), never an OAuth alias."
        )
        assert requirement.get(
            "name",
        ), f"{slug}: requirement {app} needs a display name"
        if requirement.get("kind") == "workspace":
            continue
        assert not requirement.get("required_secrets"), (
            f"{slug}: requirement {app} declares required_secrets. A gallery app or "
            "native package answers for itself; restating its secrets here offers "
            "the wrong fix and is a second place to update."
        )


@pytest.mark.parametrize("slug", SLUGS)
def test_every_param_explains_itself(slug: str):
    """A param whose meaning needs the manifest comments to decode is not
    done: the schema is rendered as a form to someone who has never seen
    the bundle."""
    for name, spec in (_manifest(slug).get("params_schema") or {}).items():
        assert isinstance(spec, dict), f"{slug}: param {name} must be a mapping"
        assert spec.get(
            "label",
        ), f"{slug}: param {name} needs a label a human would say"
        assert (
            spec.get("type") in PARAM_TYPES
        ), f"{slug}: param {name} has an unknown type"
        assert isinstance(
            spec.get("required"),
            bool,
        ), f"{slug}: param {name} needs `required`"
        help_text = str(spec.get("help") or "").strip()
        assert len(help_text) >= 60, (
            f"{slug}: param {name} has no real help. Give an example and say what "
            "happens when it is left empty."
        )
        if not spec.get("required"):
            assert re.search(
                r"left empty|by default|defaults to",
                help_text,
                re.IGNORECASE,
            ), f"{slug}: optional param {name} must say what happens when it is omitted"


@pytest.mark.parametrize("slug", SLUGS)
def test_it_actually_sets_something_up(slug: str):
    """A workflow with no recurring job sets nothing up — it is a folder of
    prose. The task is the thing that makes the workflow a workflow."""
    tasks = _jsonl(slug, "tasks")
    assert tasks, f"{slug}: plants no task"
    for task in tasks:
        assert task.get("name"), f"{slug}: a task with no name"
        description = str(task.get("description") or "")
        assert len(description) >= 80, (
            f"{slug}: task {task.get('key')!r} has a thin brief. It is the whole "
            "instruction the assistant runs on."
        )
        assert (
            task.get("repeat") or task.get("trigger") or task.get("schedule")
        ), f"{slug}: task {task.get('key')!r} never fires"


@pytest.mark.parametrize("slug", SLUGS)
def test_content_keys_are_namespaced_by_the_slug(slug: str):
    """``custom_key`` is the identity a reconcile diffs on. Namespacing by
    slug keeps two bundles' entries distinct in the same context and makes
    a stray row traceable to the bundle that planted it."""
    for surface in ("guidance", "knowledge", "tasks"):
        entries = _jsonl(slug, surface)
        keys = [str(entry.get("key") or "") for entry in entries]
        assert all(keys), f"{slug}: an entry in {surface} has no key"
        assert len(set(keys)) == len(keys), f"{slug}: duplicate key in {surface}"
        for key in keys:
            assert key.startswith(
                f"{slug}/",
            ), f"{slug}: {surface} key {key!r} is not namespaced by the slug"


@pytest.mark.parametrize("slug", SLUGS)
def test_artifact_prose_is_finished_writing(slug: str):
    """Guidance and knowledge bodies are published to the shelf and shown
    in Console's previews, so they are documents rather than notes."""
    for surface in ("guidance", "knowledge"):
        for entry in _jsonl(slug, surface):
            title = str(entry.get("title") or "").strip()
            content = str(entry.get("content") or "").strip()
            assert title, f"{slug}: an entry in {surface} has no title"
            assert len(title) <= 200, f"{slug}: {surface} title is too long to render"
            assert len(content) >= 120, (
                f"{slug}: {surface} entry {entry.get('key')!r} is a stub, and the "
                "shelf renders it verbatim"
            )


@pytest.mark.parametrize("slug", SLUGS)
def test_knowledge_claims_are_typed(slug: str):
    """A claim carries its kind and topics — that is what makes it a claim
    rather than a note, and what a later search filters on."""
    for entry in _jsonl(slug, "knowledge"):
        assert entry.get("kind"), f"{slug}: claim {entry.get('key')!r} has no kind"
        topics = entry.get("topics") or []
        assert topics, f"{slug}: claim {entry.get('key')!r} has no topics"
        assert (
            slug in topics
        ), f"{slug}: claim {entry.get('key')!r} does not carry its own slug as a topic"


@pytest.mark.parametrize("slug", SLUGS)
def test_functions_are_documented_and_self_contained(slug: str):
    """Function docstrings are published to the shelf, and the runtime
    execs each function in an isolated namespace — so module-level imports
    are not there when it runs."""
    functions_dir = WORKFLOWS_DIR / slug / "functions"
    if not functions_dir.is_dir():
        return
    for source_file in sorted(functions_dir.glob("*.py")):
        tree = ast.parse(source_file.read_text())
        module_imports = {
            alias.asname or alias.name.split(".")[0]
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        } - {"annotations", "custom_function"}

        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        assert functions, f"{slug}/{source_file.name}: no functions"
        for function in functions:
            doc = ast.get_docstring(function)
            assert (
                doc
            ), f"{slug}: {function.name} has no docstring, and the shelf shows it"
            assert (
                "Parameters" in doc and "Returns" in doc
            ), f"{slug}: {function.name} documents no parameters or return value"
            used = {
                node.names[0].name.split(".")[0]
                for node in ast.walk(function)
                if isinstance(node, ast.Import)
            } | {
                (node.module or "").split(".")[0]
                for node in ast.walk(function)
                if isinstance(node, ast.ImportFrom)
            }
            assert not module_imports or used, (
                f"{slug}: {function.name} relies on module-level imports; FunctionManager "
                "execs it in an isolated namespace, so imports belong in the body"
            )


@pytest.mark.parametrize("slug", SLUGS)
def test_no_bundle_carries_a_credential(slug: str):
    """Requirements are declared, never carried. A bundle is published to a
    public-read project, so a pasted key would be published with it."""
    for path in sorted((WORKFLOWS_DIR / slug).rglob("*")):
        if not path.is_file() or path.suffix not in {".yaml", ".yml", ".jsonl", ".py"}:
            continue
        match = SECRET_HINTS.search(path.read_text())
        assert (
            not match
        ), f"{slug}: {path.name} looks like it carries a credential: {match.group(0)[:40]}"


def test_the_shelf_loads_through_the_real_loader():
    """unify's loader is strict, and a malformed bundle raises there rather
    than vanishing from the shelf. Running it here means the author finds
    out in the repo they are editing."""
    from unify.workflow_manager.catalog import load_catalog

    bundles = load_catalog(WORKFLOWS_DIR)
    assert [bundle.slug for bundle in bundles] == SLUGS
    for bundle in bundles:
        assert bundle.surfaces, f"{bundle.slug}: loads with no content at all"
