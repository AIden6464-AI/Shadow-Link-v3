"""
shadow_link/aca/store.py

Cognitive Store + Synapse Graph.

Typed nodes (memories, files, errors, decisions, hypotheses, episodes, ...)
connected by typed, weighted, learnable synapses. Lives in the same SQLite
file as the reaction-pair corpus; existing `prompts` rows are mirrored in as
`reaction` nodes and their Jaccard `prompt_edges` as `similar_to` synapses,
so the corpus Shadow Link already learned is the starting graph.

Tables
------
aca_nodes     — one row per cognitive object.
aca_synapses  — directed typed relationships (src, dst, rel) with learning stats.
aca_events    — append-only replay log of everything ACA did.
aca_tasks     — persisted Working Brain state per task.
aca_meta      — small key/value store (sync cursors, global attention, agents).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from shadow_link.tokenizer import NEGATIVE_MODIFIERS

NODE_TYPES = frozenset({
    "memory", "reaction", "file", "function", "class", "commit", "error", "fix",
    "decision", "requirement", "goal", "task", "tool_result", "hypothesis",
    "conversation", "finding", "test_result", "research", "dependency",
    "entity", "architecture", "episode", "semantic", "procedure", "observation",
    "event",
    # Shadow Core: what every coding agent is doing (see docs/SHADOW_CORE_PLAN.md)
    "agent_session", "activity", "open_thread", "handoff", "project", "digest",
})

NODE_STATUSES = frozenset({
    "active", "temporary", "superseded", "incorrect", "refuted", "archived",
})

# Relations whose meaning is symmetric are stored in both directions.
SYMMETRIC_RELS = frozenset({"related_to", "similar_to", "contradicts", "co_active"})

# Relations that carry inhibition rather than excitation during spreading.
INHIBITORY_RELS = frozenset({"supersedes", "refutes"})

# Default trust by node type (how much to believe it, independent of relevance).
DEFAULT_TRUST: dict[str, float] = {
    "test_result": 0.99, "tool_result": 0.97, "error": 0.97, "file": 0.98,
    "function": 0.98, "class": 0.98, "commit": 0.98, "research": 0.94,
    "requirement": 0.92, "decision": 0.9, "architecture": 0.9, "goal": 0.9,
    "task": 0.9, "fix": 0.88, "observation": 0.85, "procedure": 0.85,
    "episode": 0.85, "reaction": 0.8, "memory": 0.75, "semantic": 0.75,
    "finding": 0.78, "conversation": 0.7, "dependency": 0.95, "entity": 0.9,
    "event": 0.9, "hypothesis": 0.31,
    "agent_session": 0.95, "activity": 0.95, "open_thread": 0.85, "handoff": 0.9,
    "project": 0.98, "digest": 0.85,
}

# Freshness half-life (hours) by node type; inf = does not go stale.
HALF_LIFE_HOURS: dict[str, float] = {
    "architecture": math.inf, "decision": 24 * 90, "requirement": 24 * 90,
    "procedure": math.inf, "semantic": 24 * 180, "episode": 24 * 60,
    "goal": 24 * 30, "file": 24 * 30, "function": 24 * 30, "class": 24 * 30,
    "dependency": 24 * 30, "entity": math.inf, "commit": 24 * 14,
    "fix": 24 * 14, "memory": 24 * 14, "reaction": 24 * 7, "research": 24 * 30,
    "finding": 24 * 3, "conversation": 24 * 3, "task": 24 * 7,
    "hypothesis": 24, "error": 12, "test_result": 6, "tool_result": 6,
    "observation": 6, "event": 6,
    "agent_session": 24 * 30, "activity": 24 * 14, "open_thread": 24 * 14,
    "handoff": 24 * 30, "project": math.inf, "digest": 24 * 90,
}

_STOP = frozenset("""
a an the and or but if then else of to in on at by for with from as is are was
were be been being it its this that these those i you he she we they me my your
our their what which who whom why how when where do does did done have has had
can could should would will just so than too very again there here into out up
down over under about also any some all each more most other such only own same
let lets get got make made please now
""".split())

_WORD_RE = re.compile(r"[a-z0-9_]+")


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def terms(text: str) -> set[str]:
    """Content-word set used for seeding, novelty and duplicate detection."""
    text = (text or "").lower()
    out: set[str] = set()
    for raw in _WORD_RE.findall(text):
        if raw in NEGATIVE_MODIFIERS:
            out.add("not")
            continue
        if raw in _STOP or len(raw) < 3:
            continue
        out.add(_stem(raw))
        # split snake_case / dotted identifiers into parts too
        if "_" in raw:
            out.update(_stem(p) for p in raw.split("_") if len(p) >= 3 and p not in _STOP)
    return out


def term_overlap(a: set[str], b: set[str]) -> float:
    """Jaccard over content terms (ignores the 'not' polarity marker)."""
    a2, b2 = a - {"not"}, b - {"not"}
    if not a2 or not b2:
        return 0.0
    return len(a2 & b2) / len(a2 | b2)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS aca_nodes (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    content      TEXT NOT NULL DEFAULT '',
    importance   REAL NOT NULL DEFAULT 0.5,
    trust        REAL NOT NULL DEFAULT 0.75,
    status       TEXT NOT NULL DEFAULT 'active',
    project      TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    last_used_at REAL,
    use_count    INTEGER NOT NULL DEFAULT 0,
    useful_count INTEGER NOT NULL DEFAULT 0,
    useless_count INTEGER NOT NULL DEFAULT 0,
    terms        TEXT NOT NULL DEFAULT '',
    metadata     TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS aca_synapses (
    src          TEXT NOT NULL,
    dst          TEXT NOT NULL,
    rel          TEXT NOT NULL,
    strength     REAL NOT NULL DEFAULT 0.5,
    confidence   REAL NOT NULL DEFAULT 0.8,
    frequency    INTEGER NOT NULL DEFAULT 1,
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count   INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    last_used_at REAL,
    PRIMARY KEY (src, dst, rel)
);
CREATE INDEX IF NOT EXISTS idx_aca_syn_dst ON aca_synapses(dst);
CREATE INDEX IF NOT EXISTS idx_aca_nodes_type ON aca_nodes(type);
CREATE TABLE IF NOT EXISTS aca_events (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    task_id  TEXT NOT NULL DEFAULT '',
    cycle    INTEGER NOT NULL DEFAULT 0,
    kind     TEXT NOT NULL,
    node_id  TEXT NOT NULL DEFAULT '',
    value    REAL,
    detail   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_aca_events_task ON aca_events(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_aca_events_ts ON aca_events(ts);
CREATE TABLE IF NOT EXISTS aca_tasks (
    id         TEXT PRIMARY KEY,
    goal       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    closed     INTEGER NOT NULL DEFAULT 0,
    state      TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS aca_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class CognitiveStore:
    """Persistent typed graph of everything the system knows."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        # A shared connection is required for :memory: so state survives calls.
        self._mem_conn: sqlite3.Connection | None = None
        if self.db_path == ":memory:":
            self._mem_conn = sqlite3.connect(":memory:", isolation_level=None,
                                             check_same_thread=False)
            self._mem_conn.row_factory = sqlite3.Row
        with self._conn() as c:
            c.executescript(_SCHEMA)
        self._terms_cache: dict[str, set[str]] = {}
        self._prefix_cache: dict[str, set[str]] = {}
        self._log_buf: list[tuple] | None = None

    def prefixes(self, node_id: str) -> set[str]:
        p = self._prefix_cache.get(node_id)
        if p is None:
            p = {t[:4] for t in self._terms_cache.get(node_id, ()) if len(t) >= 4}
            self._prefix_cache[node_id] = p
        return p

    def _set_terms(self, node_id: str, ts: set[str]) -> None:
        self._terms_cache[node_id] = ts
        self._prefix_cache.pop(node_id, None)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if self._mem_conn is not None:
            yield self._mem_conn
            return
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ nodes
    def add_node(
        self,
        content: str,
        type: str = "memory",
        title: str = "",
        *,
        node_id: str | None = None,
        importance: float = 0.5,
        trust: float | None = None,
        status: str = "active",
        project: str = "",
        source: str = "",
        metadata: dict | None = None,
    ) -> str:
        if type not in NODE_TYPES:
            raise ValueError(f"Unknown node type '{type}'. Valid: {sorted(NODE_TYPES)}")
        if status not in NODE_STATUSES:
            raise ValueError(f"Unknown status '{status}'. Valid: {sorted(NODE_STATUSES)}")
        nid = node_id or f"{type[:3].upper()}-{uuid.uuid4().hex[:8]}"
        t = trust if trust is not None else DEFAULT_TRUST.get(type, 0.75)
        tset = terms(f"{title} {content}")
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO aca_nodes (id, type, title, content, importance, trust,"
                " status, project, source, created_at, terms, metadata)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (nid, type, title or content[:80], content, _clip(importance), _clip(t),
                 status, project, source, time.time(), " ".join(sorted(tset)),
                 json.dumps(metadata or {})),
            )
        self._set_terms(nid, tset)
        return nid

    def get_node(self, node_id: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM aca_nodes WHERE id = ?", (node_id,)).fetchone()
        return _node_row(r) if r else None

    def get_nodes(self, ids: Iterable[str]) -> dict[str, dict]:
        ids = list(dict.fromkeys(ids))
        out: dict[str, dict] = {}
        with self._conn() as c:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                q = ",".join("?" * len(chunk))
                for r in c.execute(f"SELECT * FROM aca_nodes WHERE id IN ({q})", chunk):
                    out[r["id"]] = _node_row(r)
        return out

    def all_node_terms(self) -> list[tuple[str, str, str, set[str]]]:
        """(id, type, status, terms) for every node — the seeding index."""
        with self._conn() as c:
            rows = c.execute("SELECT id, type, status, terms FROM aca_nodes").fetchall()
        out = []
        for r in rows:
            ts = self._terms_cache.get(r["id"])
            if ts is None:
                ts = set(r["terms"].split()) if r["terms"] else set()
                self._terms_cache[r["id"]] = ts
            out.append((r["id"], r["type"], r["status"], ts))
        return out

    def find_by_title(self, needle: str, limit: int = 5) -> list[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id FROM aca_nodes WHERE lower(title) = lower(?) "
                "OR lower(title) LIKE lower(?) LIMIT ?",
                (needle, f"%{needle}%", limit),
            ).fetchall()
        return [r["id"] for r in rows]

    def set_status(self, node_id: str, status: str) -> None:
        if status not in NODE_STATUSES:
            raise ValueError(f"Unknown status '{status}'")
        with self._conn() as c:
            c.execute("UPDATE aca_nodes SET status = ? WHERE id = ?", (status, node_id))

    def update_node(self, node_id: str, **fields) -> None:
        allowed = {"importance", "trust", "status", "title", "content", "metadata", "project"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"Cannot update field '{k}'")
            if k == "metadata":
                v = json.dumps(v)
            elif k in ("importance", "trust"):
                v = _clip(v)
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return
        with self._conn() as c:
            c.execute(f"UPDATE aca_nodes SET {', '.join(sets)} WHERE id = ?", (*vals, node_id))
            if "title" in fields or "content" in fields:
                r = c.execute("SELECT title, content FROM aca_nodes WHERE id=?", (node_id,)).fetchone()
                if r:
                    ts = terms(f"{r['title']} {r['content']}")
                    c.execute("UPDATE aca_nodes SET terms=? WHERE id=?", (" ".join(sorted(ts)), node_id))
                    self._set_terms(node_id, ts)

    def touch(self, node_ids: Iterable[str]) -> None:
        now = time.time()
        with self._conn() as c:
            c.executemany(
                "UPDATE aca_nodes SET last_used_at = ?, use_count = use_count + 1 WHERE id = ?",
                [(now, i) for i in node_ids],
            )

    def mark_usefulness(self, node_id: str, useful: bool) -> None:
        col = "useful_count" if useful else "useless_count"
        with self._conn() as c:
            c.execute(f"UPDATE aca_nodes SET {col} = {col} + 1 WHERE id = ?", (node_id,))

    def prune_temporary(self, max_age_hours: float = 48.0) -> int:
        """Forget temporary memories that were never used again."""
        cutoff = time.time() - max_age_hours * 3600
        return self._delete_nodes(self._ids_where(
            "status='temporary' AND use_count=0 AND created_at < ?", (cutoff,)))

    def prune_events(self, max_age_days: float = 30.0) -> int:
        """Drop replay events older than `max_age_days`."""
        cutoff = time.time() - max_age_days * 86400
        with self._conn() as c:
            return c.execute("DELETE FROM aca_events WHERE ts < ?", (cutoff,)).rowcount

    def _ids_where(self, where: str, params: tuple = ()) -> list[str]:
        with self._conn() as c:
            return [r["id"] for r in c.execute(f"SELECT id FROM aca_nodes WHERE {where}", params)]

    def _delete_nodes(self, ids: list[str]) -> int:
        """Delete nodes and every synapse touching them."""
        with self._conn() as c:
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                q = ",".join("?" * len(chunk))
                c.execute(f"DELETE FROM aca_synapses WHERE src IN ({q}) OR dst IN ({q})", chunk + chunk)
                c.execute(f"DELETE FROM aca_nodes WHERE id IN ({q})", chunk)
        for i in ids:
            self._terms_cache.pop(i, None)
            self._prefix_cache.pop(i, None)
        return len(ids)

    def count_nodes(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM aca_nodes").fetchone()[0]

    # --------------------------------------------------------------- synapses
    def link(
        self,
        src: str,
        dst: str,
        rel: str = "related_to",
        *,
        strength: float = 0.5,
        confidence: float = 0.8,
        source: str = "",
    ) -> None:
        """Create or strengthen a synapse. Re-linking bumps frequency and keeps the max strength."""
        if src == dst:
            return
        pairs = [(src, dst)] + ([(dst, src)] if rel in SYMMETRIC_RELS else [])
        now = time.time()
        with self._conn() as c:
            for a, b in pairs:
                c.execute(
                    "INSERT INTO aca_synapses (src, dst, rel, strength, confidence, source, created_at)"
                    " VALUES (?,?,?,?,?,?,?)"
                    " ON CONFLICT(src, dst, rel) DO UPDATE SET"
                    "   frequency = frequency + 1,"
                    "   strength = MAX(strength, excluded.strength),"
                    "   confidence = MAX(confidence, excluded.confidence)",
                    (a, b, rel, _clip(strength), _clip(confidence), source, now),
                )

    def neighbors(self, node_id: str) -> list[dict]:
        """Outgoing synapses plus incoming ones (flagged reverse=True)."""
        with self._conn() as c:
            out = [dict(r, reverse=False) for r in c.execute(
                "SELECT * FROM aca_synapses WHERE src = ?", (node_id,))]
            inc = [dict(r, reverse=True) for r in c.execute(
                "SELECT * FROM aca_synapses WHERE dst = ?", (node_id,))]
        # Symmetric relations already exist in both directions; skip their mirror.
        inc = [e for e in inc if e["rel"] not in SYMMETRIC_RELS]
        return out + inc

    def neighbors_many(self, node_ids: Iterable[str]) -> dict[str, list[dict]]:
        """`neighbors()` for many nodes in one connection (one spreading layer)."""
        ids = list(dict.fromkeys(node_ids))
        out: dict[str, list[dict]] = {i: [] for i in ids}
        with self._conn() as c:
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                q = ",".join("?" * len(chunk))
                for r in c.execute(f"SELECT * FROM aca_synapses WHERE src IN ({q})", chunk):
                    out[r["src"]].append(dict(r, reverse=False))
                for r in c.execute(f"SELECT * FROM aca_synapses WHERE dst IN ({q})", chunk):
                    if r["rel"] not in SYMMETRIC_RELS:
                        out[r["dst"]].append(dict(r, reverse=True))
        return out

    def synapses_between(self, ids: Iterable[str]) -> list[dict]:
        ids = list(ids)
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                f"SELECT * FROM aca_synapses WHERE src IN ({q}) AND dst IN ({q})", ids + ids)]

    def adjust_synapse(self, src: str, dst: str, rel: str, useful: bool, lr: float = 0.15) -> float | None:
        """Learning rule: useful → strength += lr·(1−s); useless → strength −= lr·s."""
        with self._conn() as c:
            r = c.execute("SELECT strength FROM aca_synapses WHERE src=? AND dst=? AND rel=?",
                          (src, dst, rel)).fetchone()
            if r is None:
                return None
            s = float(r["strength"])
            s = s + lr * (1 - s) if useful else s - lr * s
            col = "success_count" if useful else "fail_count"
            rels = [(src, dst)] + ([(dst, src)] if rel in SYMMETRIC_RELS else [])
            for a, b in rels:
                c.execute(
                    f"UPDATE aca_synapses SET strength=?, {col}={col}+1, last_used_at=?"
                    " WHERE src=? AND dst=? AND rel=?",
                    (_clip(s), time.time(), a, b, rel),
                )
        return s

    def count_synapses(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM aca_synapses").fetchone()[0]

    # ----------------------------------------------------------------- events
    def log(self, kind: str, *, task_id: str = "", cycle: int = 0, node_id: str = "",
            value: float | None = None, **detail) -> None:
        row = (time.time(), task_id, cycle, kind, node_id, value, json.dumps(detail, default=str))
        if self._log_buf is not None:
            self._log_buf.append(row)
            return
        self._write_log([row])

    def _write_log(self, rows: list[tuple]) -> None:
        if not rows:
            return
        with self._conn() as c:
            c.executemany(
                "INSERT INTO aca_events (ts, task_id, cycle, kind, node_id, value, detail)"
                " VALUES (?,?,?,?,?,?,?)", rows)

    @contextmanager
    def buffered_log(self) -> Iterator[None]:
        """Collect replay events in memory and write them in one transaction."""
        if self._log_buf is not None:      # already buffering (nested)
            yield
            return
        self._log_buf = []
        try:
            yield
        finally:
            rows, self._log_buf = self._log_buf, None
            self._write_log(rows)

    def replay(self, task_id: str, limit: int = 500) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM aca_events WHERE task_id = ? ORDER BY seq LIMIT ?",
                (task_id, limit)).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    # ------------------------------------------------------------------ tasks
    def load_task(self, task_id: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM aca_tasks WHERE id = ?", (task_id,)).fetchone()
        if not r:
            return None
        return {**dict(r), "state": json.loads(r["state"])}

    def save_task(self, task_id: str, goal: str, state: dict, closed: bool = False) -> None:
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO aca_tasks (id, goal, created_at, updated_at, closed, state)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET goal=excluded.goal, updated_at=excluded.updated_at,"
                "   closed=excluded.closed, state=excluded.state",
                (task_id, goal, now, now, int(closed), json.dumps(state, default=str)),
            )

    # ------------------------------------------------------------------- meta
    def get_meta(self, key: str, default=None):
        with self._conn() as c:
            r = c.execute("SELECT value FROM aca_meta WHERE key = ?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_meta(self, key: str, value) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO aca_meta (key, value) VALUES (?, ?)",
                      (key, json.dumps(value)))

    # ------------------------------------------------- reaction-corpus bridge
    def sync_from_prompts(self, edge_floor: float = 0.15) -> int:
        """
        Mirror new `prompts` rows into reaction nodes and their Jaccard edges into
        `similar_to` synapses. Incremental; also refreshes importance from the
        corpus weights so accept/reject feedback reaches ACA. Returns rows added.
        """
        with self._conn() as c:
            has = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='prompts'").fetchone()
        if not has:
            return 0
        cursor = int(self.get_meta("synced_prompt_id", 0))
        with self._conn() as c:
            max_prompt = c.execute("SELECT COALESCE(MAX(id), 0) FROM prompts").fetchone()[0]
        if max_prompt < cursor:
            # The corpus was cleared and ids restarted: every mirrored reply is stale.
            self._delete_nodes(self._ids_where("id LIKE 'P-%'"))
            cursor = 0
            self.set_meta("synced_prompt_id", 0)
        else:
            # Replies deleted from the corpus must not keep activating.
            self._delete_nodes(self._ids_where(
                "id LIKE 'P-%' AND CAST(substr(id, 3) AS INTEGER) NOT IN (SELECT id FROM prompts)"))
        with self._conn() as c:
            rows = c.execute("SELECT * FROM prompts WHERE id > ? ORDER BY id", (cursor,)).fetchall()
        added = 0
        max_id = cursor
        for r in rows:
            try:
                meta = json.loads(r["metadata"] or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            situation = (meta.get("agent_output") or meta.get("question") or "").strip()
            node_meta = {"bucket": r["bucket"], "prompt_id": r["id"]}
            if situation:
                node_meta["situation"] = situation[:1000]
            self.add_node(
                content=r["text"],
                type="reaction",
                title=r["text"][:80],
                node_id=f"P-{r['id']}",
                importance=min(1.0, float(r["weight"]) / 2.0),
                source=str(meta.get("source", "corpus")),
                metadata=node_meta,
            )
            # Situation terms also seed this node: the reaction is *about* it.
            if situation:
                # Polarity belongs to the situation, not to the reply: a rejection
                # ("do not use docker") must still match "I'll use docker".
                ts = terms(situation) | (terms(r["text"]) - {"not"})
                with self._conn() as c:
                    c.execute("UPDATE aca_nodes SET terms=? WHERE id=?",
                              (" ".join(sorted(ts)), f"P-{r['id']}"))
                self._set_terms(f"P-{r['id']}", ts)
            added += 1
            max_id = max(max_id, r["id"])
        if rows:
            with self._conn() as c:
                edges = c.execute(
                    "SELECT src_id, dst_id, weight FROM prompt_edges"
                    " WHERE src_id < dst_id AND weight >= ? AND (src_id > ? OR dst_id > ?)",
                    (edge_floor, cursor, cursor)).fetchall()
            for e in edges:
                self.link(f"P-{e['src_id']}", f"P-{e['dst_id']}", "similar_to",
                          strength=float(e["weight"]), confidence=0.7, source="jaccard")
            self.set_meta("synced_prompt_id", max_id)
        with self._conn() as c:
            c.execute(
                "UPDATE aca_nodes SET importance = MIN(1.0, p.weight / 2.0) FROM prompts p"
                " WHERE aca_nodes.id = 'P-' || p.id")
        return added


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


def _node_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["metadata"] = json.loads(d.get("metadata") or "{}")
    d.pop("terms", None)
    return d
