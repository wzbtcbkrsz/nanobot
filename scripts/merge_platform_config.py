"""Reapply platform-owned settings to a PikPak-restored config.json.

Hosted deployments (Render/Koyeb) seed config.json from a platform template
whose bind, port, and token values are ${VAR} references resolved at startup.
When the deferred PikPak restore brings back an older config.json, those values
can point at 127.0.0.1, a stale port, or a stale token, which makes the gateway
unreachable from the edge after the post-restore restart. Keep everything the
user changed (model, providers, tools) but force the platform-owned fields back
to their template values.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

# Values owned by the deployment platform rather than by the persisted instance.
PLATFORM_PATHS: tuple[tuple[str, ...], ...] = (
    ("gateway", "host"),
    ("channels", "websocket", "enabled"),
    ("channels", "websocket", "host"),
    ("channels", "websocket", "port"),
    ("channels", "websocket", "publicWsUrl"),
    ("channels", "websocket", "tokenIssueSecret"),
    ("channels", "websocket", "websocketRequiresToken"),
    ("tools", "restrictToWorkspace"),
    ("tools", "webuiAllowRemotePackageInstall"),
    ("tools", "my", "allowSet"),
)


def _overlay(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Return ``extra`` with keys missing from it filled in from ``base``."""
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _overlay(merged[key], value)
        else:
            merged[key] = value
    return merged


def _get(node: dict[str, Any], path: tuple[str, ...]) -> Any:
    for key in path:
        node = node[key]
    return node


def _set(node: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def merge(template: dict[str, Any], restored: dict[str, Any]) -> dict[str, Any]:
    """Keep restored settings, fill missing template keys, enforce platform fields."""
    merged = _overlay(template, restored)
    for path in PLATFORM_PATHS:
        try:
            _set(merged, path, _get(template, path))
        except (KeyError, TypeError, AttributeError):
            # A template that does not declare the key cannot enforce it.
            continue
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", type=Path, help="Platform config template")
    parser.add_argument("config", type=Path, help="Restored config.json to rewrite in place")
    args = parser.parse_args()

    template = json.loads(args.template.read_text(encoding="utf-8"))
    restored = json.loads(args.config.read_text(encoding="utf-8"))
    merged = merge(template, restored)

    tmp = args.config.with_name(args.config.name + ".tmp")
    tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
