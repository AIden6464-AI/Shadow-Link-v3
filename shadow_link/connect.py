"""
shadow_link/connect.py

Register Shadow Link's MCP server with other agent CLIs, so they can list what
every agent is doing (`shadow_sessions`), ask its memory (`shadow_recall`) and
pick up another agent's work on request (`shadow_handoff`).

Each config file is backed up before it is changed, and a config that already
names Shadow Link is left alone.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

SERVER_NAME = "shadow-link"


def _server_command() -> tuple[str, list[str]]:
    # The interpreter that has shadow_link installed; works from any folder.
    return sys.executable, ["-m", "shadow_link.cli", "mcp"]


def _backup(path: Path) -> Path | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    dest = path.with_name(f"{path.name}.bak-shadow-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, dest)
    return dest


def _toml_literal(s: str) -> str:
    # Single-quoted TOML strings keep Windows backslashes as they are.
    if "'" in s:
        return json.dumps(s)
    return f"'{s}'"


def connect_codex(home: Path, dry_run: bool = False) -> str:
    cfg = home / ".codex" / "config.toml"
    if not cfg.parent.exists():
        return "Codex CLI not found (no ~/.codex); skipped."
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    if f"[mcp_servers.{SERVER_NAME}]" in text:
        return f"Codex: already connected ({cfg})."
    cmd, args = _server_command()
    block = (
        f"\n[mcp_servers.{SERVER_NAME}]\n"
        f"command = {_toml_literal(cmd)}\n"
        f"args = [{', '.join(_toml_literal(a) for a in args)}]\n"
        "startup_timeout_sec = 30\n"
    )
    if dry_run:
        return f"Codex: would add to {cfg}:{block}"
    backup = _backup(cfg)
    with open(cfg, "a", encoding="utf-8") as f:
        f.write(("\n" if text and not text.endswith("\n") else "") + block)
    return f"Codex: connected ({cfg}){f'; backup at {backup}' if backup else ''}."


def connect_antigravity(home: Path, dry_run: bool = False) -> str:
    cfg = home / ".gemini" / "config" / "mcp_config.json"
    if not (home / ".gemini").exists():
        return "Antigravity not found (no ~/.gemini); skipped."
    data: dict = {}
    if cfg.exists() and cfg.stat().st_size:
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except ValueError:
            return f"Antigravity: {cfg} is not valid JSON; left it alone. Add the server by hand."
    servers = data.setdefault("mcpServers", {})
    if SERVER_NAME in servers:
        return f"Antigravity: already connected ({cfg})."
    cmd, args = _server_command()
    servers[SERVER_NAME] = {"command": cmd, "args": args}
    if dry_run:
        return f"Antigravity: would write {cfg}:\n{json.dumps(data, indent=2)}"
    backup = _backup(cfg)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"Antigravity: connected ({cfg}){f'; backup at {backup}' if backup else ''}."


def connect(targets: list[str], home: Path | None = None, dry_run: bool = False) -> list[str]:
    home = home or Path.home()
    out = []
    if "codex" in targets:
        out.append(connect_codex(home, dry_run))
    if "antigravity" in targets:
        out.append(connect_antigravity(home, dry_run))
    return out
