import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "merge_platform_config.py"


def _run(tmp_path, template, restored):
    template_path = tmp_path / "template.json"
    config_path = tmp_path / "config.json"
    template_path.write_text(json.dumps(template), encoding="utf-8")
    config_path.write_text(json.dumps(restored), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(template_path), str(config_path)],
        capture_output=True, text=True,
    )
    return result, config_path


def _template():
    return {
        "agents": {"defaults": {"model": "anthropic/claude-opus-4-8"}},
        "gateway": {"host": "127.0.0.1", "port": 18790},
        "channels": {"websocket": {
            "enabled": True,
            "host": "0.0.0.0",
            "port": "${PORT}",
            "publicWsUrl": "wss://${KOYEB_PUBLIC_DOMAIN}/",
            "tokenIssueSecret": "${NANOBOT_WEB_TOKEN}",
            "websocketRequiresToken": True,
        }},
        "tools": {
            "restrictToWorkspace": True,
            "webuiAllowRemotePackageInstall": False,
            "my": {"allowSet": False},
        },
    }


def test_keeps_restored_settings_but_enforces_platform_fields(tmp_path):
    restored = {
        "agents": {"defaults": {"model": "custom/model"}},
        "providers": {"anthropic": {"apiKey": "sk-restored"}},
        "gateway": {"host": "0.0.0.0"},
        "channels": {"websocket": {
            "enabled": False,
            "host": "127.0.0.1",
            "port": 9999,
            "tokenIssueSecret": "stale-token",
        }},
        "tools": {"restrictToWorkspace": False},
    }
    result, config_path = _run(tmp_path, _template(), restored)
    assert result.returncode == 0, result.stderr

    merged = json.loads(config_path.read_text(encoding="utf-8"))
    assert merged["agents"]["defaults"]["model"] == "custom/model"
    assert merged["providers"]["anthropic"]["apiKey"] == "sk-restored"

    websocket = merged["channels"]["websocket"]
    assert websocket["enabled"] is True
    assert websocket["host"] == "0.0.0.0"
    assert websocket["port"] == "${PORT}"
    assert websocket["publicWsUrl"] == "wss://${KOYEB_PUBLIC_DOMAIN}/"
    assert websocket["tokenIssueSecret"] == "${NANOBOT_WEB_TOKEN}"
    assert websocket["websocketRequiresToken"] is True
    assert merged["gateway"]["host"] == "127.0.0.1"
    assert merged["tools"]["restrictToWorkspace"] is True
    assert merged["tools"]["webuiAllowRemotePackageInstall"] is False
    assert merged["tools"]["my"]["allowSet"] is False
    assert not config_path.with_name(config_path.name + ".tmp").exists()


def test_template_without_a_platform_key_leaves_the_restored_value(tmp_path):
    template = _template()
    del template["tools"]["restrictToWorkspace"]
    restored = {"tools": {"restrictToWorkspace": False}}
    result, config_path = _run(tmp_path, template, restored)
    assert result.returncode == 0, result.stderr
    assert json.loads(config_path.read_text(encoding="utf-8"))["tools"]["restrictToWorkspace"] is False
