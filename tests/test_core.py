"""
Shadow Core: one memory, tracking every agent, handoff, connect, deprecations.
Run with: python -m pytest tests/test_core.py
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shadow_link.aca import open_aca  # noqa: E402
from shadow_link.aca.store import CognitiveStore  # noqa: E402
from shadow_link.database import Database  # noqa: E402
from shadow_link.ingester import classify_multi  # noqa: E402
from shadow_link import tracker as T  # noqa: E402


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """A fake home with one session from each agent, all in one git repo."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    now = "2026-10-16T12:00:00Z"
    _jsonl(tmp_path / ".claude/projects/repo/claude-sess.jsonl", [
        {"type": "user", "sessionId": "claude-sess", "cwd": str(repo), "timestamp": now,
         "message": {"role": "user", "content": "fix the login bug, my key is sk-ant-abcdefghijklmnop1234"}},
        {"type": "assistant", "sessionId": "claude-sess", "cwd": str(repo), "timestamp": now,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Fixing it."},
             {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": str(repo / "auth.py")}},
             {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "python -m pytest -q"}}]}},
        {"type": "user", "sessionId": "claude-sess", "cwd": str(repo), "timestamp": now,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
             {"type": "tool_result", "tool_use_id": "t2", "content": "1 failed", "is_error": True}]}},
        {"type": "assistant", "sessionId": "claude-sess", "cwd": str(repo), "timestamp": now,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "One test still fails. Want me to update the fixture?"}]}},
    ])
    patch = ("text(await tools.apply_patch(\"*** Begin Patch\\n*** Update File: " +
             str(repo / "api.py").replace("\\", "\\\\") + "\\n@@\\n-a\\n+b\\n*** End Patch\"))")
    _jsonl(tmp_path / ".codex/sessions/2026/10/16/rollout-2026-10-16-codexsess.jsonl", [
        {"type": "session_meta", "timestamp": now, "payload": {"id": "codex-sess", "cwd": str(repo)}},
        {"type": "response_item", "timestamp": now, "payload": {
            "type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "<environment_context>cwd</environment_context>"},
                {"type": "input_text", "text": "add rate limiting to the api"}]}},
        {"type": "response_item", "timestamp": now, "payload": {
            "type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": patch}},
        {"type": "response_item", "timestamp": now, "payload": {
            "type": "custom_tool_call_output", "call_id": "c1", "output": [{"type": "input_text", "text": "Done"}]}},
        {"type": "response_item", "timestamp": now, "payload": {
            "type": "custom_tool_call", "call_id": "c2", "name": "exec",
            "input": "text(await tools.exec_command({cmd:\"npm test\"}))"}},
        {"type": "response_item", "timestamp": now, "payload": {
            "type": "custom_tool_call_output", "call_id": "c2",
            "output": [{"type": "input_text", "text": "{\"exit_code\":1,\"output\":\"2 failing\"}"}]}},
        {"type": "response_item", "timestamp": now, "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Rate limiting is in; two tests fail."}]}},
    ])
    _jsonl(tmp_path / ".gemini/antigravity-cli/brain/agy-sess/.system_generated/logs/transcript.jsonl", [
        {"type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": now,
         "content": "<USER_REQUEST>\nreview the retries\n</USER_REQUEST>"},
        {"type": "PLANNER_RESPONSE", "source": "MODEL", "created_at": now, "tool_calls": [
            {"name": "run_command", "args": {"CommandLine": "\"git status\"", "Cwd": json.dumps(str(repo))}}]},
        {"type": "GENERIC", "source": "MODEL", "created_at": now, "content": "The command exited with code 0."},
        {"type": "PLANNER_RESPONSE", "source": "MODEL", "created_at": now, "content": "Retries look fine."},
    ])
    return tmp_path


def _tracker(home: Path, tmp_path: Path) -> T.SessionTracker:
    return T.SessionTracker(tmp_path / "core.db", home=home, backfill_seconds=10 ** 9)


# ------------------------------------------------------------------ phase 1
def test_categorizer_whole_words_and_requests():
    assert classify_multi("remove the badge from the header") == ["request"]
    assert classify_multi("that's wrong, revert it")[0] == "rejection"
    assert "rejection" in classify_multi("do not use docker")
    assert classify_multi("switch to postgres instead") == ["pivot"]


def test_add_prompt_unique_is_atomic(tmp_path: Path):
    db = Database(tmp_path / "u.db")
    meta = {"agent_output": "I'll add docker"}
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        db.add_prompt_unique("no docker please", "rejection", metadata=dict(meta)))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for _, created in results if created) == 1
    assert len(db.all_prompts()) == 1


def test_new_node_types_are_known(tmp_path: Path):
    store = CognitiveStore(tmp_path / "n.db")
    for t in ("agent_session", "activity", "open_thread", "handoff", "project", "digest"):
        store.add_node("x", type=t)


# ------------------------------------------------------------------ phase 2
def test_tracker_reads_every_agent(home: Path, tmp_path: Path):
    tr = _tracker(home, tmp_path)
    changed = tr.poll_once()
    assert len(changed) == 3 and not tr.errors
    by_agent = {s["agent"]: s for s in tr.sessions.list()}
    assert set(by_agent) == {"claude-code", "codex", "antigravity"}
    assert {s["project_name"] for s in by_agent.values()} == {"repo"}

    claude = tr.sessions.load(by_agent["claude-code"]["id"])
    assert "sk-ant" not in json.dumps(claude) and "[redacted]" in claude["goal"]
    assert any(f.endswith("auth.py") for f in claude["files"])
    assert claude["tests"][-1]["status"] == "error"
    assert claude["open_thread"] == "Want me to update the fixture?"

    codex = tr.sessions.load(by_agent["codex"]["id"])
    assert codex["goal"] == "add rate limiting to the api"         # injected context dropped
    assert any(f.endswith("api.py") for f in codex["files"])
    assert codex["commands"][-1] == {**codex["commands"][-1], "cmd": "npm test", "status": "error"}

    agy = tr.sessions.load(by_agent["antigravity"]["id"])
    assert agy["goal"] == "review the retries"
    assert agy["commands"][-1]["status"] == "ok"


def test_tracker_is_incremental_and_resumes(home: Path, tmp_path: Path):
    tr = _tracker(home, tmp_path)
    tr.poll_once()
    assert tr.poll_once() == []                                     # nothing new
    log = home / ".codex/sessions/2026/10/16/rollout-2026-10-16-codexsess.jsonl"
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "response_item", "timestamp": "2026-10-16T12:05:00Z", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "also log the limits"}]}}) + "\n")
    again = _tracker(home, tmp_path)                               # a restarted daemon
    changed = again.poll_once()
    assert len(changed) == 1
    rec = again.sessions.load(changed[0])
    assert rec["requests"][-1] == "also log the limits" and rec["requests"].count("add rate limiting to the api") == 1


def test_sessions_link_into_aca_memory(home: Path, tmp_path: Path):
    tr = _tracker(home, tmp_path)
    tr.poll_once()
    aca = open_aca(tmp_path / "core.db")
    ctx = aca.process("what did codex change for rate limiting", kind="prompt", remember=False)["context"]
    assert any(i["type"] in ("agent_session", "activity") for i in ctx)


def test_quiet_sessions_get_a_template_digest(home: Path, tmp_path: Path, monkeypatch):
    tr = _tracker(home, tmp_path)
    tr.poll_once()
    monkeypatch.setattr(T, "IDLE_DIGEST_SECONDS", 0)
    tr._write_digests(time.time())
    s = {x["agent"]: x for x in tr.sessions.list()}["claude-code"]
    assert s["digest_by"] == "template" and "auth.py" in s["digest"] and s["needs_digest"]
    tr.set_digest(s["id"], "Fixed login; one test still fails.", "haiku")
    s = {x["agent"]: x for x in tr.sessions.list()}["claude-code"]
    assert s["digest"] == "Fixed login; one test still fails." and not s["needs_digest"]


def test_report_tracks_any_agent(tmp_path: Path):
    tr = T.SessionTracker(tmp_path / "r.db", home=tmp_path)
    sid = tr.report("cursor", "set up lint", project=str(tmp_path), files=[str(tmp_path / ".eslintrc")],
                    command="npm run lint", status="ok")
    rec = tr.sessions.load(sid)
    assert rec["agent"] == "cursor" and rec["goal"] == "set up lint" and rec["commands"][-1]["status"] == "ok"


# ------------------------------------------------------------------ phase 4
def test_handoff_brief(home: Path, tmp_path: Path):
    tr = _tracker(home, tmp_path)
    tr.poll_once()
    codex = [s for s in tr.sessions.list() if s["agent"] == "codex"][0]
    packet = T.build_handoff(tr.store, codex["id"], "claude-code")
    md = packet["markdown"]
    assert md.startswith("# Handoff from Codex: repo")
    assert "add rate limiting to the api" in md and "api.py" in md and "`npm test`  (failed)" in md
    assert len(md) < 2400
    node = tr.store.get_node(packet["id"])
    assert node["type"] == "handoff" and node["metadata"]["from"] == codex["id"]


def test_connect_writes_configs_once(tmp_path: Path):
    from shadow_link.connect import connect
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/config.toml").write_text('model = "x"\n', encoding="utf-8")
    (tmp_path / ".gemini/config").mkdir(parents=True)
    out = connect(["codex", "antigravity"], home=tmp_path)
    assert all("connected" in line for line in out)
    toml = (tmp_path / ".codex/config.toml").read_text(encoding="utf-8")
    assert toml.startswith('model = "x"') and "[mcp_servers.shadow-link]" in toml
    assert list((tmp_path / ".codex").glob("config.toml.bak-shadow-*"))
    cfg = json.loads((tmp_path / ".gemini/config/mcp_config.json").read_text(encoding="utf-8"))
    assert cfg["mcpServers"]["shadow-link"]["args"] == ["-m", "shadow_link.cli", "mcp"]
    again = connect(["codex", "antigravity"], home=tmp_path)
    assert all("already connected" in line for line in again)


def test_session_endpoints(home: Path, tmp_path: Path, monkeypatch):
    db = tmp_path / "core.db"
    monkeypatch.setenv("DB_PATH", str(db))
    tr = _tracker(home, tmp_path)
    tr.poll_once()
    from shadow_link.api import ShadowLinkAPIHandler
    server = ThreadingHTTPServer(("localhost", 0), ShadowLinkAPIHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://localhost:{server.server_address[1]}"

    def get(path):
        return json.loads(urlopen(base + path, timeout=10).read())

    def post(path, body):
        req = Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        return json.loads(urlopen(req, timeout=10).read())

    try:
        sessions = get("/sessions")["sessions"]
        assert {s["agent"] for s in sessions} == {"claude-code", "codex", "antigravity"}
        assert len(get("/sessions?exclude=claude-sess")["sessions"]) == 2
        codex = [s for s in sessions if s["agent"] == "codex"][0]
        assert get(f"/sessions/{codex['id']}")["record"]["goal"] == "add rate limiting to the api"
        assert get(f"/threads?project={home / 'repo'}")["threads"][0]["text"] == "Want me to update the fixture?"
        assert post("/handoff", {"session": codex["id"]})["markdown"].startswith("# Handoff from Codex")
        assert post(f"/sessions/{codex['id']}/digest", {"text": "Rate limiting done.", "by": "haiku"})["session"]["digest"] == "Rate limiting done."
        assert post("/track/event", {"agent": "aider", "what": "renamed utils"})["session"].startswith("S-aider-")
    finally:
        server.shutdown()


# ------------------------------------------------------------------ phase 5
@pytest.mark.parametrize("cmd", ["ui", "proxy", "run", "watch", "ingest-md"])
def test_deprecated_commands_warn(cmd):
    from shadow_link import cli
    assert cmd in cli.DEPRECATED
    out = subprocess.run([sys.executable, "-m", "shadow_link.cli", cmd, "--help"], cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0                     # still works in this release
    help_text = subprocess.run([sys.executable, "-m", "shadow_link.cli", "--help"], cwd=ROOT,
                               capture_output=True, text=True).stdout
    assert "(deprecated)" in help_text
