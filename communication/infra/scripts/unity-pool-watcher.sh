#!/bin/bash
# =============================================================================
# Unity Pool Watcher
#
# Systemd service that watches GCE instance metadata for assignment/release
# signals. When unify-key changes from empty to non-empty, the VM is being
# assigned to an assistant; when it changes back to empty, the VM is released.
#
# Install as: /usr/local/bin/unity-pool-watcher.sh
# Systemd unit: /etc/systemd/system/unity-pool-watcher.service
# =============================================================================

set -u

METADATA_URL="http://metadata.google.internal/computeMetadata/v1"
METADATA_HEADER="Metadata-Flavor: Google"
ETAG=""
PREV_UNIFY_KEY=""
PREV_TLS_HASH=""

source /etc/profile.d/unity-vm.sh 2>/dev/null || true
source /etc/profile.d/bun.sh 2>/dev/null || true
export HOME=/root
export PATH="/root/.bun/bin:$PATH"
RELEASE_STATE_DIR="/var/lib/unity-pool-watcher"
LAST_RELEASE_TOKEN_FILE="$RELEASE_STATE_DIR/last-release-token"
RESTORE_IN_FLIGHT_FILE="$RELEASE_STATE_DIR/restore-in-flight"

# Bound the metadata long-poll so the loop keeps supervising background phases
# even while metadata sits unchanged.
METADATA_POLL_TIMEOUT_SECONDS=60
# How long an interrupted phase gets to unwind before it is killed outright,
# and how long the kill itself is given to clear the process group.
JOB_TERM_GRACE_SECONDS=15
JOB_KILL_GRACE_SECONDS=15

get_metadata() {
    local key=$1
    curl -sf -H "$METADATA_HEADER" "$METADATA_URL/instance/attributes/$key" 2>/dev/null || echo ""
}

get_deploy_env() {
    local env_name
    env_name=$(get_metadata "unity-environment")
    if [[ -n "$env_name" ]]; then
        echo "$env_name"
    elif [[ -n "$(get_metadata "staging")" ]]; then
        echo "staging"
    else
        echo "production"
    fi
}

log() {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
}

configure_caddy_hostname() {
    local hostname=$1
    local caddyfile=/etc/caddy/Caddyfile
    [[ -n "$hostname" && -f "$caddyfile" ]] || return 0

    # Pool images boot with their pool hostname. The binding's assistant
    # hostname is stable across VM reassignment, so make Caddy accept it
    # before the readiness callback asks Comms to probe it.
    sed -i "1s|^[^{]*{|$hostname {|" "$caddyfile"
    caddy reload --config "$caddyfile" 2>/dev/null || true
}

ensure_release_state_dir() {
    mkdir -p "$RELEASE_STATE_DIR"
    chmod 700 "$RELEASE_STATE_DIR" 2>/dev/null || true
}

load_last_release_token() {
    if [[ -f "$LAST_RELEASE_TOKEN_FILE" ]]; then
        tr -d '\n' < "$LAST_RELEASE_TOKEN_FILE"
    else
        echo ""
    fi
}

save_last_release_token() {
    local token=$1
    ensure_release_state_dir
    if [[ -n "$token" ]]; then
        printf '%s' "$token" > "$LAST_RELEASE_TOKEN_FILE"
    else
        rm -f "$LAST_RELEASE_TOKEN_FILE"
    fi
}

current_release_token() {
    local binding_id release_generation
    binding_id=$(get_metadata "binding-id")
    release_generation=$(get_metadata "release-generation")
    if [[ -n "$binding_id" && -n "$release_generation" ]]; then
        printf '%s:%s' "$binding_id" "$release_generation"
    fi
}

should_trigger_release() {
    local previous_unify_key=${1-}
    local current_unify_key=${2-}
    local current_release_token=${3-}
    local last_handled_release_token=${4-}

    if [[ "$current_unify_key" != "$previous_unify_key" && -z "$current_unify_key" && -z "$current_release_token" ]]; then
        return 0
    fi

    [[ -z "$current_unify_key" && -n "$current_release_token" && "$current_release_token" != "$last_handled_release_token" ]]
}

mark_restore_in_flight() {
    ensure_release_state_dir
    : > "$RESTORE_IN_FLIGHT_FILE"
}

clear_restore_in_flight() {
    rm -f "$RESTORE_IN_FLIGHT_FILE"
}

restore_in_flight() {
    [[ -f "$RESTORE_IN_FLIGHT_FILE" ]]
}

# ─── Interruptible phases ────────────────────────────────────────────────
#
# Bootstrapping a cold pool VM runs for tens of minutes: two repository
# clones, three dependency installs and a browser download. Run inline, it
# also blocks the metadata poll, so a release signalled mid-bootstrap is not
# even read until the bootstrap finishes and the VM stays claimed for that
# whole window. Phases a release is allowed to cut short therefore run in
# their own process group, which leaves the poll loop free to notice the
# signal and lets one kill reach every descendant the phase spawned -- git,
# npm, gsutil and the rest.

JOB_PID=""
JOB_LABEL=""

start_job() {
    local label=$1
    shift
    cancel_job "superseded by $label"
    # Monitor mode is what puts the child in a process group of its own,
    # making $! usable as a process group id.
    set -m
    ( "$@" ) &
    JOB_PID=$!
    set +m
    JOB_LABEL="$label"
    log "JOB: $label started (process group $JOB_PID)"
}

cancel_job() {
    local reason=${1-}
    [[ -n "$JOB_PID" ]] || return 0

    local pid=$JOB_PID
    local label=$JOB_LABEL
    local watchdog status waited
    JOB_PID=""
    JOB_LABEL=""

    kill -TERM -"$pid" 2>/dev/null || true
    # A phase blocked in a package install can ignore the term signal, so a
    # kill follows once the grace is up. The watchdog rides outside the doomed
    # process group so it survives to deliver it.
    ( sleep "$JOB_TERM_GRACE_SECONDS"; kill -KILL -"$pid" 2>/dev/null || true ) &
    watchdog=$!
    wait "$pid" 2>/dev/null
    status=$?

    # The phase's own shell exits ahead of the children it spawned, and those
    # children are what would go on writing to a filesystem that release is
    # about to archive and scrub. Signal 0 to the group reports whether any
    # member is left, so wait for the group rather than for the shell.
    waited=0
    while kill -0 -"$pid" 2>/dev/null; do
        if [[ $waited -ge $((JOB_TERM_GRACE_SECONDS + JOB_KILL_GRACE_SECONDS)) ]]; then
            log "WARNING: $label left processes behind that did not die"
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done

    kill "$watchdog" 2>/dev/null || true
    wait "$watchdog" 2>/dev/null || true

    log "JOB: $label ended with status $status${reason:+ ($reason)}"
}

read_metadata_etag() {
    curl -sf -H "$METADATA_HEADER" \
        -o /dev/null -D - \
        "$METADATA_URL/instance/attributes/?recursive=true" \
        2>/dev/null | grep -i "etag:" | tr -d '\r' | awk '{print $2}' || echo ""
}

wait_for_metadata_change() {
    local response
    response=$(curl -sf -H "$METADATA_HEADER" \
        "$METADATA_URL/instance/attributes/?recursive=true&wait_for_change=true&timeout_sec=$METADATA_POLL_TIMEOUT_SECONDS&last_etag=$ETAG" \
        2>/dev/null || echo "")

    if [[ -z "$response" ]]; then
        log "Metadata poll returned empty, retrying in 5s"
        sleep 5
        return
    fi

    ETAG=$(read_metadata_etag)
}

# ─── Code update helpers ─────────────────────────────────────────────────

get_remote_commit_hash() {
    local repo_url=$1
    local branch=$2
    git ls-remote "$repo_url" "refs/heads/$branch" 2>/dev/null | cut -c1-12
}

get_saved_commit_hash() {
    local dir=$1
    cat "$dir/.commit-hash" 2>/dev/null || echo ""
}

save_commit_hash() {
    local dir=$1
    local hash=$2
    if [[ -n "$hash" ]]; then
        echo -n "$hash" > "$dir/.commit-hash"
    fi
}

scrub_git_tokens() {
    for repo_dir in /magnitude /agent-service; do
        if [[ -d "$repo_dir/.git" ]]; then
            local url
            url=$(git -C "$repo_dir" remote get-url origin 2>/dev/null || true)
            if [[ "$url" == *"@github.com"* ]]; then
                git -C "$repo_dir" remote set-url origin "$(echo "$url" | sed 's|https://[^@]*@|https://|')" 2>/dev/null || true
            fi
        fi
    done
}

# Desktop-profile paths live in unityuser HOME (/Unity), not /Unity/Local, so they
# are not rcloned into the Unify pod. They ride a sibling GCS blob:
#   gs://{archive-bucket}/{assistant_id}-desktop-profile.tar.gz
DESKTOP_PROFILE_CHROMIUM_FILES=(
    "Cookies"
    "Login Data"
    "Login Data-journal"
    "Web Data"
    "Web Data-journal"
    "Preferences"
    "Bookmarks"
    "Bookmarks.bak"
    "Network Persistent State"
)
DESKTOP_PROFILE_CHROMIUM_DIRS=(
    "Local Storage"
    "Session Storage"
)

ensure_chromium_password_store_basic() {
    # Existing pool images may lack --password-store=basic; patch the wrapper
    # idempotently so archived Cookies/Login Data restore across VMs.
    local wrapper=/usr/local/bin/chromium-browser
    [[ -f "$wrapper" ]] || return 0
    if grep -q -- '--password-store=basic' "$wrapper" 2>/dev/null; then
        return 0
    fi
    if grep -q -- 'exec "$CHROME_PATH"' "$wrapper" 2>/dev/null; then
        sed -i 's|exec "$CHROME_PATH" --no-sandbox "$@"|exec "$CHROME_PATH" --no-sandbox --password-store=basic "$@"|' "$wrapper"
        if ! grep -q -- '--password-store=basic' "$wrapper" 2>/dev/null; then
            sed -i 's|exec "$CHROME_PATH" "$@"|exec "$CHROME_PATH" --no-sandbox --password-store=basic "$@"|' "$wrapper"
        fi
        if grep -q -- '--password-store=basic' "$wrapper" 2>/dev/null; then
            log "Patched chromium-browser wrapper with --password-store=basic"
        else
            log "WARNING: failed to patch chromium-browser for password-store=basic"
        fi
    fi
}

_stage_chromium_profile() {
    local src_root=$1
    local dest_root=$2
    local src_default="$src_root/Default"
    local dest_default="$dest_root/Default"
    [[ -d "$src_default" ]] || return 1
    mkdir -p "$dest_default"
    local name
    for name in "${DESKTOP_PROFILE_CHROMIUM_FILES[@]}"; do
        if [[ -e "$src_default/$name" ]]; then
            cp -a "$src_default/$name" "$dest_default/$name" 2>/dev/null || true
        fi
    done
    for name in "${DESKTOP_PROFILE_CHROMIUM_DIRS[@]}"; do
        if [[ -d "$src_default/$name" ]]; then
            cp -a "$src_default/$name" "$dest_default/$name" 2>/dev/null || true
        fi
    done
    if [[ -f "$src_root/Local State" ]]; then
        cp -a "$src_root/Local State" "$dest_root/Local State" 2>/dev/null || true
    fi
    # Only keep the staged profile if we copied at least one artifact.
    if [[ -z "$(find "$dest_root" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
        return 1
    fi
    return 0
}

archive_desktop_profile() {
    local assistant_id=$1
    local archive_bucket=$2
    local archive_path="gs://${archive_bucket}/${assistant_id}-desktop-profile.tar.gz"
    local staging
    staging=$(mktemp -d /tmp/unity-desktop-profile.XXXXXX)
    local staged=0

    if [[ -d /Unity/.magnitude/browser_states ]]; then
        mkdir -p "$staging/.magnitude"
        cp -a /Unity/.magnitude/browser_states "$staging/.magnitude/" 2>/dev/null && staged=1
    fi

    if [[ -d /Unity/.config/chromium ]]; then
        mkdir -p "$staging/.config"
        if _stage_chromium_profile /Unity/.config/chromium "$staging/.config/chromium"; then
            staged=1
        else
            rm -rf "$staging/.config/chromium"
        fi
    fi
    if [[ -d /Unity/.config/google-chrome ]]; then
        mkdir -p "$staging/.config"
        if _stage_chromium_profile /Unity/.config/google-chrome "$staging/.config/google-chrome"; then
            staged=1
        else
            rm -rf "$staging/.config/google-chrome"
        fi
    fi

    if [[ -d /Unity/.config/xfce4 ]]; then
        mkdir -p "$staging/.config"
        cp -a /Unity/.config/xfce4 "$staging/.config/" 2>/dev/null && staged=1
    fi

    if [[ "$staged" -eq 0 ]]; then
        log "No desktop-profile artifacts to archive"
        rm -rf "$staging"
        return 0
    fi

    log "Archiving desktop profile to $archive_path"
    if tar czf - -C "$staging" . | gsutil -q cp - "$archive_path" 2>/dev/null; then
        log "Desktop-profile archive uploaded successfully"
    else
        log "WARNING: desktop-profile archive upload failed"
    fi
    rm -rf "$staging"
}

restore_desktop_profile() {
    local assistant_id=$1
    local archive_bucket=$2
    local archive_path="gs://${archive_bucket}/${assistant_id}-desktop-profile.tar.gz"

    if ! gsutil -q stat "$archive_path" 2>/dev/null; then
        log "No desktop-profile archive at $archive_path"
        return 0
    fi

    log "Restoring desktop profile from $archive_path"
    mkdir -p /Unity
    if gsutil -q cp "$archive_path" - 2>/dev/null | tar xzf - -C /Unity 2>/dev/null; then
        chown -R unityuser:unityuser \
            /Unity/.magnitude \
            /Unity/.config/chromium \
            /Unity/.config/google-chrome \
            /Unity/.config/xfce4 \
            2>/dev/null || true
        log "Desktop profile restored"
    else
        log "WARNING: desktop-profile restore failed"
    fi
}

scrub_filesystem() {
    log "SCRUB: cleaning session artifacts from filesystem"

    # /root/ — preserve shell config, package manager caches, and all of .cache
    # (.cache contains browser binaries, shader caches, fontconfig, etc. that are
    # expensive to rebuild and safe to keep across assignments)
    find /root -mindepth 1 -maxdepth 1 \
        ! -name '.bashrc' ! -name '.profile' ! -name '.bash_logout' \
        ! -name '.npm' ! -name '.bun' ! -name '.cache' \
        -exec rm -rf {} + 2>/dev/null || true

    # /Unity/ — preserve structural dirs; wipe session contents including browser
    # profile / magnitude / xfce4 (those ride the per-assistant desktop-profile
    # archive, not the shared pool disk).
    find /Unity -mindepth 1 -maxdepth 1 \
        ! -name '.ssh' ! -name 'Local' \
        ! -name '.bashrc' ! -name '.config' ! -name '.local' ! -name '.cache' \
        -exec rm -rf {} + 2>/dev/null || true
    for dir in .config .local; do
        if [[ -d "/Unity/$dir" ]]; then
            find "/Unity/$dir" -mindepth 1 -exec rm -rf {} + 2>/dev/null || true
        fi
    done
    # Wipe .cache contents but preserve the ms-playwright symlink
    if [[ -d /Unity/.cache ]]; then
        find /Unity/.cache -mindepth 1 ! -name 'ms-playwright' -exec rm -rf {} + 2>/dev/null || true
    fi
    rm -rf /Unity/.magnitude 2>/dev/null || true

    # Application logs
    rm -f /var/log/agent-service.log
    : > /var/log/caddy/access.log 2>/dev/null || true
    find /var/log/supervisor -name '*.log' -exec truncate -s 0 {} \; 2>/dev/null || true

    # Temp files
    find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true

    log "SCRUB complete"
}

wipe_metadata_key() {
    local key=$1
    local comms_url id_token
    comms_url=$(get_metadata "comms-url")
    id_token=$(curl -sf -H "Metadata-Flavor: Google" \
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience=unity-comms-vm&format=full" \
        2>/dev/null || true)
    [[ -z "$comms_url" || -z "$id_token" ]] && return
    curl -sf -X POST "$comms_url/infra/vm/wipe-metadata-key" \
        -H "Authorization: Bearer $id_token" \
        -H "Content-Type: application/json" \
        -d "{\"key\": \"$key\"}" >/dev/null 2>&1 \
        && log "Wiped metadata key: $key" \
        || log "WARNING: failed to wipe metadata key $key via Comms API"
}

notify_release_complete() {
    local binding_id release_generation comms_url id_token payload response http_status response_body
    binding_id=$(get_metadata "binding-id")
    release_generation=$(get_metadata "release-generation")
    comms_url=$(get_metadata "comms-url")
    id_token=$(curl -sf -H "Metadata-Flavor: Google" \
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience=unity-comms-vm&format=full" \
        2>/dev/null || true)
    if [[ -z "$binding_id" || -z "$comms_url" || -z "$id_token" ]]; then
        log "WARNING: missing release completion metadata (binding-id/comms-url/id-token)"
        return 1
    fi
    if [[ -n "$release_generation" ]]; then
        payload=$(printf '{"binding_id": "%s", "release_generation": %s}' "$binding_id" "$release_generation")
    else
        payload=$(printf '{"binding_id": "%s"}' "$binding_id")
    fi

    for attempt in $(seq 1 10); do
        response=$(curl -sS -X POST "$comms_url/infra/vm/release-complete" \
            -H "Authorization: Bearer $id_token" \
            -H "Content-Type: application/json" \
            -d "$payload" \
            -w $'\n%{http_code}' 2>&1)
        http_status=$(printf '%s\n' "$response" | tail -n 1)
        response_body=$(printf '%s\n' "$response" | sed '$d')
        if [[ "$http_status" =~ ^2[0-9][0-9]$ ]]; then
            if [[ -n "$response_body" ]]; then
                log "Reported release completion to Comms: status=$http_status body=$response_body"
            else
                log "Reported release completion to Comms: status=$http_status"
            fi
            return 0
        fi
        if [[ -n "$response_body" ]]; then
            log "Release completion attempt $attempt failed: status=${http_status:-curl_error} body=$response_body"
        else
            log "Release completion attempt $attempt failed: status=${http_status:-curl_error}"
        fi
        sleep 3
    done

    log "WARNING: failed to report release completion to Comms"
    return 1
}

kill_agent_service() {
    supervisorctl stop services:agent-service >/dev/null 2>&1 || true

    for i in $(seq 1 10); do
        if ! ss -tlnp | grep -q ':3000 '; then
            return
        fi
        if [[ $i -ge 3 ]]; then
            fuser -k -KILL 3000/tcp 2>/dev/null || true
        fi
        sleep 1
    done
    log "WARNING: port 3000 still in use after kill_agent_service"
}

do_update() {
    log "UPDATE: checking for code updates"

    kill_agent_service

    local deploy_env
    deploy_env=$(get_deploy_env)

    # Both repositories are public, so these clones are unauthenticated.
    local magnitude_url unity_url unity_branch
    magnitude_url="https://github.com/unifyai/magnitude.git"
    unity_url="https://github.com/unifyai/unity.git"
    case "$deploy_env" in
        staging) unity_branch="staging" ;;
        *) unity_branch="main" ;;
    esac

    # ── Magnitude ──
    local mag_saved mag_remote
    mag_saved=$(get_saved_commit_hash /magnitude)
    mag_remote=$(get_remote_commit_hash "$magnitude_url" "main")

    if [[ -n "$mag_saved" && -n "$mag_remote" && "$mag_saved" == "$mag_remote" ]]; then
        log "Magnitude up-to-date ($mag_saved)"

        log "Installing Magnitude dependencies..."
        cd /magnitude
        if command -v bun &>/dev/null; then
            bun install 2>&1
        else
            npm install 2>&1
        fi
        cd /
        log "Magnitude dependencies installed"
    else
        log "Magnitude updating ($mag_saved -> $mag_remote)"
        if [[ -d "/magnitude/.git" ]]; then
            cd /magnitude
            # VMs provisioned before the credential purge still carry a
            # tokenised origin; point them back at the public URL before fetch.
            git remote set-url origin "$magnitude_url" 2>/dev/null || true
            git fetch --depth 1 origin main 2>&1 || true
            git reset --hard origin/main 2>&1 || true
            local commit
            commit=$(git rev-parse --short=12 HEAD 2>/dev/null || echo "unknown")
            save_commit_hash /magnitude "$commit"
            cd /
        fi
        log "Installing Magnitude dependencies..."
        cd /magnitude
        if command -v bun &>/dev/null; then
            bun install 2>&1
        else
            npm install 2>&1
        fi
        cd /

        # Install Patchright Chromium from magnitude-core
        if [[ -f /magnitude/packages/magnitude-core/package.json ]]; then
            log "Installing Patchright Chromium..."
            cd /magnitude/packages/magnitude-core && npx --yes patchright install --with-deps chromium 2>&1 || true
            cd /
            log "Patchright Chromium installed"
        fi
        log "Magnitude updated"
    fi

    # ── Agent Service (sparse checkout from unity monorepo) ──
    # observationScaling.ts loads /unify/common/observation_scaling_policy.json;
    # include that path in the sparse checkout and install it after move.
    local as_saved as_remote
    local obs_scaling_policy_path="/unify/common/observation_scaling_policy.json"
    as_saved=$(get_saved_commit_hash /agent-service)
    as_remote=$(get_remote_commit_hash "$unity_url" "$unity_branch")

    if [[ -n "$as_saved" && -n "$as_remote" && "$as_saved" == "$as_remote" && -f "$obs_scaling_policy_path" ]]; then
        log "Agent Service up-to-date ($as_saved)"
    else
        if [[ -n "$as_saved" && -n "$as_remote" && "$as_saved" == "$as_remote" ]]; then
            log "Agent Service commit current but observation scaling policy missing; refreshing"
        else
            log "Agent Service updating ($as_saved -> $as_remote)"
        fi
        local tmp_dir
        tmp_dir=$(mktemp -d)
        git clone --depth 1 --branch "$unity_branch" --filter=blob:none --sparse "$unity_url" "$tmp_dir" 2>&1
        cd "$tmp_dir"
        git sparse-checkout set agent-service unify/common 2>&1
        local commit
        commit=$(git rev-parse --short=12 HEAD 2>/dev/null || echo "unknown")

        # Preserve node_modules to speed up npm install
        if [[ -d /agent-service/node_modules ]]; then
            mv /agent-service/node_modules "$tmp_dir/agent-service/node_modules"
        fi
        rm -rf /agent-service
        mv agent-service /agent-service
        mkdir -p /unify/common
        if [[ -f unify/common/observation_scaling_policy.json ]]; then
            cp unify/common/observation_scaling_policy.json "$obs_scaling_policy_path"
            log "Installed observation scaling policy at $obs_scaling_policy_path"
        else
            log "WARNING: observation_scaling_policy.json missing from sparse checkout"
        fi
        rm -rf "$tmp_dir"

        cd /agent-service
        npm install 2>&1
        cd /
        # Record the commit only once its dependencies are installed. An
        # update cut short by a release must re-run on the next assignment,
        # not be mistaken for current and left with half a node_modules.
        save_commit_hash /agent-service "$commit"
        log "Agent Service updated ($commit)"
    fi

    scrub_git_tokens
    ensure_chromium_password_store_basic
    log "UPDATE complete"
}

# ─── Assignment: configure VM for an assistant ───────────────────────────

ensure_unity_workspace_access() {
    # agent-service runs as unityuser and defaults command/file work to
    # /Unity/Local. Both the parent and mountpoint must be traversable by that
    # user; ownership on the mounted filesystem alone is insufficient when the
    # pool image or a prior assignment left /Unity root-owned/restricted.
    install -d -o unityuser -g unityuser -m 0755 \
        /Unity /Unity/Local /Unity/.config /Unity/.local /Unity/.cache
    chown unityuser:unityuser /Unity /Unity/Local /Unity/.config /Unity/.local /Unity/.cache
    chmod 0755 /Unity /Unity/Local /Unity/.config /Unity/.local /Unity/.cache

    if ! runuser -u unityuser -- sh -c \
        'for path; do test -r "$path" && test -w "$path" && test -x "$path" || exit 1; done' \
        sh /Unity /Unity/Local /Unity/.config /Unity/.local /Unity/.cache; then
        log "ERROR: unityuser cannot access desktop workspace paths after permission repair"
        return 1
    fi
    log "Verified unityuser access to desktop workspace paths"
}

ready_notification_is_terminal() {
    # Comms answers 401/403 once Orchestra stops accepting the assistant's key
    # and 404/409 once the binding has moved on -- all verdicts about this
    # assignment rather than transient faults, and all of them routine when a
    # release lands while the bootstrap is still running. Retrying only spends
    # the release's time. Transport failures ("000") and 5xx stay retryable.
    case ${1-} in
        401 | 403 | 404 | 409) return 0 ;;
        *) return 1 ;;
    esac
}

do_assign() {
    local unify_key=$1
    log "ASSIGN: configuring VM for assistant"

    # Clean up any previous assignment (handles re-assignment without explicit release)
    kill_agent_service
    if mountpoint -q /Unity/Local 2>/dev/null; then
        fuser -km /Unity/Local 2>/dev/null || true
        sleep 1
        umount /Unity/Local 2>/dev/null || umount -l /Unity/Local 2>/dev/null || true
        log "Unmounted previous disk from /Unity/Local"
    fi

    # Update code before configuring (skips quickly if already up-to-date)
    do_update

    # Establish a usable mountpoint before the disk arrives. Re-run this after
    # mount/restore below because either operation can replace its ownership.
    ensure_unity_workspace_access || return 1

    # Ensure desktop session dirs exist (may have been wiped by scrub_filesystem)
    for dir in .config .local .cache; do
        mkdir -p "/Unity/$dir"
        chown unityuser:unityuser "/Unity/$dir"
    done
    ln -sfn /root/.cache/ms-playwright /Unity/.cache/ms-playwright

    local vnc_password
    local ssh_public_key
    local disk_device
    local assistant_id
    local binding_id
    local hostname
    local orchestra_url
    local comms_url

    vnc_password=$(get_metadata "vnc-password")
    ssh_public_key=$(get_metadata "ssh-public-key")
    disk_device=$(get_metadata "disk-device")
    assistant_id=$(get_metadata "assistant-id")
    binding_id=$(get_metadata "binding-id")
    hostname=$(get_metadata "hostname")
    orchestra_url=$(get_metadata "orchestra-url")
    comms_url=$(get_metadata "comms-url")
    configure_caddy_hostname "$hostname"

    # From here until the profile is in place, /Unity holds a half-restored
    # copy of the assistant's archives rather than anything worth keeping. A
    # release arriving inside this window must not archive what it finds.
    mark_restore_in_flight

    # Mount persistent disk
    if [[ -n "$disk_device" ]]; then
        local dev_path="/dev/disk/by-id/google-${disk_device}"
        for i in $(seq 1 30); do
            [[ -e "$dev_path" ]] && break
            sleep 1
        done

        if [[ -e "$dev_path" ]]; then
            if ! blkid "$dev_path" &>/dev/null; then
                log "Formatting new disk: $dev_path"
                mkfs.ext4 -q "$dev_path"
            fi
            install -d -o unityuser -g unityuser -m 0755 /Unity/Local
            mount "$dev_path" /Unity/Local
            ensure_unity_workspace_access || return 1
            log "Mounted $dev_path at /Unity/Local"
        else
            log "WARNING: disk device $dev_path not found after 30s"
        fi
    fi

    # Restore from GCS archive if disk is empty (freshly formatted)
    if [[ -n "$disk_device" ]] && mountpoint -q /Unity/Local 2>/dev/null; then
        local file_count
        file_count=$(find /Unity/Local -mindepth 1 -maxdepth 1 ! -name 'lost+found' 2>/dev/null | wc -l)
        if [[ "$file_count" -eq 0 ]]; then
            local archive_bucket
            archive_bucket=$(get_metadata "archive-bucket")
            if [[ -n "$archive_bucket" && -n "$assistant_id" ]]; then
                local archive_path="gs://${archive_bucket}/${assistant_id}.tar.gz"
                log "Empty disk detected, checking for GCS archive at $archive_path"
                if gsutil -q stat "$archive_path" 2>/dev/null; then
                    if gsutil -q cp "$archive_path" - 2>/dev/null | tar xzf - -C /Unity/Local 2>/dev/null; then
                        chown -R unityuser:unityuser /Unity/Local
                        log "Restored filesystem from GCS archive"
                    else
                        log "WARNING: archive restore failed, starting with empty filesystem"
                    fi
                else
                    log "No GCS archive found, starting with empty filesystem"
                fi
            fi
        fi
    fi

    # A restored archive may carry root-owned paths, and mounting the persistent
    # disk may replace Local's ownership. Validate before restoring the desktop
    # profile so its files are written into a usable home directory.
    ensure_unity_workspace_access || return 1

    # Restore browser/GUI profile into home (always when companion blob exists).
    # Independent of Local/ emptiness — profile is not on the PD.
    if [[ -n "$assistant_id" ]]; then
        local profile_bucket
        profile_bucket=$(get_metadata "archive-bucket")
        if [[ -n "$profile_bucket" ]]; then
            restore_desktop_profile "$assistant_id" "$profile_bucket"
        fi
    fi

    # The profile archive is built from a root-owned mktemp directory and
    # contains its top-level `./` entry. GNU tar applies that directory mode to
    # its extraction target, which can silently reset /Unity to root:0700.
    # Repair it again before the unprivileged agent starts.
    ensure_unity_workspace_access || return 1

    clear_restore_in_flight

    # SSH authorized_keys
    if [[ -n "$ssh_public_key" ]]; then
        mkdir -p /Unity/.ssh
        echo "$ssh_public_key" > /Unity/.ssh/authorized_keys
        chown -R unityuser:unityuser /Unity/.ssh
        chmod 700 /Unity/.ssh
        chmod 600 /Unity/.ssh/authorized_keys
        log "SSH authorized_keys configured"
    fi

    # VNC password
    if [[ -n "$vnc_password" ]]; then
        mkdir -p /etc/vnc
        VNC_PASSWORD="$vnc_password" python3 << 'PYSCRIPT'
import os
from Crypto.Cipher import DES
def vnc_encrypt(password):
    key = bytes([0xe8, 0x4a, 0xd6, 0x60, 0xc4, 0x72, 0x1a, 0xe0])
    pw = (password + '\x00' * 8)[:8].encode('latin-1')
    cipher = DES.new(key, DES.MODE_ECB)
    return cipher.encrypt(pw)
password = os.environ.get('VNC_PASSWORD', 'unify123')
with open('/etc/vnc/passwd', 'wb') as f:
    f.write(vnc_encrypt(password))
os.chmod('/etc/vnc/passwd', 0o640)
os.system('chgrp unityuser /etc/vnc/passwd')
PYSCRIPT
        log "VNC password updated, restarting Xvnc to load new password"
        pkill -9 Xvnc 2>/dev/null || true
        sleep 2
        if ss -tlnp | grep -q ':5901 '; then
            log "Xvnc restarted and listening on port 5901"
        else
            log "WARNING: Xvnc not yet listening on port 5901 after restart"
        fi
    fi

    # Agent Service .env (owned by unityuser so the process can read it)
    # UNIFY_LOCAL_ROOT must be set explicitly: the disk is mounted/synced at
    # /Unity/Local, but the agent's default of ~/Unity/Local would resolve to
    # /Unity/Unity/Local and miss every synced file (attachments, screenshots,
    # downloads).
    cat > /agent-service/.env << EOF
PORT=3000
NODE_ENV=production
UNIFY_KEY=$unify_key
VNC_PASSWORD=$vnc_password
ORCHESTRA_URL=$orchestra_url
UNIFY_COMMS_URL=$comms_url
UNIFY_LOCAL_ROOT=/Unity/Local
PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright
DISPLAY=:1
EOF
    chown unityuser:unityuser /agent-service/.env
    chmod 600 /agent-service/.env
    log "Agent Service .env configured"

    # Supervisor owns the long-lived process and supplies the desktop's stable
    # autolaunched D-Bus. Login-scoped /run/user buses disappear after each
    # SFTP poll and Chromium aborts if it inherited one through su/PAM.
    kill_agent_service
    touch /var/log/agent-service.log
    chown unityuser:unityuser /var/log/agent-service.log
    if ! supervisorctl start services:agent-service; then
        log "ERROR: Supervisor failed to start Agent Service"
        return 1
    fi
    log "Agent Service started by Supervisor"

    log "Waiting for Agent Service on port 3000..."
    for i in $(seq 1 60); do
        if ss -tlnp | grep -q ':3000 '; then
            log "Agent Service is listening on port 3000 (after ${i}s)"
            break
        fi
        if [[ $i -eq 60 ]]; then
            log "WARNING: Agent Service not listening on port 3000 after 60s, proceeding anyway"
        fi
        sleep 1
    done

    # Wait for Caddy on port 443 before notifying (the /vm/ready endpoint probes HTTPS back)
    log "Waiting for Caddy on port 443..."
    local caddy_up=false
    for i in $(seq 1 45); do
        if ss -tlnp | grep -q ':443 '; then
            log "Caddy is listening on port 443 (after ${i}s)"
            caddy_up=true
            break
        fi
        if [[ $i -eq 15 ]]; then
            if [[ -S /var/run/supervisor.sock ]]; then
                log "Caddy not listening after 15s, restarting via supervisord..."
                supervisorctl restart services:caddy 2>/dev/null || true
            else
                log "Caddy not listening after 15s, supervisor socket absent, starting the unit..."
                # Never spawn a bare `caddy run` here: a second Caddy binds :443
                # and admin :2019 alongside supervisor's, and configure_caddy_hostname's
                # reload then reaches only one of them -- leaving the other serving
                # the pool vhost and answering assistant-host requests with an
                # empty 200. Go through the unit so there is exactly one.
                systemctl restart supervisor 2>/dev/null || true
            fi
        fi
        sleep 1
    done
    if [[ "$caddy_up" != "true" ]]; then
        log "WARNING: Caddy not listening on port 443 after 45s, proceeding anyway"
    fi

    # Send ready notification
    if [[ -n "$comms_url" && -n "$hostname" && -n "$unify_key" && -n "$assistant_id" && -n "$binding_id" ]]; then
        for attempt in $(seq 1 10); do
            local http_code
            # No -f: it makes curl exit non-zero on an error status, and the
            # shell would then append a fallback code onto the one -w already
            # printed. Without it a refused connection still reports "000".
            http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 \
                -X POST "$comms_url/infra/vm/ready" \
                -H "Content-Type: application/json" \
                -H "Authorization: Bearer $unify_key" \
                -d "{\"assistant_id\": \"$assistant_id\", \"binding_id\": \"$binding_id\", \"vm_type\": \"ubuntu\", \"hostname\": \"$hostname\"}" \
                2>/dev/null)

            if [[ "$http_code" == "200" ]]; then
                log "VM ready notification sent (attempt $attempt)"
                break
            fi
            if ready_notification_is_terminal "$http_code"; then
                log "VM ready notification rejected (HTTP $http_code), giving up"
                break
            fi
            log "VM ready notification attempt $attempt failed (HTTP $http_code), retrying in 5s..."
            sleep 5
        done
    fi

    log "ASSIGN complete"
}

# ─── Release: clean up VM for return to pool ─────────────────────────────

do_release() {
    local release_token
    release_token=$(current_release_token)
    log "RELEASE: cleaning up VM${release_token:+ (token=$release_token)}"

    # Stop Agent Service
    kill_agent_service
    log "Agent Service stopped"

    # Clear .env
    rm -f /agent-service/.env
    log "Agent Service .env cleared"

    # Clear SSH keys
    rm -f /Unity/.ssh/authorized_keys
    log "SSH authorized_keys cleared"

    # Reset VNC password to dead value
    VNC_PASSWORD="disabled" python3 << 'PYSCRIPT'
import os
from Crypto.Cipher import DES
def vnc_encrypt(password):
    key = bytes([0xe8, 0x4a, 0xd6, 0x60, 0xc4, 0x72, 0x1a, 0xe0])
    pw = (password + '\x00' * 8)[:8].encode('latin-1')
    cipher = DES.new(key, DES.MODE_ECB)
    return cipher.encrypt(pw)
password = os.environ.get('VNC_PASSWORD', 'disabled')
with open('/etc/vnc/passwd', 'wb') as f:
    f.write(vnc_encrypt(password))
os.chmod('/etc/vnc/passwd', 0o640)
os.system('chgrp unityuser /etc/vnc/passwd')
PYSCRIPT
    log "VNC password reset"

    # Archive Local/ + desktop-profile to GCS before unmount/scrub
    local assistant_id
    assistant_id=$(get_metadata "assistant-id")
    local archive_bucket
    archive_bucket=$(get_metadata "archive-bucket")
    if restore_in_flight; then
        # This release interrupted a bootstrap that was still unpacking the
        # assistant's archives, so what sits on disk is a truncated copy of
        # them and no work of its own. Uploading it would replace good
        # archives with worse ones.
        log "Assignment was interrupted mid-restore; keeping the existing archives"
        clear_restore_in_flight
    else
        if mountpoint -q /Unity/Local 2>/dev/null; then
            if [[ -n "$assistant_id" && -n "$archive_bucket" ]]; then
                local archive_path="gs://${archive_bucket}/${assistant_id}.tar.gz"
                log "Archiving /Unity/Local to $archive_path"
                if tar czf - -C /Unity/Local . | gsutil -q cp - "$archive_path" 2>/dev/null; then
                    log "Archive uploaded successfully"
                else
                    log "WARNING: archive upload failed, PD data will be preserved as fallback"
                fi
            fi
        fi
        if [[ -n "$assistant_id" && -n "$archive_bucket" ]]; then
            archive_desktop_profile "$assistant_id" "$archive_bucket"
        fi
    fi

    # Unmount persistent disk (kill busy processes first, then lazy fallback)
    if mountpoint -q /Unity/Local 2>/dev/null; then
        fuser -km /Unity/Local 2>/dev/null || true
        sleep 1
        if ! umount /Unity/Local 2>/dev/null; then
            umount -l /Unity/Local 2>/dev/null || true
            log "Lazy-unmounted /Unity/Local (was busy)"
        else
            log "Unmounted /Unity/Local"
        fi
    fi

    scrub_filesystem

    # Refreshing the code takes minutes and the pool is blocked until the
    # callback below lands, so the watcher runs that separately once this
    # returns.
    wipe_metadata_key "github-token"
    if notify_release_complete; then
        save_last_release_token "$release_token"
    else
        log "WARNING: release completion callback did not succeed; token remains unacked"
    fi

    log "RELEASE complete"
}

# ─── TLS cert refresh: update Caddy when cert metadata changes ────────────

refresh_tls() {
    local tls_cert tls_key
    tls_cert=$(get_metadata "tls-fullchain")
    tls_key=$(get_metadata "tls-privkey")

    if [[ -z "$tls_cert" || -z "$tls_key" ]]; then
        return
    fi

    local new_hash
    new_hash=$(echo -n "$tls_cert" | md5sum | cut -d' ' -f1)

    if [[ "$new_hash" == "$PREV_TLS_HASH" ]]; then
        return
    fi

    log "TLS cert changed, updating Caddy certs"
    mkdir -p /etc/caddy/certs
    echo "$tls_cert" > /etc/caddy/certs/fullchain.pem
    echo "$tls_key" > /etc/caddy/certs/privkey.pem
    chmod 600 /etc/caddy/certs/privkey.pem

    if ss -tlnp | grep -q ':443 '; then
        caddy reload --config /etc/caddy/Caddyfile 2>/dev/null && \
            log "Caddy reloaded with new cert" || \
            log "WARNING: Caddy reload failed"
    fi

    PREV_TLS_HASH="$new_hash"
}

main() {
    log "Unity Pool Watcher starting"

    # Seed TLS hash to avoid unnecessary reload on first loop iteration
    _init_tls=$(get_metadata "tls-fullchain")
    if [[ -n "$_init_tls" ]]; then
        PREV_TLS_HASH=$(echo -n "$_init_tls" | md5sum | cut -d' ' -f1)
    fi

    # Pre-fetch etag so the first long-poll has a valid value and won't block
    # on already-set metadata. The first pass reads current state before it
    # polls, which is what handles assignments made before the watcher started.
    ETAG=$(read_metadata_etag)

    while true; do
        CURRENT_UNIFY_KEY=$(get_metadata "unify-key")
        CURRENT_RELEASE_TOKEN=$(current_release_token)
        LAST_HANDLED_RELEASE_TOKEN=$(load_last_release_token)

        # A release and an assignment are never both pending: releasing is
        # signalled by clearing unify-key, assigning by setting it.
        if should_trigger_release "$PREV_UNIFY_KEY" "$CURRENT_UNIFY_KEY" "$CURRENT_RELEASE_TOKEN" "$LAST_HANDLED_RELEASE_TOKEN"; then
            cancel_job "release requested"
            # Release runs here rather than as a job: it is what the pool is
            # waiting on, and an archive upload must never be cut in half.
            do_release
            # Warm the next assignment while the VM is idle. do_assign
            # supersedes this, so a quick re-claim never waits on it.
            start_job update do_update
        elif [[ "$CURRENT_UNIFY_KEY" != "$PREV_UNIFY_KEY" && -n "$CURRENT_UNIFY_KEY" ]]; then
            start_job assign do_assign "$CURRENT_UNIFY_KEY"
        fi

        PREV_UNIFY_KEY="$CURRENT_UNIFY_KEY"

        # Refresh TLS cert if metadata changed (handles renewal pushes)
        refresh_tls

        wait_for_metadata_change
    done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
