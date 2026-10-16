"""
shadow_link/tracker.py

Shadow Core tracking: follows what every coding agent is doing.

The tracker tails each agent's own session files incrementally (Claude Code
transcripts, Codex CLI rollouts, Antigravity CLI transcripts), turns their lines
into events, folds events into one record per session, and keeps that record in
the ACA graph as an `agent_session` node linked to its project, the files it
changed and its test runs. Nothing here needs a model: digests start as a
template and a client (the Claude Code plugin's Haiku) may replace them.

Handoff packets (`build_handoff`) are built only on request, from that record.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from shadow_link.aca.store import CognitiveStore

AGENTS = ("claude-code", "codex", "antigravity")
AGENT_LABELS = {"claude-code": "Claude Code", "codex": "Codex", "antigravity": "Antigravity"}

ACTIVE_SECONDS = 10 * 60          # active: something happened in the last 10 minutes
IDLE_DIGEST_SECONDS = 10 * 60     # a session quiet this long gets a digest
ENDED_SECONDS = 24 * 3600
BACKFILL_SECONDS = 30 * 86400     # on first sight, sessions older than this are left to `ingest-sessions`

_CURSOR_KEY = "tracker:cursors"

# ----------------------------------------------------------------- redaction
_SECRET_RES = [
    re.compile(r"\b(sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,})"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})"),
    re.compile(r"\b(xox[abpr]-[A-Za-z0-9-]{10,})"),
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
    re.compile(r"\b(AIza[0-9A-Za-z_-]{30,})"),
    re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)"),
    # key = value / "token": "value" for names that sound secret
    re.compile(r"(?i)((?:api[_-]?key|secret|token|password|passwd|bearer)[\"']?\s*[:=]\s*[\"']?)"
               r"([^\s\"',;]{8,})"),
]


def redact(text: str) -> str:
    """Mask credentials before anything is stored."""
    if not text:
        return text
    for rx in _SECRET_RES[:-1]:
        text = rx.sub("[redacted]", text)
    return _SECRET_RES[-1].sub(lambda m: m.group(1) + "[redacted]", text)


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


# ------------------------------------------------------------ project roots
_ROOT_CACHE: dict[str, str] = {}


def project_root(path: str) -> str:
    """The repo root above `path` (the folder holding .git), else `path` itself."""
    if not path:
        return ""
    p = os.path.normpath(path)
    if p in _ROOT_CACHE:
        return _ROOT_CACHE[p]
    cur = Path(p)
    root = p
    for cand in [cur, *cur.parents]:
        try:
            if (cand / ".git").exists():
                root = str(cand)
                break
        except OSError:
            break
    _ROOT_CACHE[p] = root
    return root


def project_name(root: str) -> str:
    return Path(root).name if root else "unknown"


_TEST_RE = re.compile(r"\b(pytest|unittest|npm (?:run )?test|pnpm test|yarn test|go test|cargo test|"
                      r"jest|vitest|mvn test|gradle test|dotnet test|claude plugin test)\b")


# ------------------------------------------------------------------- events
# An event is a dict: kind ("user" | "assistant" | "tool"), ts, and for tools
# tool, command, written, read, status ("ok" | "error" | None).

def _epoch(value) -> float:
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e12 else 1.0)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


class _FileParser:
    """Per-file parsing state. Subclasses turn one JSONL record into events."""

    agent = ""

    def __init__(self, path: str, state: dict | None = None) -> None:
        state = state or {}
        self.path = path
        self.session_id: str = state.get("sid") or Path(path).stem
        self.cwd: str = state.get("cwd", "")
        self.pending: dict[str, dict] = {}

    def snapshot(self) -> dict:
        return {"sid": self.session_id, "cwd": self.cwd}

    def parse(self, rec: dict) -> list[dict]:
        raise NotImplementedError


_WRITE_TOOLS_CLAUDE = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


class ClaudeCodeParser(_FileParser):
    agent = "claude-code"

    def parse(self, rec: dict) -> list[dict]:
        from shadow_link.live import operator_text, _extract_text, is_real_reply, clean_reply
        if rec.get("isSidechain") or rec.get("isMeta"):
            return []
        if rec.get("cwd"):
            self.cwd = rec["cwd"]
        if rec.get("sessionId"):
            self.session_id = rec["sessionId"]
        typ = rec.get("type")
        msg = rec.get("message") or {}
        content = msg.get("content")
        ts = _epoch(rec.get("timestamp"))
        out: list[dict] = []
        if typ == "assistant":
            text = _extract_text(content).strip()
            if text:
                out.append({"kind": "assistant", "text": text, "ts": ts})
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    name = b.get("name", "")
                    path = inp.get("file_path") or inp.get("notebook_path") or ""
                    self.pending[b.get("id", "")] = {
                        "kind": "tool", "tool": name, "ts": ts,
                        "command": inp.get("command", "") if name in ("Bash", "PowerShell") else "",
                        "written": [path] if path and name in _WRITE_TOOLS_CLAUDE else [],
                        "read": [path] if path and name == "Read" else [],
                        "status": None,
                    }
        elif typ == "user":
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        ev = self.pending.pop(b.get("tool_use_id", ""), None)
                        if ev:
                            ev["status"] = "error" if b.get("is_error") else "ok"
                            out.append(ev)
            text = operator_text(content)
            if text and is_real_reply(text):
                out.append({"kind": "user", "text": clean_reply(text), "ts": ts})
        return out


_PATCH_FILE_RE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: ([^\n\\\"]+(?:\\\\[^\n\\\"]+)*)")
_JS_CMD_RE = re.compile(r"\bcmd\s*:\s*\"((?:[^\"\\]|\\.)*)\"")
_EXIT_RE = re.compile(r"exit_code\\?\"?\s*:\s*(-?\d+)|exited with code (-?\d+)|Exit code:? (-?\d+)")


def _js_unescape(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except ValueError:
        return s


def _status_from_output(text: str) -> str | None:
    codes = [int(next(g for g in m.groups() if g is not None)) for m in _EXIT_RE.finditer(text or "")]
    if not codes:
        return None
    return "error" if any(c != 0 for c in codes) else "ok"


class CodexParser(_FileParser):
    agent = "codex"

    def parse(self, rec: dict) -> list[dict]:
        from shadow_link.ingester import _CODEX_INJECTED
        from shadow_link.live import is_real_reply, clean_reply
        typ = rec.get("type")
        p = rec.get("payload") or {}
        ts = _epoch(rec.get("timestamp"))
        if typ == "session_meta":
            self.session_id = p.get("id") or p.get("session_id") or self.session_id
            self.cwd = p.get("cwd") or self.cwd
            return []
        if typ == "turn_context":
            self.cwd = p.get("cwd") or self.cwd
            return []
        if typ != "response_item":
            return []
        ptype = p.get("type")
        if ptype == "message" and p.get("role") in ("user", "assistant"):
            role = p["role"]
            want = "input_text" if role == "user" else "output_text"
            parts = [c.get("text", "") for c in p.get("content") or []
                     if isinstance(c, dict) and c.get("type") == want]
            if role == "user":
                parts = [t for t in parts if not t.lstrip().startswith(_CODEX_INJECTED)]
            text = "\n".join(t.strip() for t in parts if t.strip()).strip()
            if role == "user" and "## My request for Codex:" in text:
                text = text.split("## My request for Codex:", 1)[1].strip()
            if not text:
                return []
            if role == "user":
                return [{"kind": "user", "text": clean_reply(text), "ts": ts}] if is_real_reply(text) else []
            return [{"kind": "assistant", "text": text, "ts": ts}]
        if ptype in ("custom_tool_call", "function_call"):
            raw = p.get("input") if ptype == "custom_tool_call" else p.get("arguments")
            raw = raw if isinstance(raw, str) else json.dumps(raw or {})
            written = [_js_unescape(f).strip() for f in _PATCH_FILE_RE.findall(raw)]
            cmds = [_js_unescape(c) for c in _JS_CMD_RE.findall(raw)]
            if not cmds and ptype == "function_call":
                try:
                    args = json.loads(raw)
                    c = args.get("cmd") or args.get("command")
                    cmds = [" ".join(c) if isinstance(c, list) else str(c)] if c else []
                except (ValueError, AttributeError):
                    pass
            self.pending[p.get("call_id", "")] = {
                "kind": "tool", "tool": p.get("name", ""), "ts": ts,
                "command": "; ".join(cmds)[:400], "written": written, "read": [], "status": None,
            }
            return []
        if ptype in ("custom_tool_call_output", "function_call_output"):
            ev = self.pending.pop(p.get("call_id", ""), None)
            if not ev:
                return []
            out = p.get("output")
            text = out if isinstance(out, str) else json.dumps(out)
            ev["status"] = _status_from_output(text) or ("ok" if ev["written"] or ev["command"] else None)
            return [ev] if (ev["written"] or ev["command"]) else []
        return []


_AGY_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
_AGY_WRITE = ("write", "replace", "edit", "create")


def _agy_arg(args: dict, *names: str) -> str:
    for n in names:
        v = args.get(n)
        if isinstance(v, str) and v:
            try:
                v = json.loads(v)          # Antigravity JSON-encodes each argument
            except ValueError:
                pass
            return str(v)
    return ""


class AntigravityParser(_FileParser):
    agent = "antigravity"

    def __init__(self, path: str, state: dict | None = None) -> None:
        super().__init__(path, state)
        if not (state or {}).get("sid"):
            self.session_id = Path(path).parents[2].name
        self._last_cmd: dict | None = None

    def parse(self, rec: dict) -> list[dict]:
        from shadow_link.live import is_real_reply, clean_reply
        kind, content = rec.get("type"), rec.get("content")
        ts = _epoch(rec.get("created_at"))
        out: list[dict] = []
        if kind == "USER_INPUT" and rec.get("source") == "USER_EXPLICIT" and isinstance(content, str):
            m = _AGY_REQUEST_RE.search(content)
            if m and is_real_reply(m.group(1)):
                out.append({"kind": "user", "text": clean_reply(m.group(1)), "ts": ts})
        elif kind == "PLANNER_RESPONSE":
            if isinstance(content, str) and content.strip():
                out.append({"kind": "assistant", "text": content.strip(), "ts": ts})
            for call in rec.get("tool_calls") or []:
                name = call.get("name", "")
                args = call.get("args") or {}
                cwd = _agy_arg(args, "Cwd")
                if cwd:
                    self.cwd = cwd
                path = _agy_arg(args, "TargetFile", "AbsolutePath", "FilePath", "File")
                cmd = _agy_arg(args, "CommandLine")
                ev = {"kind": "tool", "tool": name, "ts": ts, "command": cmd,
                      "written": [path] if path and any(w in name for w in _AGY_WRITE) else [],
                      "read": [path] if path and ("view" in name or "read" in name) else [],
                      "status": "ok" if not cmd else None}
                if cmd:
                    self._last_cmd = ev
                if cmd or ev["written"]:
                    out.append(ev)
        elif kind == "GENERIC" and isinstance(content, str) and self._last_cmd is not None:
            status = _status_from_output(content)
            if status:
                self._last_cmd["status"] = status
                out.append(dict(self._last_cmd, kind="tool_status"))
                self._last_cmd = None
        return out


SOURCES: list[tuple[str, type[_FileParser], str]] = [
    ("claude-code", ClaudeCodeParser, ".claude/projects/*/*.jsonl"),
    ("codex", CodexParser, ".codex/sessions/*/*/*/rollout-*.jsonl"),
    ("antigravity", AntigravityParser, ".gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl"),
]


# ------------------------------------------------------------ session record
def new_record(agent: str, session_id: str, transcript: str) -> dict:
    return {
        "id": session_node_id(agent, session_id), "agent": agent, "session_id": session_id,
        "transcript": transcript, "project": "", "cwd": "", "started": 0.0, "last": 0.0,
        "goal": "", "requests": [], "last_agent": "", "turns": 0,
        "files": {}, "commands": [], "errors": [], "tests": [],
        "open_thread": "", "digest": "", "digest_by": "", "digest_at": 0.0,
    }


def session_node_id(agent: str, session_id: str) -> str:
    return f"S-{agent}-{hashlib.sha1(session_id.encode()).hexdigest()[:10]}"


def _open_question(text: str) -> str:
    """The question an agent left hanging at the end of its reply, if any."""
    tail = text.strip()[-600:]
    sentences = re.split(r"(?<=[.!?])\s+", tail)
    for s in reversed(sentences[-3:]):
        if s.strip().endswith("?"):
            return _clip(s.strip(), 240)
    return ""


def apply_event(rec: dict, ev: dict, cwd: str) -> None:
    ts = ev.get("ts") or time.time()
    rec["started"] = rec["started"] or ts
    rec["last"] = max(rec["last"], ts)
    if cwd and cwd != rec["cwd"]:
        rec["cwd"] = cwd
        rec["project"] = project_root(cwd)
    kind = ev["kind"]
    if kind == "user":
        text = redact(ev["text"])
        if not rec["goal"]:
            rec["goal"] = _clip(text, 240)
        rec["requests"] = (rec["requests"] + [_clip(text, 400)])[-8:]
        rec["turns"] += 1
        rec["open_thread"] = ""
    elif kind == "assistant":
        text = redact(ev["text"])
        rec["last_agent"] = _clip(text, 1200)
        rec["open_thread"] = _open_question(text)
    elif kind in ("tool", "tool_status"):
        for f in (os.path.normpath(p) for p in ev.get("written") or [] if p):
            entry = rec["files"].setdefault(f, {"edits": 0, "last": 0.0})
            entry["edits"] += 1
            entry["last"] = ts
        cmd = redact(ev.get("command") or "")
        if cmd:
            if kind == "tool_status" and rec["commands"] and rec["commands"][-1]["cmd"] == _clip(cmd, 300):
                rec["commands"][-1]["status"] = ev.get("status")
            else:
                rec["commands"] = (rec["commands"] + [
                    {"cmd": _clip(cmd, 300), "status": ev.get("status"), "ts": ts}])[-15:]
            if _TEST_RE.search(cmd) and ev.get("status"):
                rec["tests"] = (rec["tests"] + [{"cmd": _clip(cmd, 200), "status": ev["status"], "ts": ts}])[-5:]
        if ev.get("status") == "error":
            what = cmd or (ev.get("written") or [ev.get("tool", "tool")])[0]
            rec["errors"] = (rec["errors"] + [{"what": _clip(what, 200), "ts": ts}])[-6:]


def session_state(rec: dict, now: float | None = None) -> str:
    age = (now or time.time()) - (rec.get("last") or 0)
    if age < ACTIVE_SECONDS:
        return "active"
    return "idle" if age < ENDED_SECONDS else "ended"


def template_digest(rec: dict) -> str:
    """A model-free summary (the goal is shown beside it, so it is not repeated)."""
    files = sorted(rec["files"].items(), key=lambda kv: -kv[1]["last"])
    parts = []
    if files:
        names = ", ".join(Path(f).name for f, _ in files[:6])
        parts.append(f"Changed {len(files)} file(s): {names}")
    if rec["tests"]:
        t = rec["tests"][-1]
        parts.append(f"Last test run {'passed' if t['status'] == 'ok' else 'failed'}: {_clip(t['cmd'], 120)}")
    if rec["requests"]:
        parts.append(f"Last request: {_clip(rec['requests'][-1], 160)}")
    if rec["open_thread"]:
        parts.append(f"Waiting on: {rec['open_thread']}")
    return "\n".join(parts) or "No changes recorded."


def session_summary(rec: dict, now: float | None = None) -> dict:
    """The compact view clients list (sidebar rows, MCP)."""
    files = sorted(rec["files"].items(), key=lambda kv: -kv[1]["last"])
    last_cmd = rec["commands"][-1] if rec["commands"] else None
    doing = ""
    if files and (not last_cmd or files[0][1]["last"] >= last_cmd["ts"]):
        doing = f"editing {Path(files[0][0]).name}"
    elif last_cmd:
        doing = f"ran {_clip(last_cmd['cmd'], 60)}"
    elif rec["requests"]:
        doing = _clip(rec["requests"][-1], 60)
    return {
        "id": rec["id"], "agent": rec["agent"], "label": AGENT_LABELS.get(rec["agent"], rec["agent"]),
        "project": rec["project"], "project_name": project_name(rec["project"]),
        "state": session_state(rec, now), "started": rec["started"], "last": rec["last"],
        "goal": rec["goal"], "doing": doing, "files_changed": len(rec["files"]),
        "open_thread": rec["open_thread"], "errors": len(rec["errors"]),
        "digest": rec["digest"], "digest_by": rec["digest_by"],
        "needs_digest": session_state(rec, now) != "active" and rec["digest_by"] in ("", "template")
                        and rec["turns"] > 0,
    }


# ------------------------------------------------------------------ storage
class SessionStore:
    """Session records kept as ACA nodes, with their links."""

    def __init__(self, store: CognitiveStore) -> None:
        self.store = store

    def load(self, node_id: str) -> dict | None:
        n = self.store.get_node(node_id)
        if not n or n["type"] != "agent_session":
            return None
        rec = n["metadata"]
        return rec if isinstance(rec, dict) and rec.get("id") else None

    def all(self) -> list[dict]:
        with self.store._conn() as c:
            rows = c.execute("SELECT metadata FROM aca_nodes WHERE type = 'agent_session'").fetchall()
        out = []
        for r in rows:
            try:
                rec = json.loads(r["metadata"] or "{}")
            except ValueError:
                continue
            if rec.get("id"):
                out.append(rec)
        return out

    def save(self, rec: dict) -> None:
        title = f"{AGENT_LABELS.get(rec['agent'], rec['agent'])} · {project_name(rec['project'])}" \
                f" · {_clip(rec['goal'], 70)}"
        content = "\n".join(filter(None, [
            rec["goal"], *rec["requests"][-4:], " ".join(Path(f).name for f in rec["files"]),
            rec["digest"],
        ]))
        if self.store.get_node(rec["id"]):
            self.store.update_node(rec["id"], title=title, content=content, metadata=rec,
                                   project=rec["project"])
        else:
            self.store.add_node(content, type="agent_session", title=title, node_id=rec["id"],
                                importance=0.6, project=rec["project"], source=rec["agent"],
                                metadata=rec)
        if rec["project"]:
            pid = f"PRJ-{hashlib.sha1(rec['project'].lower().encode()).hexdigest()[:10]}"
            if not self.store.get_node(pid):
                self.store.add_node(rec["project"], type="project", title=project_name(rec["project"]),
                                    node_id=pid, importance=0.7, project=rec["project"], source="tracker")
            self.store.link(rec["id"], pid, "in", strength=0.8, source="tracker")
        for f, info in rec["files"].items():
            fid = f"F-{hashlib.sha1(f.lower().encode()).hexdigest()[:12]}"
            if not self.store.get_node(fid):
                self.store.add_node(f, type="file", title=Path(f).name, node_id=fid,
                                    project=rec["project"], source="tracker")
            aid = f"A-{rec['id'][2:]}-{fid[2:]}"
            content = f"{AGENT_LABELS.get(rec['agent'], rec['agent'])} edited {f} ({info['edits']}x)"
            if self.store.get_node(aid):
                self.store.update_node(aid, content=content)
            else:
                self.store.add_node(content, type="activity", title=f"edited {Path(f).name}",
                                    node_id=aid, project=rec["project"], source=rec["agent"])
                self.store.link(rec["id"], aid, "did", strength=0.7, source="tracker")
                self.store.link(aid, fid, "touched", strength=0.8, source="tracker")
        if rec["open_thread"]:
            tid = f"OT-{rec['id'][2:]}"
            text = rec["open_thread"]
            if self.store.get_node(tid):
                self.store.update_node(tid, content=text, status="active")
            else:
                self.store.add_node(text, type="open_thread", title=_clip(text, 80), node_id=tid,
                                    project=rec["project"], source=rec["agent"])
                self.store.link(rec["id"], tid, "left_open", strength=0.7, source="tracker")
        else:
            tid = f"OT-{rec['id'][2:]}"
            if self.store.get_node(tid):
                self.store.set_status(tid, "archived")

    def set_digest(self, node_id: str, text: str, by: str) -> dict | None:
        rec = self.load(node_id)
        if rec is None:
            return None
        rec["digest"] = redact(_clip(text, 1500))
        rec["digest_by"] = by
        rec["digest_at"] = time.time()
        self.save(rec)
        did = f"D-{node_id[2:]}"
        if self.store.get_node(did):
            self.store.update_node(did, content=rec["digest"])
        else:
            self.store.add_node(rec["digest"], type="digest", title=f"digest: {_clip(rec['goal'], 60)}",
                                node_id=did, project=rec["project"], source=by, importance=0.65)
            self.store.link(node_id, did, "summarized_by", strength=0.8, source="tracker")
        return rec

    def list(self, project: str = "", agent: str = "", active_only: bool = False,
             limit: int = 20, exclude: str = "") -> list[dict]:
        now = time.time()
        recs = self.all()
        if exclude:   # the asking client's own session (its raw id)
            recs = [r for r in recs if r.get("session_id") != exclude]
        if project:
            want = project_root(project).lower()
            recs = [r for r in recs if (r.get("project") or "").lower() == want]
        if agent:
            recs = [r for r in recs if r["agent"] == agent]
        # A run nobody spoke in (Codex's approval reviewer, a bare tool loop) is not work to track.
        recs = [r for r in recs if r.get("turns")]
        out = [session_summary(r, now) for r in recs]
        if active_only:
            out = [s for s in out if s["state"] == "active"]
        out.sort(key=lambda s: -s["last"])
        return out[:limit]

    def threads(self, project: str = "", limit: int = 10) -> list[dict]:
        return [
            {"session": s["id"], "agent": s["label"], "project_name": s["project_name"],
             "text": s["open_thread"], "last": s["last"]}
            for s in self.list(project=project, limit=100)
            if s["open_thread"] and s["state"] != "ended"
        ][:limit]


# ------------------------------------------------------------------ tracker
class SessionTracker:
    """Tails every agent's session files and keeps the records current."""

    def __init__(self, db_path: str | Path, home: str | Path | None = None,
                 poll_seconds: float = 2.0, backfill_seconds: float = BACKFILL_SECONDS) -> None:
        self.store = CognitiveStore(db_path)
        self.sessions = SessionStore(self.store)
        self.home = Path(home) if home else Path.home()
        self.poll_seconds = poll_seconds
        self.backfill_seconds = backfill_seconds
        self._cursors: dict[str, dict] = self.store.get_meta(_CURSOR_KEY, {}) or {}
        self._parsers: dict[str, _FileParser] = {}
        self._records: dict[str, dict] = {}
        self._running = False
        self._lock = threading.Lock()
        self.errors: dict[str, str] = {}      # source -> last read problem, shown to clients

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name="shadow-tracker").start()

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            try:
                self.poll_once()
            except Exception as e:           # one bad file must not stop tracking
                self.errors["tracker"] = str(e)
            time.sleep(self.poll_seconds)

    def _files(self) -> list[tuple[str, type[_FileParser], Path]]:
        out = []
        for agent, parser, pattern in SOURCES:
            try:
                for p in self.home.glob(pattern):
                    if "subagents" in p.parts:
                        continue
                    out.append((agent, parser, p))
                self.errors.pop(agent, None)
            except OSError as e:
                self.errors[agent] = str(e)
        return out

    def poll_once(self) -> list[str]:
        """Read whatever was appended since last time; returns session ids changed."""
        now = time.time()
        changed: dict[str, dict] = {}
        with self._lock:
            for agent, parser_cls, path in self._files():
                key = str(path)
                try:
                    st = path.stat()
                except OSError:
                    continue
                cur = self._cursors.get(key)
                if cur is None:
                    if now - st.st_mtime > self.backfill_seconds:
                        self._cursors[key] = {"offset": st.st_size, "mtime": st.st_mtime}
                        continue
                    cur = self._cursors[key] = {"offset": 0, "mtime": 0}
                if st.st_size < cur["offset"]:          # replaced or truncated
                    cur.update(offset=0)
                    self._parsers.pop(key, None)
                if st.st_size == cur["offset"]:
                    continue
                parser = self._parsers.get(key)
                if parser is None:
                    parser = self._parsers[key] = parser_cls(key, cur.get("state"))
                try:
                    with open(path, "rb") as f:
                        f.seek(cur["offset"])
                        data = f.read(st.st_size - cur["offset"])
                except OSError as e:
                    self.errors[agent] = str(e)
                    continue
                end = data.rfind(b"\n")
                if end < 0:
                    continue                               # a line still being written
                cur["offset"] += end + 1
                cur["mtime"] = st.st_mtime
                for line in data[: end + 1].split(b"\n"):
                    if not line.strip():
                        continue
                    try:
                        rec_json = json.loads(line.decode("utf-8", errors="ignore"))
                    except ValueError:
                        continue
                    try:
                        events = parser.parse(rec_json)
                    except Exception as e:
                        self.errors[agent] = f"{path.name}: {e}"
                        continue
                    if not events:
                        continue
                    sid = session_node_id(agent, parser.session_id)
                    rec = changed.get(sid) or self._records.get(sid) or self.sessions.load(sid) \
                        or new_record(agent, parser.session_id, key)
                    for ev in events:
                        apply_event(rec, ev, parser.cwd)
                    changed[sid] = rec
                cur["state"] = parser.snapshot()
            for sid, rec in changed.items():
                self._records[sid] = rec
                self.sessions.save(rec)
            self._write_digests(now)
            self.store.set_meta(_CURSOR_KEY, self._cursors)
        return list(changed)

    def _write_digests(self, now: float) -> None:
        """Quiet sessions get a template digest until a client writes a better one."""
        for rec in self._records.values():
            if rec["turns"] and not rec["digest"] and now - rec["last"] >= IDLE_DIGEST_SECONDS:
                self.sessions.set_digest(rec["id"], template_digest(rec), "template")
                fresh = self.sessions.load(rec["id"])
                if fresh:
                    rec.update(fresh)

    def set_digest(self, session_id: str, text: str, by: str) -> dict | None:
        """Store a client's digest without racing the poll loop's own save."""
        with self._lock:
            rec = self.sessions.set_digest(session_id, text, by)
            if rec is not None:
                self._records[session_id] = rec
            return rec

    def report(self, agent: str, what: str, *, session: str = "", project: str = "",
               files: list[str] | None = None, command: str = "", status: str | None = None) -> str:
        """Manual tracking (MCP `shadow_report`, POST /track/event) for any agent."""
        agent = (agent or "agent").strip().lower()
        sid_raw = session or f"{agent}:{project_root(project) or 'unknown'}"
        sid = session_node_id(agent, sid_raw)
        with self._lock:
            rec = self._records.get(sid) or self.sessions.load(sid) or new_record(agent, sid_raw, "")
            ts = time.time()
            if what:
                apply_event(rec, {"kind": "assistant", "text": what, "ts": ts}, project)
            if files or command:
                apply_event(rec, {"kind": "tool", "tool": "report", "ts": ts, "command": command,
                                  "written": list(files or []), "read": [], "status": status}, project)
            if not rec["goal"] and what:
                rec["goal"] = _clip(redact(what), 240)
            rec["turns"] = max(rec["turns"], 1)
            self._records[sid] = rec
            self.sessions.save(rec)
        return sid


# ------------------------------------------------------------------ handoff
def build_handoff(store: CognitiveStore, session_id: str, to_agent: str = "") -> dict | None:
    """A short brief another agent can pick the work up from. Recorded as a node."""
    sessions = SessionStore(store)
    rec = sessions.load(session_id)
    if rec is None:
        return None
    label = AGENT_LABELS.get(rec["agent"], rec["agent"])
    files = sorted(rec["files"].items(), key=lambda kv: -kv[1]["last"])
    lines = [f"# Handoff from {label}: {project_name(rec['project'])}", ""]
    if rec["project"]:
        lines.append(f"Project: `{rec['project']}`")
    lines.append(f"Goal: {rec['goal'] or 'unknown'}")
    if rec["digest"] and rec["digest_by"] != "template":
        lines += ["", "## Where it stands", _clip(rec["digest"], 600)]
    if files:
        lines += ["", f"## Files changed ({len(files)})"]
        lines += [f"- `{f}` ({info['edits']} edit{'s' if info['edits'] != 1 else ''})" for f, info in files[:6]]
        if len(files) > 6:
            lines.append(f"- …and {len(files) - 6} more")
    cmds = [c for c in rec["commands"] if c.get("cmd")][-4:]
    if cmds:
        lines += ["", "## Recent commands"]
        lines += [f"- `{_clip(c['cmd'], 110)}`{'  (failed)' if c.get('status') == 'error' else ''}" for c in cmds]
    if rec["tests"]:
        t = rec["tests"][-1]
        lines += ["", f"Last tests: `{_clip(t['cmd'], 110)}` {'passed' if t['status'] == 'ok' else 'FAILED'}"]
    if rec["errors"]:
        lines += ["", "## Recent errors"] + [f"- {_clip(e['what'], 120)}" for e in rec["errors"][-3:]]
    if rec["requests"]:
        lines += ["", "## What the user asked most recently"]
        lines += [f"- {_clip(r, 160)}" for r in rec["requests"][-3:]]
    if rec["last_agent"]:
        lines += ["", f"## {label}'s last reply", _clip(rec["last_agent"], 360)]
    if rec["open_thread"]:
        lines += ["", f"Open question: {rec['open_thread']}"]
    lines += ["", "Continue this work: check the files above, then pick up from the last request."]
    text = redact("\n".join(lines))
    hid = f"H-{rec['id'][2:]}-{int(time.time())}"
    store.add_node(text, type="handoff", title=f"handoff from {label}: {_clip(rec['goal'], 50)}",
                   node_id=hid, project=rec["project"], source="shadow",
                   metadata={"from": rec["id"], "to_agent": to_agent, "at": time.time()})
    store.link(hid, rec["id"], "from", strength=0.8, source="shadow")
    return {"id": hid, "session": rec["id"], "to_agent": to_agent, "markdown": text,
            "from_label": label, "project_name": project_name(rec["project"])}


TRACKER: SessionTracker | None = None
