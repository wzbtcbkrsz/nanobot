#!/bin/sh
# PikPak-backed persistent storage for the nanobot container.
#
# Sourced by entrypoint.sh. This file only defines functions: the entrypoint is
# re-exec'd once via setpriv to drop privileges, so sourcing it twice must be
# free of side effects. Free PaaS tiers (Koyeb included) do not expose
# /dev/fuse, so persistence uses rclone copy instead of an rclone mount.

pikpak_log() {
    printf '[pikpak] %s\n' "$*"
}

pikpak_warn() {
    printf '[pikpak] warning: %s\n' "$*" >&2
}

# Upload local state. Use copy, never sync: a transient local problem must not
# delete the persistent copy. Local deletions can reappear after a fresh pull;
# that tradeoff is deliberate for a single-instance free deployment.
# Logs are excluded from every transfer: they are regenerated on each boot and
# grow without bound (LLM retries, WebUI traffic), so persisting them only makes
# the next startup pull slower. The initial pull uses the same filter so logs
# left on the remote by an older image never come back.
pikpak_sync_up() {
    rclone copy "$PIKPAK_DATA_DIR" "$PIKPAK_REMOTE_SPEC" \
        --exclude "/logs/**" \
        --retries 3 --timeout 60s --transfers 4 --checkers 8 \
        --log-level ERROR
}

# Keep the image's conventional data path usable too (bind mounts, Render disk,
# docs). Only replace an empty directory; never touch a populated volume.
pikpak_link_legacy_home() {
    legacy="${PIKPAK_ORIGINAL_HOME:-/home/nanobot}/.nanobot"
    if [ -L "$legacy" ]; then
        return 0
    fi
    if [ -d "$legacy" ]; then
        if [ -n "$(ls -A "$legacy" 2>/dev/null)" ]; then
            pikpak_warn "$legacy holds data (bind mount or volume); leaving it untouched"
            return 0
        fi
        rmdir "$legacy" 2>/dev/null || return 0
    fi
    ln -s "$PIKPAK_DATA_DIR" "$legacy" 2>/dev/null || \
        pikpak_warn "could not link $legacy to $PIKPAK_DATA_DIR"
}

# PikPak needs a real login on first use (device id, captcha token, OAuth
# token). `rclone config create` performs it non-interactively and writes those
# values into rclone.conf; defining the remote with only RCLONE_CONFIG_PIKPAK_*
# env vars skips that login and leaves an empty token. Suppress the output: it
# contains the freshly issued token.
pikpak_login() {
    obscured_pass=$(rclone obscure "$PIKPAK_PASSWORD") || return 1
    rm -f "$PIKPAK_CONF" || return 1
    rclone config create pikpak pikpak user "$PIKPAK_USER" pass "$obscured_pass" \
        >/dev/null 2>&1 || return 1
    # rclone copy fails when the remote path does not exist, which is normal on
    # the very first deployment. mkdir is idempotent; ignore its error.
    rclone mkdir "$PIKPAK_REMOTE_SPEC" >/dev/null 2>&1 || true
    return 0
}

# Pull persisted state. Callers may pass extra --exclude flags: hosted platforms
# exclude config.json so a stale remote config cannot replace the platform
# template the gateway is already running with. Logs are always excluded.
pikpak_pull_state() {
    pikpak_log "pulling $PIKPAK_REMOTE_SPEC into $PIKPAK_DATA_DIR"
    set -- --exclude "/logs/**" "$@"
    timeout "$PIKPAK_PULL_TIMEOUT" rclone copy "$PIKPAK_REMOTE_SPEC" "$PIKPAK_DATA_DIR" \
        "$@" \
        --retries 3 --timeout 60s --transfers 4 --checkers 8 \
        --log-level ERROR
}

pikpak_bootstrap() {
    PIKPAK_LOCAL_ROOT="${PIKPAK_LOCAL_ROOT:-/data/nanobot-home}"
    PIKPAK_REMOTE_PATH="${PIKPAK_REMOTE_PATH:-nanobot/.nanobot}"
    PIKPAK_SYNC_INTERVAL="${PIKPAK_SYNC_INTERVAL:-300}"
    PIKPAK_CONF_DIR="${PIKPAK_CONF_DIR:-/tmp/nanobot-pikpak}"
    PIKPAK_SYNC_LOOP="${PIKPAK_SYNC_LOOP:-1}"
    # The deferred pull runs after the gateway is already serving, so it may
    # take longer than the startup window. Still bound a stalled remote.
    PIKPAK_PULL_TIMEOUT="${PIKPAK_PULL_TIMEOUT:-120}"
    # Hosted web services set this to 1 (see entrypoint.sh) to keep every
    # PikPak network operation off the health-check startup path.
    PIKPAK_DEFER_PULL="${PIKPAK_DEFER_PULL:-0}"

    if ! command -v rclone >/dev/null 2>&1; then
        pikpak_warn "rclone is not installed — refusing to start without persistent storage"
        return 1
    fi

    PIKPAK_DATA_DIR="$PIKPAK_LOCAL_ROOT/.nanobot"
    PIKPAK_REMOTE_SPEC="pikpak:$PIKPAK_REMOTE_PATH"
    PIKPAK_CONF="$PIKPAK_CONF_DIR/rclone.conf"
    PIKPAK_CACHE_DIR="$PIKPAK_CONF_DIR/cache"
    PIKPAK_SYNC_LOG="$PIKPAK_CONF_DIR/sync.log"
    PIKPAK_ORIGINAL_HOME="$HOME"

    # rclone copy does not create the destination when the remote is empty
    # (first deployment), so make the data dir up front.
    mkdir -p "$PIKPAK_CONF_DIR" "$PIKPAK_CACHE_DIR" "$PIKPAK_LOCAL_ROOT" "$PIKPAK_DATA_DIR" || return 1

    export RCLONE_CONFIG="$PIKPAK_CONF"
    export RCLONE_CACHE_DIR="$PIKPAK_CACHE_DIR"
    chown -R nanobot:nanobot "$PIKPAK_LOCAL_ROOT" "$PIKPAK_CONF_DIR" 2>/dev/null || true

    # All nanobot state is derived from $HOME/.nanobot (nanobot/config/paths.py),
    # so pointing HOME here puts config.json, sessions, memory, cron, media,
    # logs and WebUI history on the PikPak-backed directory. This must happen
    # even when the pull is deferred: the gateway needs the data dir to exist.
    export HOME="$PIKPAK_LOCAL_ROOT"
    export PIKPAK_DATA_DIR PIKPAK_REMOTE_SPEC PIKPAK_ORIGINAL_HOME PIKPAK_LOCAL_ROOT
    export PIKPAK_CONF PIKPAK_CONF_DIR PIKPAK_CACHE_DIR PIKPAK_SYNC_LOG
    export PIKPAK_SYNC_INTERVAL PIKPAK_SYNC_LOOP PIKPAK_PULL_TIMEOUT PIKPAK_DEFER_PULL
    pikpak_link_legacy_home

    # Local/Compose deployments keep pulling state before startup and abort if
    # it fails. Hosted web services defer both login and pull to after the
    # gateway has bound its health-check port.
    if [ "$PIKPAK_DEFER_PULL" != "1" ]; then
        pikpak_login || {
            pikpak_warn "PikPak login failed — refusing to start with an incomplete data dir"
            return 1
        }
        if ! pikpak_pull_state; then
            pikpak_warn "initial pull failed — refusing to start with an incomplete data dir"
            return 1
        fi
    fi
    return 0
}

# Called after the gateway process has been started. Nothing here can delay the
# health-check bind; output goes to the sync log rather than container stderr.
pikpak_start_deferred_pull() {
    [ "$PIKPAK_DEFER_PULL" = "1" ] || return 0
    (
        # Let the first gateway finish binding before any state lands in the
        # data dir; the health check must see a live port during the restore.
        sleep 2
        if ! pikpak_login; then
            pikpak_warn "deferred PikPak login failed — persistent state unavailable"
            exit 0
        fi
        if ! pikpak_pull_state --exclude "/config.json"; then
            pikpak_warn "deferred pull failed or timed out after ${PIKPAK_PULL_TIMEOUT}s — persistent state may be incomplete"
        fi
    ) >>"$PIKPAK_SYNC_LOG" 2>&1 &
    PIKPAK_PULL_PID=$!
    export PIKPAK_PULL_PID
    pikpak_log "deferred PikPak pull started (pid $PIKPAK_PULL_PID)"
}

# Started only after every other startup check has passed, so a failed
# privilege drop or config seed cannot leave a background uploader behind.
pikpak_start_sync_loop() {
    [ "$PIKPAK_SYNC_LOOP" = "1" ] || return 0
    (
        while :; do
            sleep "$PIKPAK_SYNC_INTERVAL"
            pikpak_sync_up || pikpak_warn "periodic sync failed"
        done
    ) >>"$PIKPAK_SYNC_LOG" 2>&1 &
    PIKPAK_SYNC_PID=$!
    export PIKPAK_SYNC_PID
    pikpak_log "periodic upload every ${PIKPAK_SYNC_INTERVAL}s (pid $PIKPAK_SYNC_PID)"
}

pikpak_final_sync() {
    if [ -n "$PIKPAK_SYNC_PID" ]; then
        kill "$PIKPAK_SYNC_PID" 2>/dev/null || true
        wait "$PIKPAK_SYNC_PID" 2>/dev/null || true
    fi
    pikpak_log "final upload before exit"
    pikpak_sync_up || pikpak_warn "final upload failed"
}
