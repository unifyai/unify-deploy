from pathlib import Path
import shlex
import shutil
import subprocess
import textwrap

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BASH_WATCHER = REPO_ROOT / "communication/infra/scripts/unity-pool-watcher.sh"
POWERSHELL_WATCHER = REPO_ROOT / "communication/infra/scripts/unity-pool-watcher.ps1"
INSTALL_BASE = (
    REPO_ROOT
    / "communication/infra/scripts/ubuntu-vm-custom-image/packer/scripts/install-base.sh"
)
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


def test_bash_watcher_defines_desktop_profile_helpers():
    text = BASH_WATCHER.read_text()
    assert "archive_desktop_profile()" in text
    assert "restore_desktop_profile()" in text
    assert "desktop-profile.tar.gz" in text
    assert "ensure_chromium_password_store_basic()" in text
    # xfce4 must no longer be preserved across assistants on the shared pool VM
    assert '! -path "/Unity/.config/xfce4"' not in text
    assert "rm -rf /Unity/.magnitude" in text


def test_bash_watcher_repairs_unityuser_workspace_access_before_agent_start():
    text = BASH_WATCHER.read_text()

    assert "ensure_unity_workspace_access()" in text
    assert "install -d -o unityuser -g unityuser -m 0755 /Unity /Unity/Local" in text
    assert 'test -r "$1" && test -w "$1" && test -x "$1"' in text
    # The check must happen after a mount/restore can replace filesystem
    # ownership and before agent-service starts with UNITY_LOCAL_ROOT there.
    assert text.index("ensure_unity_workspace_access || return 1") < text.index(
        "# Agent Service .env"
    )


def test_powershell_watcher_scrubs_magnitude_and_archives_profile():
    text = POWERSHELL_WATCHER.read_text()
    assert "function Archive-DesktopProfile" in text
    assert "function Restore-DesktopProfile" in text
    assert "desktop-profile.tar.gz" in text
    assert ".magnitude" in text
    # Scrub must wipe magnitude / Chrome profile dirs after archive
    assert 'Remove-Item (Join-Path $userProfile ".magnitude")' in text
    assert "AppData\\Local\\Google\\Chrome\\User Data" in text


def test_chromium_wrapper_uses_password_store_basic():
    text = INSTALL_BASE.read_text()
    assert "--password-store=basic" in text


def test_bash_stage_chromium_profile_copies_essentials(tmp_path):
    """_stage_chromium_profile keeps Cookies/prefs and skips caches."""
    src = tmp_path / "chromium"
    default = src / "Default"
    default.mkdir(parents=True)
    (default / "Cookies").write_text("cookie-db")
    (default / "Preferences").write_text("{}")
    (default / "Cache").mkdir()
    (default / "Cache" / "f").write_text("cache")
    (src / "Local State").write_text("local")

    dest = tmp_path / "staged"
    command = textwrap.dedent(
        f"""
        source {shlex.quote(str(BASH_WATCHER))}
        if _stage_chromium_profile {shlex.quote(str(src))} {shlex.quote(str(dest))}; then
            printf 'ok'
        else
            printf 'fail'
        fi
        """,
    )
    result = subprocess.run(
        ["bash", "-lc", command],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "ok"
    assert (dest / "Default" / "Cookies").read_text() == "cookie-db"
    assert (dest / "Default" / "Preferences").read_text() == "{}"
    assert (dest / "Local State").read_text() == "local"
    assert not (dest / "Default" / "Cache").exists()


@pytest.mark.skipif(POWERSHELL is None, reason="pwsh not installed")
def test_powershell_stage_chromium_profile_copies_essentials(tmp_path):
    src = tmp_path / "User Data"
    default = src / "Default"
    default.mkdir(parents=True)
    (default / "Cookies").write_text("cookie-db")
    (default / "Preferences").write_text("{}")
    (default / "Cache").mkdir()
    (src / "Local State").write_text("local")
    dest = tmp_path / "staged"

    command = textwrap.dedent(
        f"""
        . {_powershell_literal(str(POWERSHELL_WATCHER))} -SkipMain
        $ok = Stage-ChromiumProfile {_powershell_literal(str(src))} {_powershell_literal(str(dest))}
        if ($ok) {{ [Console]::Write('ok') }} else {{ [Console]::Write('fail') }}
        """,
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "ok"
    assert (dest / "Default" / "Cookies").read_text() == "cookie-db"
    assert not (dest / "Default" / "Cache").exists()
