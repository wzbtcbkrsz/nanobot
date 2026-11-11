import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from nanobot.channels.websocket.runtime import WebSocketConfig
from nanobot.config.loader import load_config, resolve_config_env_vars

ROOT = Path(__file__).resolve().parents[1]


def _initialize(path: Path, web_token: str = "", api_key: str = "") -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, NANOBOT_WEB_TOKEN=web_token, NANOBOT_API_KEY=api_key)
    return subprocess.run(
        [sys.executable, "-m", "nanobot.cli.docker_bootstrap", str(path)],
        env=env, capture_output=True, text=True,
    )


def _entrypoint_run(tmp_path, *, env=None, existing=None, args=("gateway",)):
    """Run the container entrypoint with an isolated HOME and a stub nanobot."""
    home = tmp_path / "home"
    (home / ".nanobot").mkdir(parents=True)
    templates = tmp_path / "templates"
    templates.mkdir()
    for name in ("render-config.json", "koyeb-config.json"):
        shutil.copy(ROOT / name, templates / name)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    recorded = tmp_path / "argv.txt"
    stub = bindir / "nanobot"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{recorded}"\n', encoding="utf-8")
    stub.chmod(0o755)
    config = home / ".nanobot" / "config.json"
    if existing is not None:
        config.write_text(existing, encoding="utf-8")
    environment = {
        "HOME": str(home),
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "NANOBOT_CONFIG_TEMPLATE_DIR": str(templates),
    }
    environment.update(env or {})
    result = subprocess.run(
        ["sh", str(ROOT / "entrypoint.sh"), *args],
        env=environment, capture_output=True, text=True,
    )
    return result, config, recorded


def test_render_blueprint_starts_gateway_through_entrypoint():
    blueprint_path = ROOT / "render.yaml"
    blueprint = yaml.safe_load(blueprint_path.read_text(encoding="utf-8"))
    service = next(service for service in blueprint["services"] if service["name"] == "nanobot")
    assert shlex.split(service["dockerCommand"]) == ["/usr/local/bin/entrypoint.sh", "gateway"]


def test_koyeb_descriptor_serves_the_webui_on_one_public_port():
    descriptor = yaml.safe_load((ROOT / "koyeb-compose.yaml").read_text(encoding="utf-8"))
    definition = descriptor["services"][0]["definition"]
    assert definition["type"] == "WEB"
    docker = definition["git"]["docker"]
    assert docker["entrypoint"] == ["/usr/local/bin/entrypoint.sh"]
    assert docker["command"] == "gateway"
    port = definition["ports"][0]["port"]
    assert definition["routes"] == [{"port": port, "path": "/"}]
    assert definition["health_checks"][0]["tcp"] == {"port": port}
    # The PikPak pull happens before the gateway binds; a short grace period
    # fails the probe and restarts into another slow pull.
    assert definition["health_checks"][0]["grace_period"] == 300
    assert definition["health_checks"][0]["restart_limit"] == 5
    env = {entry["key"]: entry["value"] for entry in definition["env"]}
    assert set(env) == {
        "ANTHROPIC_API_KEY", "NANOBOT_WEB_TOKEN", "PIKPAK_USER", "PIKPAK_PASSWORD",
        "PIKPAK_SYNC_INTERVAL",
    }
    assert env["PIKPAK_SYNC_INTERVAL"] == "300"


def test_koyeb_config_template_binds_the_public_websocket_endpoint(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("NANOBOT_WEB_TOKEN", "web-token")
    monkeypatch.setenv("PORT", "8765")
    monkeypatch.setenv("KOYEB_PUBLIC_DOMAIN", "nanobot-org-hash.koyeb.app")
    config = resolve_config_env_vars(load_config(ROOT / "koyeb-config.json"))
    websocket = WebSocketConfig.model_validate(config.channels.model_dump()["websocket"])
    assert websocket.host == "0.0.0.0"
    assert websocket.port == 8765
    # Koyeb terminates TLS at the edge and does not forward x-forwarded-proto,
    # so the browser-facing scheme is pinned instead of derived from headers.
    assert websocket.public_ws_url == "wss://nanobot-org-hash.koyeb.app/"
    assert websocket.token_issue_secret == "web-token"
    assert websocket.websocket_requires_token
    assert config.gateway.host == "127.0.0.1"


@pytest.mark.skipif(sys.platform == "win32", reason="the container entrypoint is a POSIX shell script")
@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 0)() == 0,
    reason="a root start takes the privilege-drop path instead of the seeding path",
)
@pytest.mark.parametrize(
    ("env", "template"),
    [
        ({"KOYEB": "true"}, "koyeb-config.json"),
        ({"KOYEB_PUBLIC_DOMAIN": "nanobot-org-hash.koyeb.app"}, "koyeb-config.json"),
        ({"RENDER": "true"}, "render-config.json"),
    ],
)
def test_entrypoint_seeds_the_platform_config_template(tmp_path, env, template):
    result, config, recorded = _entrypoint_run(tmp_path, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_text(encoding="utf-8") == (ROOT / template).read_text(encoding="utf-8")
    assert recorded.read_text(encoding="utf-8").splitlines() == ["gateway", "--config", str(config)]


@pytest.mark.skipif(sys.platform == "win32", reason="the container entrypoint is a POSIX shell script")
@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 0)() == 0,
    reason="a root start takes the privilege-drop path instead of the seeding path",
)
def test_entrypoint_keeps_an_existing_platform_config(tmp_path):
    result, config, recorded = _entrypoint_run(tmp_path, env={"KOYEB": "true"}, existing='{"kept": true}')
    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_text(encoding="utf-8") == '{"kept": true}'
    assert "existing" in result.stdout
    assert recorded.read_text(encoding="utf-8").splitlines() == ["gateway", "--config", str(config)]


@pytest.mark.skipif(sys.platform == "win32", reason="the container entrypoint is a POSIX shell script")
@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 0)() == 0,
    reason="a root start takes the privilege-drop path instead of the seeding path",
)
def test_entrypoint_runs_foreground_gateway_for_default_platform_command(tmp_path):
    result, config, recorded = _entrypoint_run(
        tmp_path, env={"KOYEB": "true"}, args=("webui", "--yes", "--no-open"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert recorded.read_text(encoding="utf-8").splitlines() == ["gateway", "--config", str(config)]


@pytest.mark.skipif(sys.platform == "win32", reason="the container entrypoint is a POSIX shell script")
@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 0)() == 0,
    reason="a root start takes the privilege-drop path instead of the seeding path",
)
def test_entrypoint_keeps_an_explicit_platform_command(tmp_path):
    result, config, recorded = _entrypoint_run(
        tmp_path, env={"RENDER": "true"}, args=("gateway", "--verbose"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert recorded.read_text(encoding="utf-8").splitlines() == [
        "gateway", "--verbose", "--config", str(config),
    ]


def _pikpak_entrypoint_run(tmp_path, *, env=None, pull_fails=False):
    """Run the entrypoint with PikPak credentials and stubbed rclone/nanobot."""
    home = tmp_path / "home"
    (home / ".nanobot").mkdir(parents=True)
    templates = tmp_path / "templates"
    templates.mkdir()
    shutil.copy(ROOT / "koyeb-config.json", templates / "koyeb-config.json")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    recorded = tmp_path / "argv.txt"
    nanobot_stub = bindir / "nanobot"
    nanobot_stub.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{recorded}"\nprintf "%s\\n" "$HOME" >> "{recorded}"\n',
        encoding="utf-8",
    )
    nanobot_stub.chmod(0o755)
    calls = tmp_path / "rclone-calls.txt"
    rclone_stub = bindir / "rclone"
    copy_failure = 'case "$1" in copy) exit 1 ;; esac\n' if pull_fails else ""
    rclone_stub.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\n'
        'case "$1" in obscure) echo OBSCURED ;; esac\n'
        + copy_failure
        + 'exit 0\n',
        encoding="utf-8",
    )
    rclone_stub.chmod(0o755)

    pikpak_root = tmp_path / "pikpak-home"
    if pull_fails:
        # A partial pull can leave a stale config behind. The hosted path must
        # reseed the template rather than start the gateway with that config.
        stale = pikpak_root / ".nanobot" / "config.json"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text('{"stale": true}', encoding="utf-8")
    environment = {
        "HOME": str(home),
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "NANOBOT_CONFIG_TEMPLATE_DIR": str(templates),
        "NANOBOT_PIKPAK_SCRIPT": str(ROOT / "pikpak-storage.sh"),
        "PIKPAK_USER": "user@example.com",
        "PIKPAK_PASSWORD": "secret",
        "PIKPAK_LOCAL_ROOT": str(pikpak_root),
        "PIKPAK_CONF_DIR": str(tmp_path / "pikpak-conf"),
        "PIKPAK_SYNC_LOOP": "0",
    }
    environment.update(env or {})
    result = subprocess.run(
        ["sh", str(ROOT / "entrypoint.sh"), "gateway"],
        env=environment, capture_output=True, text=True,
    )
    return result, pikpak_root, home, recorded, calls


@pytest.mark.skipif(sys.platform == "win32", reason="the container entrypoint is a POSIX shell script")
@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 0)() == 0,
    reason="a root start takes the privilege-drop path instead of the seeding path",
)
def test_entrypoint_points_home_at_pikpak_storage(tmp_path):
    """With PikPak credentials set, state is pulled before startup, HOME moves
    onto the persistent directory, and the conventional data path is linked."""
    result, pikpak_root, home, recorded, calls = _pikpak_entrypoint_run(
        tmp_path, env={"KOYEB": "true"},
    )
    assert result.returncode == 0, result.stdout + result.stderr

    config = pikpak_root / ".nanobot" / "config.json"
    assert config.read_text(encoding="utf-8") == (ROOT / "koyeb-config.json").read_text(encoding="utf-8")
    assert recorded.read_text(encoding="utf-8").splitlines() == [
        "gateway", "--config", str(config), str(pikpak_root),
    ]
    assert (home / ".nanobot").is_symlink()
    calls_text = calls.read_text(encoding="utf-8")
    assert f"copy pikpak:nanobot/.nanobot {pikpak_root}/.nanobot --exclude /logs/**" in calls_text
    # The final upload after the gateway exits must use the same filter, or
    # logs would creep back into the persistent store on every shutdown.
    assert f"copy {pikpak_root}/.nanobot pikpak:nanobot/.nanobot --exclude /logs/**" in calls_text


@pytest.mark.skipif(sys.platform == "win32", reason="the container entrypoint is a POSIX shell script")
@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 0)() == 0,
    reason="a root start takes the privilege-drop path instead of the seeding path",
)
def test_hosted_entrypoint_starts_gateway_when_pikpak_pull_fails(tmp_path):
    """A slow or broken remote store must not keep the health-check port closed
    on hosted web services: the platform template is reseeded and the gateway
    starts in the foreground instead of aborting."""
    result, pikpak_root, _, recorded, _ = _pikpak_entrypoint_run(
        tmp_path, env={"KOYEB": "true"}, pull_fails=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    config = pikpak_root / ".nanobot" / "config.json"
    assert config.read_text(encoding="utf-8") == (ROOT / "koyeb-config.json").read_text(encoding="utf-8")
    assert recorded.read_text(encoding="utf-8").splitlines() == [
        "gateway", "--config", str(config), str(pikpak_root),
    ]
    assert "timed out" in result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="the container entrypoint is a POSIX shell script")
@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 0)() == 0,
    reason="a root start takes the privilege-drop path instead of the seeding path",
)
def test_local_bootstrap_stays_fail_closed_when_pikpak_pull_fails(tmp_path):
    """Outside hosted platforms the startup pull stays a hard prerequisite."""
    result, _, _, recorded, _ = _pikpak_entrypoint_run(tmp_path, pull_fails=True)
    assert result.returncode != 0
    assert not recorded.exists()
    assert "refusing to start" in result.stderr


def test_first_boot_generates_valid_container_config(tmp_path):
    path = tmp_path / "instance" / "config.json"
    result = _initialize(path)
    assert result.returncode == 0, result.stderr
    config = resolve_config_env_vars(load_config(path))
    ws = WebSocketConfig.model_validate(config.channels.model_dump()["websocket"])
    assert config.gateway.host == ws.host == config.api.host == "0.0.0.0"
    assert ws.enabled and ws.websocket_requires_token
    assert len(ws.token_issue_secret) == len(config.api.api_key) == 32
    assert ws.token_issue_secret != config.api.api_key
    assert ws.token_issue_secret in result.stdout
    assert config.api.api_key in result.stdout


@pytest.mark.parametrize("secret", ['review&secret', 'review|secret', 'review"secret', r'review\secret'])
def test_custom_secrets_survive_json_serialization(tmp_path, secret):
    path = tmp_path / "config.json"
    result = _initialize(path, secret, secret)
    assert result.returncode == 0, result.stderr
    config = resolve_config_env_vars(load_config(path))
    ws = WebSocketConfig.model_validate(config.channels.model_dump()["websocket"])
    assert ws.token_issue_secret == config.api.api_key == secret


def test_restart_preserves_user_settings_and_credentials(tmp_path):
    path = tmp_path / "config.json"
    assert _initialize(path, "original-web", "original-api").returncode == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    data["providers"]["custom"] = {"apiKey": "provider-key", "apiBase": "http://model:8000/v1"}
    path.write_text(json.dumps(data), encoding="utf-8")
    previous = path.read_bytes()
    result = _initialize(path, "replacement-web", "replacement-api")
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == previous
    assert "replacement-web" not in result.stdout
    assert "replacement-api" not in result.stdout


def test_failed_write_does_not_leave_an_existing_config(tmp_path):
    from unittest.mock import patch

    from nanobot.cli.docker_bootstrap import initialize_config

    path = tmp_path / "config.json"
    with patch.object(Path, "replace", side_effect=PermissionError("read-only mount")):
        with pytest.raises(PermissionError, match="read-only mount"):
            initialize_config(path)
    assert not path.exists()
    assert not list(tmp_path.glob("*.tmp"))
