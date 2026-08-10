"""Every canvas a bundle ships compiles, at curation time.

A workflow's view is real TypeScript, and it is compiled at *install* —
against whichever canvas kit the deployment has — precisely so a shipped
view never pins a host runtime. The cost of that choice is that a broken
view would otherwise first fail on somebody's install, at which point the
author is not in the room.

So this is the gate that moves the failure back to the pull request:
lint, typecheck and bundle each shipped ``view.tsx`` here, where the
person who wrote it is the person who sees the compiler's words.

Lint runs everywhere. The bundle stage needs the node workspace (esbuild,
typescript, ``@unity/canvas-kit``) and skips without it, so a checkout
with no toolchain reports honestly rather than passing vacuously — the
same probe the canvas manager's own tests use.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Tuple

import pytest

WORKFLOWS_DIR = (
    Path(__file__).resolve().parents[3]
    / "unify_deploy"
    / "assistant_deployments"
    / "workflows"
)


def _shipped_views() -> List[Tuple[str, str, Path]]:
    """Every ``(slug, view name, source path)`` on the shelf."""
    if not WORKFLOWS_DIR.is_dir():
        return []
    found: List[Tuple[str, str, Path]] = []
    for bundle in sorted(WORKFLOWS_DIR.iterdir()):
        canvas_dir = bundle / "canvas"
        if not bundle.is_dir() or not canvas_dir.is_dir():
            continue
        for view_dir in sorted(canvas_dir.iterdir()):
            source = view_dir / "view.tsx"
            if source.is_file():
                found.append((bundle.name, view_dir.name, source))
    return found


VIEWS = _shipped_views()
VIEW_IDS = [f"{slug}/{name}" for slug, name, _ in VIEWS]

pytestmark = pytest.mark.skipif(
    not VIEWS,
    reason="no bundle ships a canvas yet",
)


@pytest.mark.parametrize(("slug", "name", "source"), VIEWS, ids=VIEW_IDS)
def test_a_shipped_view_lints(slug: str, name: str, source: Path):
    """The colour rule in particular.

    Authored TSX never passes through Console's lint-staged or its
    production build, so this is the only place it is applied — and an
    off-palette canvas looks correct in one theme and wrong in the other,
    which nobody notices until a user is in the wrong one.
    """
    from unify.canvas_manager.ops.build_ops import lint_source

    problems = lint_source(source.read_text())
    assert not problems, f"{slug}/{name}: " + "; ".join(problems)


@pytest.mark.parametrize(("slug", "name", "source"), VIEWS, ids=VIEW_IDS)
def test_a_shipped_view_typechecks_and_bundles(slug: str, name: str, source: Path):
    from unify.canvas_manager.ops.build_ops import build_canvas, toolchain_available

    if not toolchain_available():
        pytest.skip("canvas toolchain unavailable in this environment")

    report, bundle = build_canvas(source.read_text())
    assert report.ok, f"{slug}/{name} failed at {report.failed_stage}: " + "; ".join(
        report.diagnostics,
    )
    assert bundle, f"{slug}/{name} compiled to nothing"


@pytest.mark.parametrize(("slug", "name", "source"), VIEWS, ids=VIEW_IDS)
def test_a_shipped_view_declares_what_it_needs(slug: str, name: str, source: Path):
    """The manifest beside the source is what the install publishes under.

    A view with no title has nothing to be listed as, and its bindings are
    the whole statement of what it may read — both are decisions for the
    author, not defaults for the installer.
    """
    manifest_path = source.parent / "view.json"
    assert manifest_path.is_file(), f"{slug}/{name}: no view.json"
    manifest = json.loads(manifest_path.read_text())

    assert str(manifest.get("title") or "").strip(), f"{slug}/{name}: no title"
    assert isinstance(manifest.get("bindings", []), list)
    assert isinstance(manifest.get("actions", []), list)
    assert manifest.get("visibility", "private") in {"private", "shared", "public"}

    # Bindings are validated here, not merely shaped. A plausible-looking
    # `{"name": ..., "context": ...}` parses as JSON and fails only when the
    # install tries to publish, at which point the author is not in the room
    # — which is exactly how the first version of this view was written.
    from unify.canvas_manager.ops import binding_ops

    binding_ops.coerce_bindings(manifest.get("bindings") or [])


@pytest.mark.parametrize(("slug", "name", "source"), VIEWS, ids=VIEW_IDS)
def test_a_shipped_view_carries_no_built_artifact(slug: str, name: str, source: Path):
    """A compiled bundle in a git bundle pins a host runtime.

    The canvas host serves versioned runtimes, so a view built against one
    kit and planted into another breaks at *view* time, for the user, with
    nothing failing at plant time to warn anyone. The loader refuses these
    too; catching it here names the file.
    """
    for built in ("bundle.js", "bundle.mjs", "view.js"):
        assert not (
            source.parent / built
        ).exists(), f"{slug}/{name} ships a built {built}; ship source instead"
