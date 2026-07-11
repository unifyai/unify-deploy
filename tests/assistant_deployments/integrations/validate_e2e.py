#!/usr/bin/env python3
"""End-to-end validation script for the integration framework.

Runs through the full pipeline — discovery, loading, validation, function
invocation — and reports pass/fail for each step.  Not a pytest file;
intended for manual pre-commit validation.

Usage:
    .venv/bin/python tests/assistant_deployments/integrations/validate_e2e.py
    .venv/bin/python tests/assistant_deployments/integrations/validate_e2e.py --real   # include real API calls
"""

from __future__ import annotations

import asyncio
import argparse
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from unify_deploy.assistant_deployments.integrations.discovery import (
    _BUILTIN_DIR,
    _load_manifest,
    discover_integrations,
)
from unify_deploy.assistant_deployments.integrations.loader import load_integration
from unify.guidance_manager.custom_guidance import collect_custom_guidance
from unify_deploy.assistant_deployments.integrations.validation import (
    validate_integration,
)


class ValidationRunner:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def check(self, label: str, fn, *args, **kwargs) -> object | None:
        try:
            result = fn(*args, **kwargs)
            self.passed += 1
            print(f"  PASS  {label}")
            return result
        except Exception as e:
            self.failed += 1
            print(f"  FAIL  {label}")
            print(f"        {e}")
            traceback.print_exc(limit=2)
            return None

    async def check_async(self, label: str, fn, *args, **kwargs) -> object | None:
        try:
            result = await fn(*args, **kwargs)
            self.passed += 1
            print(f"  PASS  {label}")
            return result
        except Exception as e:
            self.failed += 1
            print(f"  FAIL  {label}")
            print(f"        {e}")
            traceback.print_exc(limit=2)
            return None

    def skip(self, label: str, reason: str) -> None:
        self.skipped += 1
        print(f"  SKIP  {label} ({reason})")

    def summary(self) -> int:
        total = self.passed + self.failed + self.skipped
        print(f"\n{'=' * 60}")
        print(
            f"Results: {self.passed} passed, {self.failed} failed, {self.skipped} skipped (total {total})",
        )
        if self.failed:
            print("VALIDATION FAILED")
            return 1
        print("ALL CHECKS PASSED")
        return 0


async def run_validation(include_real: bool = False) -> int:
    runner = ValidationRunner()

    # ---------------------------------------------------------------
    # Step 1: Discovery
    # ---------------------------------------------------------------
    print("\n--- Step 1: Discovery ---")

    manifests = runner.check(
        "discover_integrations() finds default package roots",
        discover_integrations,
    )
    if manifests is not None:
        slugs = {m.slug for m in manifests}
        runner.check(
            f"Found github integration (slugs: {slugs})",
            lambda: (
                None
                if "github" in slugs
                else (_ for _ in ()).throw(
                    AssertionError("'github' not in discovered slugs"),
                )
            ),
        )
        runner.check(
            f"Found fetch_mcp integration (slugs: {slugs})",
            lambda: (
                None
                if "fetch_mcp" in slugs
                else (_ for _ in ()).throw(
                    AssertionError("'fetch_mcp' not in discovered slugs"),
                )
            ),
        )

    # ---------------------------------------------------------------
    # Step 2: Manifest parsing per package
    # ---------------------------------------------------------------
    print("\n--- Step 2: Manifest Parsing ---")

    for slug in ["github", "fetch_mcp"]:
        manifest_path = _BUILTIN_DIR / slug / "manifest.yaml"
        manifest = runner.check(
            f"{slug}: manifest parses",
            _load_manifest,
            manifest_path,
        )
        if manifest is not None:
            runner.check(
                f"{slug}: slug matches directory name",
                lambda m=manifest, s=slug: (
                    None
                    if m.slug == s
                    else (_ for _ in ()).throw(
                        AssertionError(f"slug '{m.slug}' != '{s}'"),
                    )
                ),
            )

    # ---------------------------------------------------------------
    # Step 3: Validation
    # ---------------------------------------------------------------
    print("\n--- Step 3: Structural Validation ---")

    for slug in ["github", "fetch_mcp"]:
        root = _BUILTIN_DIR / slug
        manifest = _load_manifest(root / "manifest.yaml")
        errors = runner.check(
            f"{slug}: validate_integration returns no errors",
            validate_integration,
            manifest,
            root,
        )
        if errors:
            print(f"        Errors: {errors}")

    # ---------------------------------------------------------------
    # Step 4: Loading
    # ---------------------------------------------------------------
    print("\n--- Step 4: Loading ---")

    root = _BUILTIN_DIR / "github"
    manifest = _load_manifest(root / "manifest.yaml")
    loaded = runner.check(
        "github: load_integration succeeds",
        load_integration,
        manifest,
        root,
    )
    if loaded is not None:
        runner.check(
            "github: guidance entries are non-empty",
            lambda l=loaded: (
                None
                if l.guidance_dir and collect_custom_guidance(path=l.guidance_dir)
                else (_ for _ in ()).throw(
                    AssertionError("No guidance entries loaded"),
                )
            ),
        )
        runner.check(
            "github: function_dir found",
            lambda l=loaded: (
                None
                if l.function_dir
                else (_ for _ in ()).throw(
                    AssertionError("No function_dir"),
                )
            ),
        )
        runner.check(
            "github: secret entries present",
            lambda l=loaded: (
                None
                if l.secret_entries
                else (_ for _ in ()).throw(
                    AssertionError("No secret entries"),
                )
            ),
        )

    root = _BUILTIN_DIR / "fetch_mcp"
    manifest = _load_manifest(root / "manifest.yaml")
    loaded_mcp = runner.check(
        "fetch_mcp: load_integration succeeds",
        load_integration,
        manifest,
        root,
    )
    if loaded_mcp is not None:
        runner.check(
            "fetch_mcp: guidance entries are non-empty",
            lambda l=loaded_mcp: (
                None
                if l.guidance_dir and collect_custom_guidance(path=l.guidance_dir)
                else (_ for _ in ()).throw(
                    AssertionError("No guidance entries loaded"),
                )
            ),
        )
        runner.check(
            "fetch_mcp: mcp_config present",
            lambda l=loaded_mcp: (
                None
                if l.mcp_config
                else (_ for _ in ()).throw(
                    AssertionError("No mcp_config"),
                )
            ),
        )
        runner.check(
            "fetch_mcp: no function_dir (MCP tier)",
            lambda l=loaded_mcp: (
                None
                if l.function_dir is None
                else (_ for _ in ()).throw(
                    AssertionError("MCP tier should not have function_dir"),
                )
            ),
        )

    # ---------------------------------------------------------------
    # Step 5: Function calls (mock mode)
    # ---------------------------------------------------------------
    print("\n--- Step 5: Mock Function Calls ---")

    from unify_deploy.assistant_deployments.integrations.packages.github.functions.users import (
        get_user,
        get_user_repos,
    )
    from unify_deploy.assistant_deployments.integrations.packages.github.functions.repos import (
        get_repo,
        search_repos,
    )
    from unify_deploy.assistant_deployments.integrations.packages.github.functions.issues import (
        get_repo_issues,
    )

    result = await runner.check_async(
        "get_user('octocat', mock=True)",
        get_user,
        "octocat",
        mock=True,
    )
    if result:
        runner.check(
            "  -> returned login field",
            lambda: (
                None
                if result.get("login") == "octocat"
                else (_ for _ in ()).throw(
                    AssertionError(
                        f"Expected login='octocat', got {result.get('login')}",
                    ),
                )
            ),
        )

    result = await runner.check_async(
        "get_user_repos('octocat', mock=True)",
        get_user_repos,
        "octocat",
        mock=True,
    )
    if result:
        runner.check(
            "  -> returned repos list",
            lambda: (
                None
                if isinstance(result.get("repos"), list)
                else (_ for _ in ()).throw(
                    AssertionError("Expected repos to be a list"),
                )
            ),
        )

    await runner.check_async(
        "get_repo('octocat', 'Hello-World', mock=True)",
        get_repo,
        "octocat",
        "Hello-World",
        mock=True,
    )

    await runner.check_async(
        "search_repos('language:python', mock=True)",
        search_repos,
        "language:python",
        mock=True,
    )

    await runner.check_async(
        "get_repo_issues('octocat', 'Hello-World', mock=True)",
        get_repo_issues,
        "octocat",
        "Hello-World",
        mock=True,
    )

    # ---------------------------------------------------------------
    # Step 6: Real API calls (optional)
    # ---------------------------------------------------------------
    if include_real:
        print("\n--- Step 6: Real API Calls (api.github.com) ---")

        result = await runner.check_async(
            "get_user('octocat', mock=False) -> real GitHub API",
            get_user,
            "octocat",
            mock=False,
        )
        if result and "login" in result:
            runner.check(
                "  -> login matches 'octocat'",
                lambda: (
                    None
                    if result["login"] == "octocat"
                    else (_ for _ in ()).throw(
                        AssertionError(f"Expected 'octocat', got {result['login']}"),
                    )
                ),
            )

        result = await runner.check_async(
            "get_user_repos('torvalds', mock=False) -> real GitHub API",
            get_user_repos,
            "torvalds",
            mock=False,
        )
        if result:
            runner.check(
                "  -> returned non-empty repos",
                lambda: (
                    None
                    if result.get("count", 0) > 0
                    else (_ for _ in ()).throw(
                        AssertionError("Expected repos for torvalds"),
                    )
                ),
            )

        await runner.check_async(
            "get_repo('torvalds', 'linux', mock=False) -> real GitHub API",
            get_repo,
            "torvalds",
            "linux",
            mock=False,
        )

        await runner.check_async(
            "search_repos('language:python stars:>50000', mock=False) -> real GitHub API",
            search_repos,
            "language:python stars:>50000",
            mock=False,
        )

        await runner.check_async(
            "get_repo_issues('octocat', 'Hello-World', mock=False) -> real GitHub API",
            get_repo_issues,
            "octocat",
            "Hello-World",
            mock=False,
        )
    else:
        print("\n--- Step 6: Real API Calls (SKIPPED — use --real to enable) ---")
        runner.skip("Real API calls", "pass --real to enable")

    return runner.summary()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate integration framework end-to-end",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="Include real GitHub API calls",
    )
    args = parser.parse_args()

    exit_code = asyncio.run(run_validation(include_real=args.real))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
