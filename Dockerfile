FROM rclone/rclone:1.76.0 AS rclone-bin

FROM node:24-bookworm-slim AS webui-builder

WORKDIR /app
COPY webui/package.json webui/package-lock.json ./webui/
WORKDIR /app/webui
RUN npm ci
COPY webui/ ./
COPY packages/client-events/ /app/packages/client-events/
COPY nanobot/channels/ /app/nanobot/channels/
RUN mkdir -p /app/nanobot/web && npm run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# rclone backs the PikPak persistent store for hosts without a durable volume.
# Copy the static binary from the official multi-arch image instead of
# downloading a zip: hosted build sandboxes often cannot reach
# downloads.rclone.org, and the image tag lets buildx pick the right arch.
COPY --from=rclone-bin /usr/local/bin/rclone /usr/local/bin/rclone
RUN chmod 0755 /usr/local/bin/rclone && rclone version

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl git bubblewrap openssh-client libmagic1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Keep the runtime environment writable by the non-root nanobot user. Enabled
# channels may install their manifest-declared dependencies at startup.
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"
RUN uv venv --seed "$VIRTUAL_ENV"

ARG NANOBOT_EXTRAS=
COPY pyproject.toml ./
RUN set -- && \
    for extra in $(printf '%s' "$NANOBOT_EXTRAS" | tr ',' ' '); do \
        set -- "$@" --extra "$extra"; \
    done && \
    uv pip install --python "$VIRTUAL_ENV/bin/python" --no-cache -r pyproject.toml "$@"

COPY README.md LICENSE THIRD_PARTY_NOTICES.md hatch_build.py ./
COPY docs/ docs/
COPY nanobot/ nanobot/
COPY scripts/install_channel_dependencies.py scripts/
COPY --from=webui-builder /app/nanobot/web/dist/ nanobot/web/dist/
RUN NANOBOT_SKIP_WEBUI_BUILD=1 uv pip install --python "$VIRTUAL_ENV/bin/python" --no-cache --no-deps .

ARG NANOBOT_CHANNELS=whatsapp
RUN for channel in $(printf '%s' "$NANOBOT_CHANNELS" | tr ',' ' '); do \
        python -m scripts.install_channel_dependencies "$channel"; \
    done

# Render deploy template (see render.yaml): committed gateway config that wires
# secrets through ${ANTHROPIC_API_KEY} / ${NANOBOT_WEB_TOKEN} env vars (resolved
# at startup). Lives in the code dir (/app), not the data dir, so a mounted disk
# won't shadow it. Only used when RENDER=true; ignored by local runs.
COPY render-config.json ./

# Koyeb deploy template (see koyeb-compose.yaml): same shape as the Render
# template, plus the platform-provided ${PORT} and ${KOYEB_PUBLIC_DOMAIN}. The
# edge terminates TLS and does not forward x-forwarded-proto, so publicWsUrl
# pins the browser-facing wss:// endpoint instead of deriving it from headers.
COPY koyeb-config.json ./

RUN useradd -m -u 1000 -s /bin/bash nanobot && \
    mkdir -p /home/nanobot/.nanobot /data/nanobot-home && \
    chown -R nanobot:nanobot /home/nanobot /data/nanobot-home /app/.venv

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY pikpak-storage.sh /usr/local/bin/pikpak-storage.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh /usr/local/bin/pikpak-storage.sh && \
    chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/pikpak-storage.sh

# Start as root so the entrypoint can chown the data dir (on Render, the
# freshly-mounted root-owned persistent disk) before dropping to the non-root
# nanobot user via setpriv. The entrypoint drops privileges on every root start
# and fails closed if it cannot, so the agent never runs as root (see
# entrypoint.sh).
USER root
ENV HOME=/home/nanobot
# PikPak persistence is opt-in: entrypoint.sh activates it only when both
# PIKPAK_USER and PIKPAK_PASSWORD are injected at runtime. Credentials are
# never baked into the image (.env is git-ignored and .dockerignore'd).
ENV PIKPAK_LOCAL_ROOT=/data/nanobot-home \
    PIKPAK_REMOTE_PATH=nanobot/.nanobot \
    PIKPAK_SYNC_INTERVAL=30
# Ensure crash output reaches Render logs (app output is otherwise swallowed on
# non-graceful exit).
ENV PYTHONUNBUFFERED=1 PYTHONFAULTHANDLER=1
# Keep container logs quiet: only WARNING/ERROR/CRITICAL reach stderr. Outside
# the image the console level stays INFO, and --verbose still selects DEBUG.
ENV NANOBOT_LOG_LEVEL=WARNING

EXPOSE 18790 8765

ENTRYPOINT ["entrypoint.sh"]
# Start the WebUI and its gateway in the foreground. --yes skips the first-run
# prompt when there is no TTY; --no-open avoids a browser in the headless
# container and prints the signed login link to the container logs instead.
# On hosted web services (Render/Koyeb) entrypoint.sh swaps this interactive
# launcher for the foreground `gateway` command, which binds 0.0.0.0.
CMD ["webui", "--yes", "--no-open"]
