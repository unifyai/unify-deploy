from pathlib import Path
import shlex
import shutil
import subprocess
import textwrap

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BASH_WATCHER = REPO_ROOT / "communication/infra/scripts/droid-pool-watcher.sh"
POWERSHELL_WATCHER = REPO_ROOT / "communication/infra/scripts/droid-pool-watcher.ps1"
POWERSHELL = shutil.which("pwsh")

RELEASE_TRIGGER_CASES = [
    pytest.param(
        "",
        "",
        "binding-1:2",
        "binding-1:1",
        True,
        id="rearm-visible-while-unify-key-stays-empty",
    ),
    pytest.param(
        "",
        "",
        "binding-1:2",
        "binding-1:2",
        False,
        id="acked-token-is-not-replayed",
    ),
    pytest.param(
        "assigned-key",
        "",
        "",
        "",
        True,
        id="legacy-unify-key-clear-still-releases",
    ),
    pytest.param(
        "",
        "assigned-key",
        "binding-1:2",
        "binding-1:1",
        False,
        id="active-assignment-does-not-release",
    ),
]


def _bash_should_trigger_release(
    previous_unify_key: str,
    current_unify_key: str,
    current_release_token: str,
    last_handled_release_token: str,
) -> bool:
    command = textwrap.dedent(
        f"""
        source {shlex.quote(str(BASH_WATCHER))}
        if should_trigger_release \
            {shlex.quote(previous_unify_key)} \
            {shlex.quote(current_unify_key)} \
            {shlex.quote(current_release_token)} \
            {shlex.quote(last_handled_release_token)}; then
            printf 'true'
        else
            printf 'false'
        fi
        """,
    )
    result = subprocess.run(
        ["bash", "-lc", command],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "true"


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@pytest.mark.parametrize(
    (
        "previous_unify_key",
        "current_unify_key",
        "current_release_token",
        "last_handled_release_token",
        "expected",
    ),
    RELEASE_TRIGGER_CASES,
)
def test_bash_pool_watcher_release_decision(
    previous_unify_key: str,
    current_unify_key: str,
    current_release_token: str,
    last_handled_release_token: str,
    expected: bool,
):
    assert (
        _bash_should_trigger_release(
            previous_unify_key,
            current_unify_key,
            current_release_token,
            last_handled_release_token,
        )
        is expected
    )


@pytest.mark.skipif(POWERSHELL is None, reason="pwsh not installed")
@pytest.mark.parametrize(
    (
        "previous_unify_key",
        "current_unify_key",
        "current_release_token",
        "last_handled_release_token",
        "expected",
    ),
    RELEASE_TRIGGER_CASES,
)
def test_powershell_pool_watcher_release_decision(
    previous_unify_key: str,
    current_unify_key: str,
    current_release_token: str,
    last_handled_release_token: str,
    expected: bool,
):
    command = textwrap.dedent(
        f"""
        . { _powershell_literal(str(POWERSHELL_WATCHER))} -SkipMain
        if (Should-TriggerRelease {_powershell_literal(previous_unify_key)} {_powershell_literal(current_unify_key)} {_powershell_literal(current_release_token)} {_powershell_literal(last_handled_release_token)}) {{
            [Console]::Write('true')
        }} else {{
            [Console]::Write('false')
        }}
        """,
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (result.stdout.strip() == "true") is expected
