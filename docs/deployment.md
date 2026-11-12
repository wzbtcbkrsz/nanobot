# Deployment

Use this page to keep an already working agent online, or start with the [Docker setup](#docker-compose) on a new headless host. A normal WebUI reply is sufficient to confirm an existing model setup; a CLI check is useful for terminal and container deployments. Deployment keeps long-running surfaces online: WebUI, chat apps, heartbeat, Dream, cron jobs, and channel connections.

## Before You Deploy

Check these before making a deployment persistent. For a new container host, perform them inside the container after the initialization steps below:

| Check | Why it matters |
|---|---|
| `nanobot status` shows the expected config and workspace | Confirms the process will read the instance you meant to run |
| `nanobot agent -m "Hello!"` works | Proves install, config, provider, model, and workspace writes before adding a service layer |
| Secrets are in environment variables or protected config files | API keys, bot tokens, OAuth state, and chat credentials should not be world-readable |
| The active config directory (including `sessions/`) and workspace are persistent | Sessions follow `--config`; memory, generated artifacts, and the workspace identity marker follow the workspace |
| Channel access control is intentional | Use `allowFrom`, pairing, WebSocket `token`/`tokenIssueSecret`, or private test channels before exposing the bot |
| Ports are planned | Gateway health defaults to local-only `127.0.0.1:18790`; WebUI/WebSocket defaults to `8765`; `nanobot serve` defaults to `8900` |
| Logs are easy to reach | Use `docker compose logs`, `journalctl`, LaunchAgent log files, or `nanobot gateway --verbose` while diagnosing startup |

Restart the deployed process after editing `config.json`. Long-running processes read config at startup.

## Choose a Runtime

| Runtime | Use it for | State location | Useful first command |
|---|---|---|---|
| Render | One-click hosted gateway and WebUI | Persistent disk at `/home/nanobot/.nanobot` | [Deploy to Render](#render) |
| Koyeb | Hosted gateway and WebUI behind Koyeb's edge | Volume, or PikPak-backed `$HOME/.nanobot` on the free tier | [Deploy to Koyeb](#koyeb) |
| Docker Compose | Repeatable container runs on Linux servers or workstations | Bind-mount `~/.nanobot` to `/home/nanobot/.nanobot` | `docker compose run --rm nanobot-cli agent -m "Hello!"` |
| Docker CLI | Manual container testing or small one-off hosts | Bind-mount `~/.nanobot` to `/home/nanobot/.nanobot` | `docker run -v ~/.nanobot:/home/nanobot/.nanobot --rm nanobot status` |
| systemd user service | Linux user-level gateway that restarts automatically | Host user's `~/.nanobot` unless you pass explicit paths | `systemctl --user status nanobot-gateway` |
| macOS LaunchAgent | macOS gateway that starts after login | Host user's `~/.nanobot` unless the plist passes explicit paths | `launchctl list | grep ai.nanobot.gateway` |

## Render

Run nanobot online without managing a server. The blueprint deploys the gateway and bundled WebUI together, with a persistent disk so sessions, memory, and chat history survive restarts.

> [!IMPORTANT]
> This setup requires a paid Render service because persistent disks are not available on the free tier. During setup, provide `ANTHROPIC_API_KEY` and set `NANOBOT_WEB_TOKEN` to a strong private password (for example, generate one with `openssl rand -hex 32`).

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/HKUDS/nanobot)

[Review the deployment blueprint](../render.yaml)

### First Deployment

1. Click **Deploy to Render**, sign in, and review the Blueprint. It creates one Starter web service and a 1 GB persistent disk.
2. Enter your `ANTHROPIC_API_KEY`. Set `NANOBOT_WEB_TOKEN` to a new random value and save it in your password manager; this is the password for the public WebUI.
3. Create the Blueprint and wait for the service status to become **Live**. The first build can take several minutes.
4. Open the generated `onrender.com` URL. The **Authentication required** page means the gateway is running: enter the same `NANOBOT_WEB_TOKEN` value to open the WebUI.

The model API key is used by nanobot to call Anthropic. The Web token only protects access to this deployment; do not share it in issues, screenshots, or chat.

### Updates and Data

The Blueprint disables automatic deploys so upstream repository changes do not unexpectedly restart your agent. To update, open the service in the Render Dashboard and choose **Manual Deploy → Deploy latest commit**.

The persistent disk keeps `config.json`, sessions, memory, WebUI history, cron state, media, and logs across restarts and updates. The deployment initializes `config.json` only when it does not already exist, so settings changed later in the WebUI are not replaced on every boot.

If deployment fails, open the service **Logs** page first. A missing model key fails provider requests after startup, while an incorrect Web token leaves you on the authentication page.

## Koyeb

Run nanobot online on Koyeb as a web service. The deployment descriptor builds the image from this repository, routes the public domain to the WebUI, and keeps the gateway in the foreground.

> [!IMPORTANT]
> Create the four secrets the descriptor references before deploying: `ANTHROPIC_API_KEY` for the model API, `NANOBOT_WEB_TOKEN` as the password for the public WebUI, and `PIKPAK_USER` / `PIKPAK_PASSWORD` for the free-tier persistent store. Generate the web token with `openssl rand -hex 32` and keep it private.

[Review the deployment descriptor](../koyeb-compose.yaml)

### First Deployment

1. Install the [Koyeb CLI](https://www.koyeb.com/docs/build-and-deploy/cli/installation) and authenticate with `koyeb login`.
2. Create the secrets: `koyeb secrets create anthropic-api-key --value "$ANTHROPIC_API_KEY"`, `koyeb secrets create nanobot-web-token --value "$(openssl rand -hex 32)"`, plus the two PikPak secrets shown in [Persistent Data on the Free Tier](#persistent-data-on-the-free-tier).
3. Run `koyeb compose ./koyeb-compose.yaml` from a clone of this repository. Koyeb builds the image from the GitHub source named in the descriptor, so the first build compiles the WebUI and can take several minutes.
4. Open the generated `https://<app>-<organization>-<hash>.koyeb.app` URL. The **Authentication required** page means the gateway is running: enter the `NANOBOT_WEB_TOKEN` value to open the WebUI.

The model API key is used by nanobot to call Anthropic. The web token only protects access to this deployment; do not share it in issues, screenshots, or chat.

To deploy a prebuilt image instead of building from the repository, replace the `git` block of the descriptor with `docker: {image: <registry>/<image>:<tag>}` and keep the entrypoint, command, ports, and routes unchanged. The same settings apply when you create the service in the control panel: a **Web** service, port `8765` with the HTTP protocol, the route `/`, a TCP health check on `8765`, and the environment variables from the descriptor. Leave the Docker command empty — the entrypoint replaces the image's interactive `webui` default with the foreground `gateway` — or set it to `gateway` explicitly.

### Ports, TLS, and the WebSocket URL

Koyeb always defines `PORT` for web services, so the WebUI channel binds to `${PORT}` instead of a hard-coded port. Koyeb terminates TLS at the edge and forwards `x-forwarded-for` and `x-forwarded-host`, but it does not forward `x-forwarded-proto`. The template therefore pins `channels.websocket.publicWsUrl` to `wss://${KOYEB_PUBLIC_DOMAIN}/`; without it the WebUI would offer the browser a plaintext `ws://` socket on an HTTPS page. Replace that value with your own `wss://` endpoint when you serve nanobot from a custom domain.

The image selects the Koyeb template on its own, through the `KOYEB_PUBLIC_DOMAIN` variable the platform publishes to every web service. `KOYEB=true` selects the same template explicitly.

Route `/` to the WebUI port. Koyeb strips the route prefix from incoming requests, so a subpath route would move the WebSocket upgrade path away from the path configured in `channels.websocket.path`.

### Persistent Data

Sessions, memory, WebUI history, cron state, and logs live in `/home/nanobot/.nanobot`. Without a volume, Koyeb discards them whenever the instance is replaced. Koyeb volumes are regional, work with a single replica, and are only available in the `was` and `fra` regions:

```bash
koyeb volumes create nanobot-data --region fra --size 5
koyeb services update nanobot/nanobot --volumes nanobot-data:/home/nanobot/.nanobot
```

The volume must sit in the same region as the service, so keep the `fra` region of the descriptor when you attach one. The deployment initializes `config.json` only when it does not already exist, so settings changed later in the WebUI are not replaced on every boot.

### Persistent Data on the Free Tier

Koyeb volumes require a paid instance, and local storage is ephemeral on the free tier, so `config.json`, sessions, memory, and WebUI history are otherwise lost whenever the instance is replaced. Set `PIKPAK_USER` and `PIKPAK_PASSWORD` as secrets to persist them in a PikPak drive instead; the image ships rclone for this. No `/dev/fuse` or privileged container is needed, because Koyeb free Web Services cannot expose FUSE devices — the entrypoint uses rclone `copy` rather than an rclone mount.

Before starting the gateway, the entrypoint pulls `pikpak:nanobot/.nanobot` into `/data/nanobot-home/.nanobot`, points `HOME` at `/data/nanobot-home`, and links the conventional `/home/nanobot/.nanobot` path to it (an existing populated volume is never replaced). All nanobot state derives from `$HOME/.nanobot`, so config, sessions, memory, cron, media, and WebUI history are covered. The `logs/` directory is excluded in both directions: logs are regenerated on every boot and grow without bound (LLM retries, WebUI traffic), and a large log directory was the main reason cold starts slowed down after a few restarts. A background loop uploads local changes every `PIKPAK_SYNC_INTERVAL` seconds (image default 300), and the entrypoint performs one final upload after the gateway exits. On hosted web services (Render/Koyeb), the initial pull is bounded by `PIKPAK_PULL_TIMEOUT` (default 20s) and is best-effort: a failure or timeout logs a warning, reseeds the platform config template, and starts the gateway anyway, so a slow or unavailable PikPak cannot keep the health-check port closed. The 20s ceiling leaves most of even the default 60-second health-check grace period for the gateway to bind. State from that run may be incomplete and is uploaded again by the periodic loop. Local Compose and other deployments keep the strict fail-closed behaviour and abort rather than start with an empty data directory. Logs already stored on the remote by an older image are never pulled again; remove them once with `rclone purge pikpak:nanobot/.nanobot/logs` if you need the space.

```bash
koyeb secrets create pikpak-user --value "$PIKPAK_USER"
koyeb secrets create pikpak-password --value "$PIKPAK_PASSWORD"
koyeb services redeploy nanobot/nanobot
```

The two credentials are the only required settings. Optional overrides are `PIKPAK_LOCAL_ROOT` (default `/data/nanobot-home`), `PIKPAK_REMOTE_PATH` (default `nanobot/.nanobot`), `PIKPAK_SYNC_INTERVAL` (default `300`, in seconds), `PIKPAK_PULL_TIMEOUT` (default `20`, in seconds, the hard ceiling on the startup pull), and `PIKPAK_SYNC_LOOP=0` to disable periodic uploads and keep only the startup pull and shutdown upload. Leave both credentials unset to go back to the volume/disk behaviour described above; the image keeps its existing startup path untouched in that case. For local Compose runs, copy `.env.example` to `.env` and fill in the two credentials — `.env` is git-ignored and excluded from the image build.

This is eventual consistency, not a mounted filesystem: up to `PIKPAK_SYNC_INTERVAL` seconds of the most recent writes can be lost if the instance is killed without a graceful shutdown, and a file deleted locally reappears after the next pull because uploads use `copy`, never `sync` (so a transient local failure cannot wipe the remote copy). SQLite state is copied while the gateway is running, so treat the store as a single-instance backup rather than a shared database; run one replica only.

### Health Checks

A healthy start logs `initializing …/config.json from koyeb-config.json`. The `WebSocket server listening on ws://0.0.0.0:8765/` message is INFO-level and the image sets `NANOBOT_LOG_LEVEL=WARNING`, so it is normally hidden — raise the log level to INFO or DEBUG to see it. The health check probes that socket from outside the container, so the `0.0.0.0` bind matters.

When PikPak persistence is enabled, a bounded startup pull (hard `PIKPAK_PULL_TIMEOUT`, default 20s) runs before the gateway binds; on hosted services a failed or timed-out pull degrades to a template-seeded start instead of blocking. The descriptor also uses a 300-second health check grace period and a restart limit of 5. A stalled pull that consumed the entire 60-second grace period left no time for the gateway to bind, so the probe failed and the instance was terminated before the port opened; the 20s ceiling fixes that even when the control panel keeps the default health check. Set the wider values in the control panel when configuring the service manually for extra headroom.

If Koyeb reports `TCP health check failed on port 8765` while the container logs look normal and show `WebUI ready` with a `http://127.0.0.1:8765` link, the image is running its interactive `webui` launcher instead of the gateway. That launcher binds the WebSocket channel to `127.0.0.1` for a local desktop session and probes itself over loopback, so `WebUI ready` says nothing about reachability from the edge. It also means the Koyeb config template was not applied — usually an image built before Koyeb support was added. Rebuild the image from the current Dockerfile and redeploy; Koyeb injects `KOYEB_PUBLIC_DOMAIN` into web services automatically, and `KOYEB=true` forces the same template when you supply that variable yourself. The WebUI is reached through the public domain, not the loopback link that `webui` prints.

Both `ANTHROPIC_API_KEY` and `NANOBOT_WEB_TOKEN` must be set before the first start. The seeded config keeps them as `${VAR}` references, so either one missing aborts startup with "missing environment variable reference(s)".

### Updates and Troubleshooting

Deployments from a public repository do not redeploy automatically, so update the service with `koyeb services redeploy nanobot/nanobot` after a new commit; the descriptor keeps that behavior explicit so an upstream commit never restarts a live agent. The deployment initializes `config.json` only when it is missing, and the volume keeps everything else, so a redeploy preserves your settings and history.

Read the service logs with `koyeb services logs nanobot/nanobot`. A missing model key fails provider requests after startup, while an incorrect web token leaves you on the authentication page. The instance type in the descriptor is `micro` (512 MB); use `small` or larger when you enable extra channels or keep long conversations.

## Docker

> [!TIP]
> The `-v ~/.nanobot:/home/nanobot/.nanobot` flag mounts your local config directory into the container, so your config and workspace persist across container restarts. The container runs as the non-root user `nanobot` (UID 1000) and reads config from `/home/nanobot/.nanobot`. Always mount your host config directory to `/home/nanobot/.nanobot`, not `/root/.nanobot`. If you get **Permission denied**, fix ownership on the host first: `sudo chown -R 1000:1000 ~/.nanobot`, or pass `--user $(id -u):$(id -g)` to match your host UID. Podman users can use `--userns=keep-id` instead.
>
> [!IMPORTANT]
> Official Docker usage currently means building from this repository with the included `Dockerfile`. Docker Hub images under third-party namespaces are not maintained or verified by HKUDS/nanobot; do not mount API keys or bot tokens into them unless you trust the publisher.

> [!IMPORTANT]
> The gateway and WebSocket channel default to `host: "127.0.0.1"` in `config.json` (set in `nanobot/config/schema.py`). Docker `-p` port forwarding cannot reach a container's loopback interface, so for the host or LAN to reach the exposed ports existing configurations and manual Docker runs must set both binds to `0.0.0.0` in `~/.nanobot/config.json` before starting the container. The Compose first-boot setup below sets these automatically. To serve the bundled WebUI from Docker, bind the WebSocket channel externally and protect bootstrap with `tokenIssueSecret`:
>
> ```json
> {
>   "gateway": { "host": "0.0.0.0" },
>   "channels": {
>     "websocket": {
>       "host": "0.0.0.0",
>       "port": 8765,
>       "tokenIssueSecret": "your-secret-here"
>     }
>   }
> }
> ```
>
> When the WebSocket `host` is `0.0.0.0`, the channel refuses to start unless `token`, `tokenIssueSecret`, or a fully configured `trustedProxyAuth` is also configured. See [`webui.md#lan-access`](./webui.md#lan-access) for details. The gateway health route itself is intentionally minimal and unauthenticated. When the container binds it to `0.0.0.0`, publish port `18790` to host loopback only; place any remotely monitored health endpoint behind a firewall or reverse proxy. If another host must probe it directly, replace `127.0.0.1` in the port mapping with a trusted host interface and restrict inbound traffic to the monitoring system.

### Docker Compose

On a new Linux host, install Git and Docker with the Compose plugin, then get the repository. You do not need to install Python or nanobot on the host:

```bash
git clone https://github.com/HKUDS/nanobot.git
cd nanobot
docker compose up -d
docker compose logs
```

On first boot, Compose initializes `~/.nanobot/config.json` with container bind addresses and random WebUI/API credentials. Open `http://HOST:8765`, enter the WebUI password printed in the first-boot logs, then configure your provider and model in **Settings → Models**. Send a message to verify the setup. No provider is required just to open the WebUI.

Export `NANOBOT_WEB_TOKEN` or `NANOBOT_API_KEY` before the first startup to supply your own credentials. Compose forwards these variables into the containers. Existing configuration is left untouched, including credentials and bind addresses; changing these variables later does not rotate persisted credentials. An explicit `--config` or `-c` selects a user-managed configuration and skips default-instance initialization. If you reuse an existing configuration, check the [Docker bind and authentication settings](#docker) above.

The configuration and workspace persist in the `~/.nanobot` bind mount. Keep `agents.defaults.workspace` as a container path, normally `~/.nanobot/workspace`, rather than a host home-directory path. Other config `${VAR}` references require explicit `environment` entries in the CLI and gateway services; Compose does not forward arbitrary host variables.

Compose starts both the gateway and the OpenAI-compatible API. Before a provider/model is configured, the API remains available for health checks and returns HTTP 503 with a `configuration_error` for model requests, including streaming requests. After configuring the model, restart the API to load those settings:

```bash
docker compose restart nanobot-api
```

Its endpoint is `http://127.0.0.1:8900/v1`; use the API key from the first-boot logs or `api.apiKey` in the persisted config. It is published to host loopback only.

CLI commands do not trigger Docker service initialization. For terminal setup, run `docker compose run --rm nanobot-cli onboard --wizard`, configure the model, and merge the [Docker bind and authentication settings](#docker) and set `api.apiKey` to a private credential before starting services. Plain `onboard` keeps its original first-run behavior without an extra overwrite prompt. Verify it with `docker compose run --rm nanobot-cli agent -m "Hello!"` before starting the persistent gateway.

The default image preinstalls WhatsApp dependencies. To bake other enabled channels into an image (recommended for deployments without PyPI access), pass a comma-separated `NANOBOT_CHANNELS` build argument:

```bash
NANOBOT_CHANNELS=telegram,slack docker compose build
```

The image keeps nanobot in a virtual environment owned by its built-in non-root runtime user (UID 1000). If an enabled channel was not preinstalled, gateway startup can therefore install its manifest-declared dependencies. Rebuilding with `NANOBOT_CHANNELS` keeps that installation reproducible instead of relying on the container's writable layer. If you override the container with a different `--user`, bake every enabled channel into the image because that UID is not guaranteed write access to the virtual environment.

```bash
curl --fail http://127.0.0.1:18790/health
```

```bash
docker compose run --rm nanobot-cli agent -m "Hello!"   # run CLI
docker compose logs -f nanobot-gateway                   # view logs
docker compose down                                      # stop
```

Open `http://HOST:8765` and enter the configured `tokenIssueSecret` when prompted. For a remote host, allow access only from the intended network or use an SSH tunnel (`ssh -L 8765:127.0.0.1:8765 user@HOST`) and open `http://127.0.0.1:8765` locally. The health check confirms the process is reachable; send a browser message to verify the model and WebUI together.

The default Compose file drops all Linux capabilities except `CHOWN`, `SETUID`, and `SETGID`, which the root entrypoint needs to fix bind-mount ownership and become UID 1000. It also enables `no-new-privileges`, so the non-root process cannot regain those bootstrap capabilities through setuid binaries or file capabilities. Docker's default AppArmor/seccomp profiles remain enabled. If you explicitly set `"tools.exec.sandbox": "bwrap"` in `~/.nanobot/config.json`, add the bwrap override file when starting containers:

```bash
docker compose -f docker-compose.yml -f docker-compose.bwrap.yml up -d nanobot-gateway
docker compose -f docker-compose.yml -f docker-compose.bwrap.yml run --rm nanobot-cli agent -m "Hello!"
```

The override adds `CAP_SYS_ADMIN` and disables AppArmor/seccomp confinement for the container so bubblewrap can create its nested namespaces. It preserves `no-new-privileges`. The host must also allow unprivileged user namespaces; the override cannot bypass a host-level namespace restriction. Use it only when the bwrap sandbox is enabled.

### Docker

```bash
# Build the image
docker build -t nanobot .

# Or preinstall a regular Python extra such as Bedrock support
docker build --build-arg NANOBOT_EXTRAS=bedrock -t nanobot .

# Or preinstall dependencies for a specific set of channels
docker build --build-arg NANOBOT_CHANNELS=telegram,slack -t nanobot .

# Initialize config (first time only)
docker run -v ~/.nanobot:/home/nanobot/.nanobot --rm nanobot onboard

# Merge model/provider and Docker bind/auth settings as described above.
vim ~/.nanobot/config.json

# Verify the configuration and model before running the persistent gateway.
docker run -v ~/.nanobot:/home/nanobot/.nanobot --rm nanobot status
docker run -v ~/.nanobot:/home/nanobot/.nanobot --rm nanobot agent -m "Hello!"

# Run gateway (connects to enabled channels, e.g. Telegram/Discord/Mochat).
# `-p 8765:8765` exposes the WebSocket channel / WebUI alongside the gateway
# health endpoint on 18790.
docker run \
  --cap-drop ALL \
  --cap-add CHOWN --cap-add SETGID --cap-add SETUID \
  --security-opt no-new-privileges:true \
  -v ~/.nanobot:/home/nanobot/.nanobot \
  -p 127.0.0.1:18790:18790 -p 8765:8765 \
  nanobot gateway

# If `tools.exec.sandbox: "bwrap"` is enabled, run with the extra permissions
# bubblewrap needs for nested namespaces. Without them, `bwrap` may exit with
# `clone3: Operation not permitted`.
docker run \
  --cap-drop ALL \
  --cap-add CHOWN --cap-add SETGID --cap-add SETUID --cap-add SYS_ADMIN \
  --security-opt no-new-privileges:true \
  --security-opt apparmor=unconfined \
  --security-opt seccomp=unconfined \
  -v ~/.nanobot:/home/nanobot/.nanobot \
  -p 127.0.0.1:18790:18790 -p 8765:8765 \
  nanobot gateway

# Or run a single command
docker run -v ~/.nanobot:/home/nanobot/.nanobot --rm nanobot agent -m "Hello!"
docker run -v ~/.nanobot:/home/nanobot/.nanobot --rm nanobot status
```

### Cloudflare Tunnel + Cloudflare Access

For a local `cloudflared` process in front of nanobot, Cloudflare Access can authenticate the user before forwarding the request and add `Cf-Access-Jwt-Assertion`. Opt in to trusted-proxy no-token mode only when the direct TCP peer is the tunnel process and the assertion is non-empty:

```json
{
  "gateway": { "host": "127.0.0.1" },
  "channels": {
    "websocket": {
      "host": "127.0.0.1",
      "port": 8765,
      "publicWsUrl": "wss://nanobot.example.com/",
      "trustedProxyAuth": {
        "trustedPeerCidrs": ["127.0.0.1/32", "::1/128"],
        "assertionHeader": "Cf-Access-Jwt-Assertion"
      }
    }
  }
}
```

This is two-part authorization: a trusted direct loopback peer **and** a non-empty Cloudflare Access assertion. A trusted CIDR alone is not a bypass. For this flow `/webui/bootstrap` returns connection metadata without a bootstrap token or REST API token; the proxy assertion authorizes the WebSocket handshake and REST requests directly.

Set `publicWsUrl` to the browser-facing `wss://` endpoint when the tunnel sends the origin host header (such as `127.0.0.1:8765`); otherwise the WebUI could attempt to open its WebSocket directly against the loopback address. The assertion header must be generated by Cloudflare Access after authentication; routing/client metadata headers such as `Host`, `Forwarded`, `X-Forwarded-*`, `X-Real-IP`, and `CF-Connecting-IP` are rejected as `assertionHeader` values. Nanobot trusts the assertion but does not cryptographically validate the JWT, so configure the tunnel and Access policy carefully and do not expose the nanobot listener directly to untrusted clients. Forwarded client headers do not establish proxy trust.

## Switch an Existing Gateway to a Service

Before installing an OS service, finish active work and close WebUI/TUI launchers that could start another gateway. Stop the existing instance: use `nanobot gateway stop` for a recorded background gateway (including one retained by `/detach`), or Ctrl+C in the terminal running a foreground gateway. Pass the same `--config` and `--workspace` selectors to stop, status, and service-install commands for a custom instance.

The service installer does not take over an existing standalone process. Starting both on the same ports makes the new service fail. If an OS service already owns the instance, manage it with its service manager instead of launching another process.

## Linux Service

Run the gateway as a systemd user service so it starts automatically and restarts on failure. Complete the [existing-gateway handoff](#switch-an-existing-gateway-to-a-service) first if an instance is already running.

Preview the generated unit first:

```bash
nanobot gateway install-service --manager systemd --dry-run
```

Install, enable, and start it:

```bash
nanobot gateway install-service --manager systemd
```

For a custom instance, pass the same config/workspace selector you use to run the gateway:

```bash
nanobot gateway install-service \
  --manager systemd \
  --name nanobot-telegram \
  --config ~/.nanobot-telegram/config.json \
  --workspace ~/.nanobot-telegram/workspace
```

Common operations:

```bash
systemctl --user status nanobot-gateway        # check status
systemctl --user restart nanobot-gateway       # restart after config changes
journalctl --user -u nanobot-gateway -f        # follow logs
nanobot gateway uninstall-service --manager systemd
```

The installer writes `~/.config/systemd/user/nanobot-gateway.service`, runs `systemctl --user daemon-reload`, enables the unit, and restarts it. It uses the current Python executable with `python -m nanobot gateway --foreground`, so the service runs in the same environment you used to install nanobot.

> **Note:** User services only run while you are logged in. To keep the gateway running after logout, enable lingering:
>
> ```bash
> loginctl enable-linger $USER
> ```

## macOS LaunchAgent

Use a LaunchAgent when you want `nanobot gateway` to stay online after you log in, without keeping a terminal open. Complete the [existing-gateway handoff](#switch-an-existing-gateway-to-a-service) first if an instance is already running.

Preview the generated plist first:

```bash
nanobot gateway install-service --manager launchd --dry-run
```

Install, load, enable, and start it:

```bash
nanobot gateway install-service --manager launchd
```

For a custom instance:

```bash
nanobot gateway install-service \
  --manager launchd \
  --name nanobot-telegram \
  --config ~/.nanobot-telegram/config.json \
  --workspace ~/.nanobot-telegram/workspace
```

Common operations:

```bash
launchctl list | grep ai.nanobot.gateway
launchctl kickstart -k gui/$(id -u)/ai.nanobot.gateway
nanobot gateway uninstall-service --manager launchd
```

The installer writes `~/Library/LaunchAgents/ai.nanobot.gateway.plist`, uses the current Python executable with `python -m nanobot gateway --foreground`, and writes LaunchAgent logs under `~/.nanobot/logs/`.

> **Note:** if startup fails with "address already in use", stop the manually started `nanobot gateway` process first.
