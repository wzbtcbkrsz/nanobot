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

pikpak_bootstrap() {
    PIKPAK_LOCAL_ROOT="${PIKPAK_LOCAL_ROOT:-/data/nanobot-home}"
    PIKPAK_REMOTE_PATH="${PIKPAK_REMOTE_PATH:-nanobot/.nanobot}"
    PIKPAK_SYNC_INTERVAL="${PIKPAK_SYNC_INTERVAL:-300}"
    PIKPAK_CONF_DIR="${PIKPAK_CONF_DIR:-/tmp/nanobot-pikpak}"
    PIKPAK_SYNC_LOOP="${PIKPAK_SYNC_LOOP:-1}"
    # Keep the startup pull well inside the platform's health-check grace
    # period: a stalled remote must not consume the whole window before the
    # gateway gets a chance to bind. Hosted services degrade after this.
    PIKPAK_PULL_TIMEOUT="${PIKPAK_PULL_TIMEOUT:-20}"
    # Hosted web services set this to 1 (see entrypoint.sh): the gateway must
    # bind its health-check port even when the remote store is slow or down.
    PIKPAK_ALLOW_PARTIAL_START="${PIKPAK_ALLOW_PARTIAL_START:-0}"

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

    # The pikpak backend requires an obscured password. Keep the plaintext only
    # in PIKPAK_PASSWORD, which the platform injects as an environment variable.
    obscured_pass=$(rclone obscure "$PIKPAK_PASSWORD") || return 1

    # PikPak needs a real login on first use (device id, captcha token, OAuth
    # token). `rclone config create` performs it non-interactively and writes
    # those values into the config file. Defining the remote with only
    # RCLONE_CONFIG_PIKPAK_* env vars skips that login, and rclone then fails
    # with "empty token found - please run rclone config reconnect". Suppress
    # the output: it contains the freshly issued token.
    export RCLONE_CONFIG="$PIKPAK_CONF"
    export RCLONE_CACHE_DIR="$PIKPAK_CACHE_DIR"
    rm -f "$PIKPAK_CONF" || return 1
    rclone config create pikpak pikpak user "$PIKPAK_USER" pass "$obscured_pass" \
        >/dev/null 2>&1 || return 1

    # rclone copy fails when the remote path does not exist, which is the normal
    # state on the very first deployment. mkdir is idempotent; ignore its error.
    rclone mkdir "$PIKPAK_REMOTE_SPEC" >/dev/null 2>&1 || true

    pikpak_log "pulling $PIKPAK_REMOTE_SPEC into $PIKPAK_DATA_DIR"
    # `timeout` is a hard ceiling. rclone's --timeout is per request, and with
    # --retries a slow or stalled remote can otherwise keep the gateway from
    # binding for several minutes, which fails the platform health check.
    if ! timeout "$PIKPAK_PULL_TIMEOUT" rclone copy "$PIKPAK_REMOTE_SPEC" "$PIKPAK_DATA_DIR" \
            --exclude "/logs/**" \
            --retries 3 --timeout 60s --transfers 4 --checkers 8 \
            --log-level ERROR; then
        if [ "$PIKPAK_ALLOW_PARTIAL_START" != "1" ]; then
            pikpak_warn "initial pull failed — refusing to start with an incomplete data dir"
            return 1
        fi
        pikpak_warn "initial pull failed or timed out after ${PIKPAK_PULL_TIMEOUT}s — starting the gateway with the platform config template; persistent state may be incomplete"
        PIKPAK_START_DEGRADED=1
        export PIKPAK_START_DEGRADED
    fi

    chown -R nanobot:nanobot "$PIKPAK_LOCAL_ROOT" "$PIKPAK_CONF_DIR" 2>/dev/null || true

    # All nanobot state is derived from $HOME/.nanobot (nanobot/config/paths.py),
    # so pointing HOME here puts config.json, sessions, memory, cron, media,
    # logs and WebUI history on the PikPak-backed directory.
    export HOME="$PIKPAK_LOCAL_ROOT"
    export PIKPAK_DATA_DIR PIKPAK_REMOTE_SPEC PIKPAK_ORIGINAL_HOME PIKPAK_LOCAL_ROOT
    export PIKPAK_CONF_DIR PIKPAK_CACHE_DIR PIKPAK_SYNC_LOG PIKPAK_SYNC_INTERVAL
    export PIKPAK_SYNC_LOOP
    pikpak_link_legacy_home
    return 0
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
