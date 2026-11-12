#!/bin/sh

# Render and Koyeb keep environment references in their deployment templates.
# Local Compose generates and validates its initial config through Python instead.
# Koyeb publishes KOYEB_PUBLIC_DOMAIN to every web service, so it identifies the
# platform on its own; KOYEB=true selects the same template explicitly. The
# templates ship in the image code dir; NANOBOT_CONFIG_TEMPLATE_DIR points the
# entrypoint at a different copy. Resolve the template before the PikPak
# bootstrap: hosted web services use it to keep every PikPak network operation
# off the health-check startup path.
template_dir="${NANOBOT_CONFIG_TEMPLATE_DIR:-/app}"
template=""
if [ "$RENDER" = "true" ]; then
    template=render-config.json
elif [ "$KOYEB" = "true" ] || [ -n "$KOYEB_PUBLIC_DOMAIN" ]; then
    template=koyeb-config.json
fi
if [ -n "$template" ]; then
    export PIKPAK_DEFER_PULL=1
fi

# Optional PikPak-backed persistent storage for hosts without a durable volume.
# Active only when both credentials are supplied, so existing Render/Koyeb/
# Compose deployments keep their current disk/volume behaviour unchanged.
pikpak_enabled=0
if [ -n "$PIKPAK_USER" ] && [ -n "$PIKPAK_PASSWORD" ]; then
    pikpak_script="${NANOBOT_PIKPAK_SCRIPT:-/usr/local/bin/pikpak-storage.sh}"
    if [ ! -f "$pikpak_script" ]; then
        echo "[entrypoint] error: PikPak credentials are set but $pikpak_script is missing" >&2
        exit 1
    fi
    . "$pikpak_script"
    # The privilege drop below re-execs this script. Bootstrap only the first
    # pass, but re-source the functions so pikpak_final_sync stays available.
    if [ -z "$NANOBOT_PIKPAK_BOOTSTRAPPED" ]; then
        export NANOBOT_PIKPAK_BOOTSTRAPPED=1
        pikpak_bootstrap || exit 1
    fi
    pikpak_enabled=1
fi

dir="$HOME/.nanobot"

# Drop privileges whenever the container starts as root. Render mounts the
# persistent disk root-owned, and a plain `docker run` also defaults to root now,
# so this covers both. Chown the data dir so the non-root user can write it, then
# re-exec as nanobot. Fail closed: if the privilege drop cannot be performed,
# exit rather than run the agent as root.
if [ "$(id -u)" = "0" ]; then
    chown -R nanobot:nanobot "$dir" 2>/dev/null || echo "[entrypoint] warning: chown $dir failed"
    if setpriv --reuid=nanobot --regid=nanobot --init-groups true 2>/dev/null; then
        echo "[entrypoint] dropping privileges to nanobot via setpriv"
        exec setpriv --reuid=nanobot --regid=nanobot --init-groups "$0" "$@"
    fi
    echo "[entrypoint] error: started as root but setpriv privilege drop failed — refusing to run as root" >&2
    exit 1
fi

if [ -d "$dir" ] && [ ! -w "$dir" ]; then
    owner_uid=$(stat -c %u "$dir" 2>/dev/null || stat -f %u "$dir" 2>/dev/null)
    cat >&2 <<EOF
Error: $dir is not writable (owned by UID $owner_uid, running as UID $(id -u)).

Fix (pick one):
  Host:   sudo chown -R 1000:1000 ~/.nanobot
  Docker: docker run --user \$(id -u):\$(id -g) ...
  Podman: podman run --userns=keep-id ...
EOF
    exit 1
fi

if [ -n "$template" ]; then
    config="$dir/config.json"
    mkdir -p "$dir" || exit 1
    # The deferred pull excludes config.json, so a stale remote config can
    # never replace the platform template after the gateway has started.
    if [ ! -f "$config" ]; then
        echo "[entrypoint] initializing $config from $template"
        cp "$template_dir/$template" "$config" || exit 1
    else
        echo "[entrypoint] existing $config found — leaving it in place"
    fi
    # The image default CMD (`webui --yes --no-open`) owns a local desktop
    # session: it starts a background gateway bound to 127.0.0.1 and waits for
    # Ctrl+C, so nothing listens on the container interface and the platform
    # health check cannot reach the port even though `webui` reports "ready".
    # Hosted web services need the gateway itself in the foreground on 0.0.0.0.
    # Normalize only the exact default command; an explicit command always wins.
    if [ "$#" -eq 3 ] && [ "$1" = "webui" ] && [ "$2" = "--yes" ] && [ "$3" = "--no-open" ]; then
        set -- gateway
    fi
    set -- "$@" --config "$config"
elif [ "$NANOBOT_DOCKER_BOOTSTRAP" = "1" ] && { [ "$1" = "gateway" ] || [ "$1" = "serve" ]; }; then
    # Explicit config selectors belong to the CLI; bootstrap only the default instance.
    initialize_default=1
    for argument in "$@"; do
        case "$argument" in
            --config|--config=*|-c*) initialize_default=0; break ;;
        esac
    done
    if [ "$initialize_default" = "1" ]; then
        config="$dir/config.json"
        python -m nanobot.cli.docker_bootstrap "$config" || exit 1
        set -- "$@" --config "$config"
    fi
fi

if [ "$pikpak_enabled" = "1" ]; then
    # Keep this shell as PID 1 so a final upload runs after the gateway exits;
    # nanobot itself becomes a child and receives forwarded termination signals.
    pikpak_start_sync_loop
    nanobot "$@" &
    app_pid=$!
    shutting_down=0
    trap 'shutting_down=1; kill -TERM "$app_pid" 2>/dev/null' TERM INT

    if [ "$PIKPAK_DEFER_PULL" = "1" ]; then
        # The gateway is already answering health checks on the platform port.
        # Restore persisted state in the background, wait for it, then restart
        # the gateway once: only that second start is the real, fully loaded
        # instance. A shutdown signal during the restore skips the restart.
        pikpak_start_deferred_pull
        wait "$PIKPAK_PULL_PID" 2>/dev/null || true
        if [ "$shutting_down" != "1" ] && kill -0 "$app_pid" 2>/dev/null; then
            pikpak_log "state restore finished — restarting the gateway"
            kill -TERM "$app_pid" 2>/dev/null
            wait "$app_pid" 2>/dev/null || true
            nanobot "$@" &
            app_pid=$!
        fi
    fi

    while :; do
        if wait "$app_pid"; then
            app_status=0
            break
        fi
        app_status=$?
        # A trapped signal interrupts wait while the child is still alive.
        if ! kill -0 "$app_pid" 2>/dev/null; then
            break
        fi
    done
    trap - TERM INT
    pikpak_final_sync
    exit "$app_status"
fi

exec nanobot "$@"
