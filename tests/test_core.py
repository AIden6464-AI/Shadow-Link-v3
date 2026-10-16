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


def test_pasted_output_is_not_your_voice():
    from shadow_link.live import is_real_reply, clean_reply
    assert not is_real_reply("PS D:\\app> npm start\n> app@1.0 start\nError: boom")
    assert not is_real_reply("reid@box:~$ lms ps\nIDENTIFIER MODEL STATUS")
    assert not is_real_reply("✅ ready\n⏳ loading\n🔊 [x] queued\n[2026] done")
    kept = "can you fix this crash\n\nTraceback (most recent call last):\n  File x.py, line 1\n    boom()\nNameError: boom"
    assert is_real_reply(kept) and clean_reply(kept) == "can you fix this crash"
    assert clean_reply("like the sidebar in opencode [Image #2]") == "like the sidebar in opencode"
    assert is_real_reply("ok build it with the venv")
    assert not is_real_reply('Starting Container\n run(\n File "/app/x.py", line 3, in main\n    boom()\nValueError: no')
    assert not is_real_reply("Windows PowerShell\nCopyright (C) Microsoft Corporation. All rights reserved.\n\nInstall the latest")
    assert is_real_reply("set x = 5 in the config please") and is_real_reply("add a return button to the page")


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


def test_decisions_are_remembered_and_linked(home: Path, tmp_path: Path):
    log = home / ".claude/projects/repo/claude-sess.jsonl"
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "sessionId": "claude-sess", "cwd": str(home / "repo"),
                            "timestamp": "2026-10-16T12:10:00Z",
                            "message": {"role": "user", "content": "let's go with postgres, don't use sqlite"}}) + "\n")
    tr = _tracker(home, tmp_path)
    tr.poll_once()
    claude = [s for s in tr.sessions.list() if s["agent"] == "claude-code"][0]
    rec = tr.sessions.load(claude["id"])
    assert [d["text"] for d in rec["decisions"]] == ["let's go with postgres, don't use sqlite"]
    decisions = tr.sessions._linked(claude["id"], "decision")
    assert len(decisions) == 1 and decisions[0]["content"].startswith("let's go with postgres")
    assert "## Decisions the user made" in T.build_handoff(tr.store, claude["id"])["markdown"]
    assert not T.is_decision("can you check the logs?") and T.is_decision("switch to pnpm instead")
    assert not T.is_decision("i don't really like how it looks")            # an opinion, not a decision
    q = "Should I merge it now, or wait?"
    assert T.decision_text("yes do it", q) == f"yes do it (answering: {q})"
    assert T.decision_text("do option 2", q).startswith("do option 2 (answering:")
    assert T.decision_text("ok installed", q) == "" and T.decision_text("sure", "") == ""


def test_handoff_links_the_session_that_picks_it_up(home: Path, tmp_path: Path):
    tr = _tracker(home, tmp_path)
    tr.poll_once()
    codex = [s for s in tr.sessions.list() if s["agent"] == "codex"][0]
    packet = T.build_handoff(tr.store, codex["id"], "claude-code")
    assert f"Shadow handoff {packet['id']}" in packet["markdown"]
    _jsonl(home / ".claude/projects/repo/new-sess.jsonl", [
        {"type": "user", "sessionId": "new-sess", "cwd": str(home / "repo"), "timestamp": "2026-10-16T13:00:00Z",
         "message": {"role": "user", "content": packet["markdown"]}}])
    tr.poll_once()
    new = tr.sessions.load(T.session_node_id("claude-code", "new-sess"))
    assert new["handoff_from"] == packet["id"] and new["requests"] == []     # the brief is not "what you said"
    assert tr.store.get_node(packet["id"])["metadata"]["to_session"] == new["id"]
    from shadow_link.live import is_real_reply
    assert not is_real_reply(packet["markdown"])                            # never learned as your reply


def test_scripted_runs_are_recorded_but_not_listed(tmp_path: Path):
    _jsonl(tmp_path / ".claude/projects/p/sdk-run.jsonl", [
        {"type": "user", "sessionId": "sdk-run", "entrypoint": "sdk-cli", "cwd": str(tmp_path),
         "timestamp": "2026-10-16T12:00:00Z", "message": {"role": "user", "content": "Agent: Codex.\nGoal: summarise"}}])
    _jsonl(tmp_path / ".codex/sessions/2026/10/16/rollout-exec.jsonl", [
        {"type": "session_meta", "timestamp": "2026-10-16T12:00:00Z", "payload": {"id": "exec-run", "cwd": str(tmp_path), "source": "exec"}},
        {"type": "response_item", "timestamp": "2026-10-16T12:00:00Z", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "call shadow_sessions please"}]}}])
    _jsonl(tmp_path / ".claude/projects/p/person.jsonl", [
        {"type": "user", "sessionId": "person", "entrypoint": "cli", "cwd": str(tmp_path),
         "timestamp": "2026-10-16T12:00:00Z", "message": {"role": "user", "content": "build the\nsettings page"}}])
    tr = _tracker(tmp_path, tmp_path)
    tr.poll_once()
    listed = tr.sessions.list()
    assert [s["goal"] for s in listed] == ["build the settings page"]          # one line, scripted runs hidden
    every = {s["automated"] for s in tr.sessions.list(include_automated=True)}
    assert every == {"", "claude sdk-cli", "codex exec"}


def _opencode_db(home: Path, repo: Path) -> Path:
    """A minimal OpenCode store: a session with a user turn, a reply, an edit and a failing bash call."""
    db = home / ".local/share/opencode/opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, slug TEXT, directory TEXT,
                              title TEXT, version TEXT, time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER,
                           time_updated INTEGER, data TEXT);
        CREATE TABLE credential (id TEXT PRIMARY KEY, value TEXT);
        INSERT INTO credential VALUES ('c1', 'sk-ant-should-never-be-read-000000');
    """)
    now = int(time.time() * 1000)
    c.execute("INSERT INTO session VALUES ('ses_1','p',NULL,'s',?,'t','1',?,?)", (str(repo), now, now))
    c.execute("INSERT INTO session VALUES ('ses_sub','p','ses_1','s',?,'t','1',?,?)", (str(repo), now, now))
    msgs = [("m1", "ses_1", {"role": "user"}), ("m2", "ses_1", {"role": "assistant", "path": {"cwd": str(repo)}}),
            ("m3", "ses_sub", {"role": "user"})]
    for i, (mid, sid, data) in enumerate(msgs):
        c.execute("INSERT INTO message VALUES (?,?,?,?,?)", (mid, sid, now + i, now + i, json.dumps(data)))
    parts = [
        ("p1", "m1", "ses_1", {"type": "text", "text": "add retries to the fetcher"}),
        ("p2", "m2", "ses_1", {"type": "text", "text": "Added retries. Want me to add tests too?"}),
        ("p3", "m2", "ses_1", {"type": "tool", "tool": "edit", "state": {"status": "completed",
                                "input": {"filePath": str(repo / "fetcher.py")}}}),
        ("p4", "m2", "ses_1", {"type": "tool", "tool": "bash", "state": {"status": "error",
                                "input": {"command": "pytest -q"}}}),
        ("p5", "m3", "ses_sub", {"type": "text", "text": "subagent task prompt here"}),
    ]
    for i, (pid, mid, sid, data) in enumerate(parts):
        c.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (pid, mid, sid, now + 10 + i, now + 10 + i, json.dumps(data)))
    c.commit()
    c.close()
    return db


def test_opencode_is_tracked_from_its_database(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _opencode_db(tmp_path, repo)
    tr = _tracker(tmp_path, tmp_path)
    tr.poll_once()
    (s,) = tr.sessions.list(agent="opencode")
    assert s["label"] == "OpenCode" and s["project_name"] == "repo" and s["goal"] == "add retries to the fetcher"
    rec = tr.sessions.load(s["id"])
    assert any(f.endswith("fetcher.py") for f in rec["files"])
    assert rec["commands"][-1] == {**rec["commands"][-1], "cmd": "pytest -q", "status": "error"}
    assert rec["open_thread"] == "Want me to add tests too?"
    assert "sk-ant" not in json.dumps(tr.sessions.all())                    # credentials are never read
    sub = [x for x in tr.sessions.list(agent="opencode", include_automated=True) if x["automated"]]
    assert [x["automated"] for x in sub] == ["opencode subagent"]
    assert tr.poll_once() == []                                              # nothing new, nothing re-read


def test_gemini_cli_is_tracked_and_new_messages_only(tmp_path: Path):
    chat = tmp_path / ".gemini/tmp/proj/chats/session-1.json"
    chat.parent.mkdir(parents=True)
    body = {"sessionId": "g1", "messages": [
        {"type": "user", "timestamp": "2026-10-16T12:00:00Z", "content": [{"text": "build the cluster runner"}]},
        {"type": "gemini", "timestamp": "2026-10-16T12:00:05Z", "content": "Done. Shall I wire it to the CLI?"}]}
    chat.write_text(json.dumps(body), encoding="utf-8")
    tr = _tracker(tmp_path, tmp_path)
    tr.poll_once()
    (s,) = tr.sessions.list(agent="gemini")
    assert s["label"] == "Gemini CLI" and s["goal"] == "build the cluster runner"
    body["messages"].append({"type": "user", "timestamp": "2026-10-16T12:01:00Z", "content": [{"text": "yes wire it"}]})
    time.sleep(0.05)
    chat.write_text(json.dumps(body), encoding="utf-8")
    import os
    os.utime(chat, (time.time() + 5, time.time() + 5))
    tr.poll_once()
    rec = tr.sessions.load(s["id"])
    assert rec["requests"] == ["build the cluster runner", "yes wire it"]    # the first one not read twice


def test_opencode_and_gemini_history_import(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _opencode_db(tmp_path, repo)
    from shadow_link.ingester import _opencode_sessions
    sessions = dict(_opencode_sessions(tmp_path))
    assert list(sessions) == ["ses_1"]                                       # subagent runs are not you
    assert [r for r, *_ in sessions["ses_1"]] == ["user", "assistant"]


def test_antigravity_ide_is_tracked_too(tmp_path: Path):
    _jsonl(tmp_path / ".gemini/antigravity/brain/ide-sess/.system_generated/logs/transcript.jsonl", [
        {"type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-10-16T12:00:00Z",
         "content": "<USER_REQUEST>\nbuild the settings page\n</USER_REQUEST>"}])
    tr = _tracker(tmp_path, tmp_path)
    tr.poll_once()
    (s,) = tr.sessions.list()
    assert s["agent"] == "antigravity-ide" and s["label"] == "Antigravity IDE" and s["goal"] == "build the settings page"


def test_ignored_projects_are_not_recorded(home: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SHADOW_TRACK_IGNORE", str(home / "repo"))
    tr = _tracker(home, tmp_path)
    assert tr.poll_once() == [] and tr.sessions.list() == []


def test_saves_only_touch_what_changed(home: Path, tmp_path: Path, monkeypatch):
    tr = _tracker(home, tmp_path)
    tr.poll_once()
    writes = []
    real_update = tr.store.update_node
    monkeypatch.setattr(tr.store, "update_node", lambda nid, **kw: (writes.append(nid), real_update(nid, **kw)))
    log = home / ".codex/sessions/2026/10/16/rollout-2026-10-16-codexsess.jsonl"
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "response_item", "timestamp": "2026-10-16T12:20:00Z", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "looks good"}]}}) + "\n")
    tr.poll_once()
    assert not any(w.startswith("A-") for w in writes)      # no file changed, so no activity rewrite


def test_rebuild_rereads_and_keeps_model_summaries(home: Path, tmp_path: Path):
    tr = _tracker(home, tmp_path)
    tr.poll_once()
    codex = [s for s in tr.sessions.list() if s["agent"] == "codex"][0]
    tr.set_digest(codex["id"], "Codex added rate limiting; npm test fails.", "haiku",
                  ["Rate limit per API key, not per IP"])
    assert any(n["content"] == "Rate limit per API key, not per IP"
               for n in tr.sessions._linked(codex["id"], "decision"))
    before = {s["id"]: s for s in tr.sessions.list()}
    assert tr.rebuild() == 3
    after = {s["id"]: s for s in tr.sessions.list()}
    assert set(after) == set(before)
    assert after[codex["id"]]["digest"] == "Codex added rate limiting; npm test fails."
    rec = tr.sessions.load(codex["id"])
    assert [d["text"] for d in rec["decisions"]] == ["Rate limit per API key, not per IP"]   # kept
    assert rec["requests"].count("add rate limiting to the api") == 1         # read once, not twice
    assert list(rec["files"].values())[0]["edits"] == 1


def test_prune_and_forget(home: Path, tmp_path: Path):
    tr = _tracker(home, tmp_path)
    tr.poll_once()
    assert tr.prune(time.time())["activity"] == 0            # nothing old yet
    assert tr.prune(time.time() + 40 * 86400)["activity"] >= 2
    claude = [s for s in tr.sessions.list() if s["agent"] == "claude-code"][0]
    assert tr.forget(claude["id"])["sessions"] == 1
    assert claude["id"] not in {s["id"] for s in tr.sessions.list()}
    out = tr.forget(str(home / "repo"))
    assert out["sessions"] == 2 and tr.sessions.list() == []
    assert not tr.store._ids_where("type = 'project'")


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
    assert "default_tools_approval_mode" not in toml                         # Codex asks unless you opt in
    (tmp_path / "h2/.codex").mkdir(parents=True)
    connect(["codex"], home=tmp_path / "h2", auto_approve=True)
    assert 'default_tools_approval_mode = "approve"' in (tmp_path / "h2/.codex/config.toml").read_text(encoding="utf-8")


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
        assert get("/stats")["database_bytes"] > 0
        forgot = post("/forget", {"target": codex["id"]})
        assert forgot["sessions"] == 1 and forgot["nodes"] >= 2      # the session and its activity
        assert codex["id"] not in {s["id"] for s in get("/sessions")["sessions"]}
        assert post("/forget", {"target": codex["id"]})["sessions"] == 0
    finally:
        server.shutdown()


def test_one_prediction_per_reply_and_input_validation(tmp_path: Path, monkeypatch):
    import shadow_link.live as live_mod
    from shadow_link.api import ShadowLinkAPIHandler
    from urllib.error import HTTPError
    monkeypatch.setenv("DB_PATH", str(tmp_path / "p.db"))
    calls = []
    monkeypatch.setattr(live_mod, "build_prediction",
                        lambda db, text, top_k=4, timeout_seconds=None: (calls.append(text), ({"text": "ok", "confidence": 0.5, "source": "retrieved", "tier": "medium"}, []))[1])
    watcher = live_mod.LiveWatcher(str(tmp_path / "p.db"), str(tmp_path / "none.jsonl"))
    monkeypatch.setattr(live_mod, "ACTIVE_WATCHER", watcher)
    server = ThreadingHTTPServer(("localhost", 0), ShadowLinkAPIHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://localhost:{server.server_address[1]}"

    def post(path, body):
        req = Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        return json.loads(urlopen(req, timeout=10).read())

    def status(path, body=None):
        try:
            if body is None:
                urlopen(base + path, timeout=10)
            else:
                post(path, body)
            return 200
        except HTTPError as e:
            return e.code

    try:
        # The plugin asks first; the watcher then sees the same reply and reuses the answer.
        assert post("/predict", {"question": "Done. Want me to push?"})["ghost"]["text"] == "ok"
        watcher._handle("assistant", "Done.  Want me to push?")
        assert calls == ["Done. Want me to push?"]
        # The watcher first; the plugin's /predict reuses it.
        watcher._handle("assistant", "Tests pass now.")
        post("/predict", {"question": "Tests pass now."})
        assert calls == ["Done. Want me to push?", "Tests pass now."]
        assert status("/query", {"text": "x", "top_k": "lots"}) == 400
        assert status("/pairs?limit=abc") == 400
        assert status("/pairs?limit=5") == 200
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
