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


def _pikpak_entrypoint_run(
    tmp_path, *, env=None, pull_fails=False, app_stays_alive=False, restore_config=False,
):
    """Run the entrypoint with PikPak credentials and stubbed rclone/nanobot.

    With app_stays_alive, the first gateway keeps running until terminated so
    the deferred-restore restart path can be observed; the second start exits.
    With restore_config, the stubbed pull writes an old config.json as if it
    had come back from the remote.
    """
    home = tmp_path / "home"
    (home / ".nanobot").mkdir(parents=True)
    templates = tmp_path / "templates"
    templates.mkdir()
    shutil.copy(ROOT / "koyeb-config.json", templates / "koyeb-config.json")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    recorded = tmp_path / "argv.txt"
    starts = tmp_path / "starts.txt"
    nanobot_stub = bindir / "nanobot"
    first_start = (
        'if [ "$count" -eq 1 ]; then\n'
        '    trap "exit 0" TERM\n'
        '    while :; do sleep 1; done\n'
        'fi\n'
    ) if app_stays_alive else ""
    nanobot_stub.write_text(
        f'#!/bin/sh\nprintf "start\\n" >> "{starts}"\n'
        f'count=$(wc -l < "{starts}")\n'
        f'printf "%s\\n" "$@" > "{recorded}"\n'
        f'printf "%s\\n" "$HOME" >> "{recorded}"\n'
        + first_start
        + 'exit 0\n',
        encoding="utf-8",
    )
    nanobot_stub.chmod(0o755)
    calls = tmp_path / "rclone-calls.txt"
    rclone_stub = bindir / "rclone"
    copy_failure = 'case "$1" in copy) exit 1 ;; esac\n' if pull_fails else ""
    restored = json.dumps({
        "agents": {"defaults": {"model": "custom/user-model", "provider": "auto"}},
        "providers": {"anthropic": {"apiKey": "sk-restored"}},
        "gateway": {"host": "0.0.0.0", "port": 18790},
        "channels": {"websocket": {
            "enabled": False, "host": "127.0.0.1", "port": 9999,
            "tokenIssueSecret": "stale-token",
        }},
        "tools": {"restrictToWorkspace": False},
    })
    restore_block = (
        'case "$1" in copy)\n'
        '    case "$2" in pikpak:*)\n'
        f"        printf '%s\\n' '{restored}' > \"$3/config.json\"\n"
        '    ;; esac\n'
        ';; esac\n'
    ) if restore_config else ""
    rclone_stub.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\n'
        'case "$1" in obscure) echo OBSCURED ;; esac\n'
        + restore_block
        + copy_failure
        + 'exit 0\n',
        encoding="utf-8",
    )
    rclone_stub.chmod(0o755)

    pikpak_root = tmp_path / "pikpak-home"
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
        "NANOBOT_MERGE_SCRIPT": str(ROOT / "scripts" / "merge_platform_config.py"),
    }
    environment.update(env or {})
    result = subprocess.run(
        ["sh", str(ROOT / "entrypoint.sh"), "gateway"],
        env=environment, capture_output=True, text=True, timeout=60,
    )
    return result, pikpak_root, home, recorded, calls


@pytest.mark.skipif(sys.platform == "win32", reason="the container entrypoint is a POSIX shell script")
@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 0)() == 0,
    reason="a root start takes the privilege-drop path instead of the seeding path",
)
def test_hosted_entrypoint_restores_config_and_restarts(tmp_path):
    """Hosted services restore config.json too, but reapply the platform-owned
    fields before the post-restore restart so an old loopback bind is harmless."""
    result, pikpak_root, home, recorded, calls = _pikpak_entrypoint_run(
        tmp_path, env={"KOYEB": "true"}, app_stays_alive=True, restore_config=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    config_path = pikpak_root / ".nanobot" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    template = json.loads((ROOT / "koyeb-config.json").read_text(encoding="utf-8"))
    # User settings survive the restore.
    assert config["agents"]["defaults"]["model"] == "custom/user-model"
    assert config["providers"]["anthropic"]["apiKey"] == "sk-restored"
    # Platform-owned fields are forced back to their template values.
    websocket = config["channels"]["websocket"]
    for key in ("enabled", "host", "port", "publicWsUrl", "tokenIssueSecret", "websocketRequiresToken"):
        assert websocket[key] == template["channels"]["websocket"][key], key
    assert config["gateway"]["host"] == template["gateway"]["host"]
    assert config["tools"]["restrictToWorkspace"] is True
    assert config["tools"]["webuiAllowRemotePackageInstall"] is False
    assert not config_path.with_name(config_path.name + ".tmp").exists()

    assert recorded.read_text(encoding="utf-8").splitlines() == [
        "gateway", "--config", str(config_path), str(pikpak_root),
    ]
    assert (home / ".nanobot").is_symlink()
    assert len((tmp_path / "starts.txt").read_text(encoding="utf-8").splitlines()) == 2
    calls_text = calls.read_text(encoding="utf-8")
    assert f"copy pikpak:nanobot/.nanobot {pikpak_root}/.nanobot --exclude /logs/**" in calls_text
    # The final upload after the gateway exits must still exclude logs.
    assert f"copy {pikpak_root}/.nanobot pikpak:nanobot/.nanobot --exclude /logs/**" in calls_text


@pytest.mark.skipif(sys.platform == "win32", reason="the container entrypoint is a POSIX shell script")
@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 0)() == 0,
    reason="a root start takes the privilege-drop path instead of the seeding path",
)
def test_hosted_entrypoint_starts_when_pikpak_is_down(tmp_path):
    """A broken remote store cannot block the first gateway start on hosted
    services; startup performs no PikPak network operation at all."""
    result, pikpak_root, _, recorded, calls = _pikpak_entrypoint_run(
        tmp_path, env={"KOYEB": "true"}, pull_fails=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    config = pikpak_root / ".nanobot" / "config.json"
    assert json.loads(config.read_text(encoding="utf-8")) == json.loads(
        (ROOT / "koyeb-config.json").read_text(encoding="utf-8")
    )
    assert recorded.read_text(encoding="utf-8").splitlines() == [
        "gateway", "--config", str(config), str(pikpak_root),
    ]
    assert "refusing to start" not in result.stderr
    assert calls.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="the container entrypoint is a POSIX shell script")
@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 0)() == 0,
    reason="a root start takes the privilege-drop path instead of the seeding path",
)
def test_local_bootstrap_pulls_state_before_start(tmp_path):
    """Local Compose keeps the original behaviour: state is pulled up front."""
    result, _, _, _, calls = _pikpak_entrypoint_run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    calls_text = calls.read_text(encoding="utf-8")
    assert "copy pikpak:nanobot/.nanobot" in calls_text
    assert "--exclude /logs/**" in calls_text


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
