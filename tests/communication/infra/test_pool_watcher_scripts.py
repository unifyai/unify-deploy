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
SUPERVISOR_CONFIG = (
    REPO_ROOT
    / "communication/infra/scripts/ubuntu-vm-custom-image/packer/files/supervisord.conf"
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


def _bash_function_body(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start)
    return text[start:end]


def _watcher_scenario(tmp_path: Path, body: str, timeout: int = 90) -> str:
    """Run *body* with the watcher sourced and its state directory redirected.

    The stubs a scenario installs are what keep ``main`` away from the real
    cleanup helpers: ``scrub_filesystem`` deletes the contents of ``/tmp`` and
    ``/root`` on a pool VM, which is not something a test run should discover.
    """
    script = (
        textwrap.dedent(
            f"""
        source {shlex.quote(str(BASH_WATCHER))}
        state={shlex.quote(str(tmp_path))}
        RELEASE_STATE_DIR="$state"
        LAST_RELEASE_TOKEN_FILE="$state/last-release-token"
        RESTORE_IN_FLIGHT_FILE="$state/restore-in-flight"
        JOB_TERM_GRACE_SECONDS=2
        events="$state/events"
        : > "$events"
        """,
        )
        + textwrap.dedent(body)
    )
    result = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert "UNSTUBBED" not in result.stdout, result.stdout
    return result.stdout


def test_bash_release_interrupts_an_in_flight_assignment(tmp_path):
    """A release signalled mid-bootstrap must not wait for the bootstrap.

    A cold pool VM bootstraps for tens of minutes. The stubbed assignment here
    stands in for that: if the watcher still ran it inline, the release would
    only be handled once it returned.
    """
    stdout = _watcher_scenario(
        tmp_path,
        """
        printf 'assistant-key' > "$state/unify-key"
        printf '' > "$state/release-token"

        get_metadata() {
            case $1 in
                unify-key) cat "$state/unify-key" ;;
                *) printf '' ;;
            esac
        }
        current_release_token() { cat "$state/release-token"; }
        refresh_tls() { :; }
        scrub_filesystem() { printf 'UNSTUBBED scrub\\n' >> "$events"; }
        kill_agent_service() { :; }

        do_assign() {
            printf 'assign-started\\n' >> "$events"
            sleep 120
            printf 'assign-finished\\n' >> "$events"
        }
        do_release() { printf 'release-ran\\n' >> "$events"; }
        do_update() { printf 'update-ran\\n' >> "$events"; }

        tick=0
        wait_for_metadata_change() {
            tick=$((tick + 1))
            if [[ $tick -eq 1 ]]; then
                printf '' > "$state/unify-key"
                printf 'binding-1:1' > "$state/release-token"
                return
            fi
            exit 0
        }

        trap 'printf "EVENTS:%s\\n" "$(tr "\\n" "," < "$events")"' EXIT
        main
        """,
    )

    events = stdout.rsplit("EVENTS:", 1)[1].strip().strip(",").split(",")
    assert events == ["assign-started", "release-ran", "update-ran"]
    assert "JOB: assign ended with status" in stdout
    assert "(release requested)" in stdout


def test_bash_cancel_job_kills_processes_that_ignore_term(tmp_path):
    """Cancellation must reach the whole subtree, not just the phase itself.

    An assignment's real cost sits in its children -- git, npm, gsutil -- and
    a package install that ignores SIGTERM would otherwise keep writing to the
    filesystem that release is about to scrub.
    """
    stdout = _watcher_scenario(
        tmp_path,
        """
        stubborn_phase() {
            bash -c 'printf "%s" $$ > "'"$state"'/grandchild.pid"; trap "" TERM; sleep 120' &
            wait
        }

        start_job assign stubborn_phase
        for _ in $(seq 1 100); do
            [[ -s "$state/grandchild.pid" ]] && break
            sleep 0.1
        done
        grandchild=$(cat "$state/grandchild.pid")
        cancel_job "release requested"

        for _ in $(seq 1 50); do
            kill -0 "$grandchild" 2>/dev/null || break
            sleep 0.1
        done
        if kill -0 "$grandchild" 2>/dev/null; then
            printf 'GRANDCHILD:alive\\n'
        else
            printf 'GRANDCHILD:reaped\\n'
        fi
        """,
    )

    assert "GRANDCHILD:reaped" in stdout
    assert "JOB: assign ended with status" in stdout
    assert "left processes behind" not in stdout


def test_bash_cancel_job_is_a_no_op_without_a_running_phase(tmp_path):
    stdout = _watcher_scenario(
        tmp_path,
        """
        cancel_job "release requested"
        start_job update true
        sleep 0.5
        cancel_job "assignment requested"
        printf 'DONE\\n'
        """,
    )

    assert "DONE" in stdout
    assert "JOB: update ended with status 0" in stdout


@pytest.mark.parametrize(
    ("http_code", "expected"),
    [
        ("401", True),
        ("403", True),
        ("404", True),
        ("409", True),
        ("500", False),
        ("502", False),
        ("000", False),
        ("", False),
    ],
)
def test_bash_ready_notification_terminal_statuses(http_code: str, expected: bool):
    command = textwrap.dedent(
        f"""
        source {shlex.quote(str(BASH_WATCHER))}
        if ready_notification_is_terminal {shlex.quote(http_code)}; then
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
    assert (result.stdout.strip() == "true") is expected


def test_bash_ready_notification_reads_a_clean_status_code():
    """``curl -f`` exits non-zero on an error status, so a shell fallback on
    that exit concatenates onto the code ``-w`` already printed (``404`` plus
    ``000`` reads as ``404000``) and no status can ever be recognised."""
    assign = _bash_function_body(BASH_WATCHER.read_text(), "do_assign")
    ready_call = assign[assign.index("/infra/vm/ready") - 400 :]
    assert 'echo "000"' not in ready_call
    assert "curl -s -o /dev/null" in ready_call


def test_bash_restore_marker_survives_until_the_release_that_clears_it(tmp_path):
    stdout = _watcher_scenario(
        tmp_path,
        """
        restore_in_flight && printf 'MARKED:before\\n' || printf 'CLEAR:before\\n'
        mark_restore_in_flight
        restore_in_flight && printf 'MARKED:during\\n' || printf 'CLEAR:during\\n'
        clear_restore_in_flight
        restore_in_flight && printf 'MARKED:after\\n' || printf 'CLEAR:after\\n'
        """,
    )

    assert "CLEAR:before" in stdout
    assert "MARKED:during" in stdout
    assert "CLEAR:after" in stdout


def test_bash_interrupted_restore_keeps_the_existing_archives():
    """A bootstrap cut short mid-restore leaves a partial copy of the very
    archives it was unpacking; uploading that would overwrite good state."""
    text = BASH_WATCHER.read_text()
    assign = _bash_function_body(text, "do_assign")
    release = _bash_function_body(text, "do_release")

    assert assign.index("mark_restore_in_flight") < assign.index(
        "# Mount persistent disk",
    )
    assert assign.index("restore_desktop_profile") < assign.index(
        "clear_restore_in_flight",
    )
    assert release.index("if restore_in_flight; then") < release.index(
        "Archiving /Unity/Local",
    )
    assert release.index("if restore_in_flight; then") < release.index(
        "archive_desktop_profile",
    )


def test_bash_release_reports_completion_before_refreshing_code():
    """The pool stays blocked until release-complete lands, so the idle code
    refresh runs after the callback rather than in front of it."""
    text = BASH_WATCHER.read_text()
    release = _bash_function_body(text, "do_release")

    assert "do_update" not in release
    assert "notify_release_complete" in release
    assert "start_job update do_update" in text


def test_bash_watcher_references_no_purged_github_credential():
    """``set -u`` makes a stale variable reference fatal, and systemd restarts
    the watcher straight back into it: the assignment path would crash-loop
    rather than fail once. The credential itself was removed as unnecessary --
    every repository a pool VM clones is public."""
    assert "github_token" not in BASH_WATCHER.read_text()


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
    assert "/Unity /Unity/Local /Unity/.config /Unity/.local /Unity/.cache" in text
    assert (
        'for path; do test -r "$path" && test -w "$path" && test -x "$path" || exit 1; done'
        in text
    )
    profile_restore = text.index(
        'restore_desktop_profile "$assistant_id" "$profile_bucket"',
    )
    repair_after_profile_restore = text.index(
        "ensure_unity_workspace_access || return 1",
        profile_restore,
    )
    # Extracting the root-owned profile archive can reset /Unity to mode 0700.
    # Repair after that extraction and before agent-service starts with
    # UNIFY_LOCAL_ROOT there.
    assert repair_after_profile_restore < text.index("# Agent Service .env")


def test_agent_service_uses_supervisor_with_stable_desktop_dbus():
    watcher = BASH_WATCHER.read_text()
    supervisor = SUPERVISOR_CONFIG.read_text()

    assert "supervisorctl start services:agent-service" in watcher
    assert "supervisorctl stop services:agent-service" in watcher
    assert "su -s /bin/bash unityuser -c" not in watcher
    assert 'pkill -f "node"' not in watcher

    agent_program = supervisor.split("[program:agent-service]", 1)[1].split(
        "[program:caddy]",
        1,
    )[0]
    assert 'DBUS_SESSION_BUS_ADDRESS="autolaunch:"' in agent_program
    assert "autorestart=true" in agent_program
    assert "redirect_stderr=true" in agent_program
    assert "stdout_logfile=/var/log/supervisor/agent-service.log" in agent_program


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
