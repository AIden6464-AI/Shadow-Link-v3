"""
shadow_link/database.py

SQLite-backed interaction graph.

Schema
------
prompts       — node table; stores text, bucket, weight, metadata (JSON).
prompt_edges  — edge table; pairwise Jaccard weights between prompt nodes.

Features
--------
- Leak-proof _connection context manager (commit on exit, close always).
- Dynamic token caching in metadata JSON column.
- BFS theme discovery: clusters nodes above a similarity threshold.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from shadow_link.tokenizer import TokenSet, jaccard, tokenize

logger = logging.getLogger(__name__)

# Bucket names — keep in sync with engine.py and retrieval.py
BUCKETS = ("request", "reminder", "rejection", "correction", "pivot")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT    NOT NULL,
    bucket      TEXT    NOT NULL DEFAULT 'request',
    weight      REAL    NOT NULL DEFAULT 1.0,
    created_at  REAL    NOT NULL,           -- Unix timestamp
    triggered_at REAL,                      -- Last retrieval hit
    metadata    TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS prompt_edges (
    src_id  INTEGER NOT NULL,
    dst_id  INTEGER NOT NULL,
    weight  REAL    NOT NULL,
    PRIMARY KEY (src_id, dst_id),
    FOREIGN KEY (src_id) REFERENCES prompts(id),
    FOREIGN KEY (dst_id) REFERENCES prompts(id)
);

-- Embedding vectors live in their OWN table, never in prompts.metadata, so the
-- hot read path (all_prompts) stays lean instead of parsing ~768 floats/row.
CREATE TABLE IF NOT EXISTS prompt_vectors (
    prompt_id INTEGER NOT NULL,
    field     TEXT    NOT NULL,          -- 'embed_vec' (reply) | 'embed_ctx_vec' (situation)
    model     TEXT    NOT NULL,
    vec       TEXT    NOT NULL,          -- JSON float array
    PRIMARY KEY (prompt_id, field),
    FOREIGN KEY (prompt_id) REFERENCES prompts(id)
);

CREATE INDEX IF NOT EXISTS idx_prompts_bucket    ON prompts(bucket);
CREATE INDEX IF NOT EXISTS idx_prompts_weight    ON prompts(weight DESC);
CREATE INDEX IF NOT EXISTS idx_edges_src         ON prompt_edges(src_id);
"""

class Database:
    """SQLite interaction graph."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._init_schema()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """
        Leak-proof connection context manager.
        """
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connection() as conn:
            conn.executescript(_SCHEMA)

    def add_prompt(
        self,
        text: str,
        bucket: str = "request",
        weight: float = 1.0,
        metadata: dict | None = None,
    ) -> int:
        """Insert a prompt node; return new row id."""
        if bucket not in BUCKETS:
            raise ValueError(f"Unknown bucket '{bucket}'. Valid: {BUCKETS}")
        meta = metadata or {}
        now = time.time()
        with self._connection() as conn:
            cur = conn.execute(
                "INSERT INTO prompts (text, bucket, weight, created_at, metadata) "
                "VALUES (?, ?, ?, ?, ?)",
                (text, bucket, weight, now, json.dumps(meta)),
            )
            new_id: int = cur.lastrowid
        self._update_edges_for(new_id)
        return new_id

    def get_prompt(self, prompt_id: int) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM prompts WHERE id = ?", (prompt_id,)
            ).fetchone()
        return dict(row) if row else None

    def all_prompts(self) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM prompts ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def update_triggered(self, prompt_id: int) -> None:
        """Record that this prompt was retrieved/used right now."""
        with self._connection() as conn:
            conn.execute(
                "UPDATE prompts SET triggered_at = ? WHERE id = ?",
                (time.time(), prompt_id),
            )

    def cache_embedding(self, prompt_id: int, vec: list[float], model: str,
                        field: str = "embed_vec") -> None:
        """
        Persist a row's embedding in the dedicated prompt_vectors table.
        """
        with self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO prompt_vectors (prompt_id, field, model, vec) "
                "VALUES (?, ?, ?, ?)",
                (prompt_id, field, model, json.dumps(vec)),
            )

    def get_embedding(self, prompt_id: int, field: str, model: str) -> list[float] | None:
        """Return a cached embedding vector, or None."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT vec FROM prompt_vectors WHERE prompt_id = ? AND field = ? AND model = ?",
                (prompt_id, field, model),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["vec"])
        except (json.JSONDecodeError, TypeError):
            return None

    def reinforce(self, prompt_id: int, bump: float = 0.25,
                  cap: float = 5.0, floor: float = 0.05) -> float:
        """
        Feedback loop: nudge a stored reply's weight by bump, clamped to
        [floor, cap], and reset the decay clock.
        """
        now = time.time()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT weight FROM prompts WHERE id = ?", (prompt_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"No prompt with id {prompt_id}")
            new_weight = max(floor, min(cap, float(row["weight"]) + bump))
            conn.execute(
                "UPDATE prompts SET weight = ?, triggered_at = ? WHERE id = ?",
                (new_weight, now, prompt_id),
            )
        return new_weight

    def bulk_add_reactions(self, items: list[dict]) -> int:
        """
        Bulk-insert reaction rows in ONE transaction.
        """
        now = time.time()
        rows = []
        for it in items:
            b = it.get("bucket", "request")
            if b not in BUCKETS:
                b = "request"
            rows.append((
                it["text"], b, float(it.get("weight", 1.0)),
                float(it.get("created_at") or now),
                float(it.get("triggered_at") or now),
                json.dumps(it.get("metadata") or {}),
            ))
        if not rows:
            return 0
        with self._connection() as conn:
            conn.executemany(
                "INSERT INTO prompts (text, bucket, weight, created_at, triggered_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def _get_cached_tokens(self, row: dict) -> TokenSet:
        meta = json.loads(row.get("metadata") or "{}")
        if "token_flat_set" in meta:
            ts = TokenSet()
            ts.words = meta["token_flat_set"]
            return ts
        ts = tokenize(row["text"])
        meta["token_flat_set"] = list(ts.as_flat_set())
        with self._connection() as conn:
            conn.execute(
                "UPDATE prompts SET metadata = ? WHERE id = ?",
                (json.dumps(meta), row["id"]),
            )
        return ts

    def _update_edges_for(self, new_id: int) -> None:
        """Compute and store Jaccard edge weights from new_id to all others."""
        all_rows = self.all_prompts()
        new_row = next((r for r in all_rows if r["id"] == new_id), None)
        if new_row is None:
            return
        ts_new = tokenize(new_row["text"])

        edges: list[tuple[int, int, float]] = []
        for row in all_rows:
            if row["id"] == new_id:
                continue
            ts_other = self._get_cached_tokens(row)
            w = jaccard(ts_new, ts_other)
            if w > 0.0:
                edges.append((new_id, row["id"], w))
                edges.append((row["id"], new_id, w))

        if edges:
            with self._connection() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO prompt_edges (src_id, dst_id, weight) "
                    "VALUES (?, ?, ?)",
                    edges,
                )

    def get_edges(self) -> list[dict]:
        """Return all edges (unique pairs only)."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT src_id, dst_id, weight FROM prompt_edges WHERE src_id < dst_id "
                "ORDER BY weight DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def discover_themes(self, threshold: float = 0.30) -> list[list[int]]:
        """
        BFS cluster prompt nodes into thematic groups.
        """
        edges = self.get_edges()
        adjacency: dict[int, set[int]] = {}
        for e in edges:
            if e["weight"] >= threshold:
                adjacency.setdefault(e["src_id"], set()).add(e["dst_id"])
                adjacency.setdefault(e["dst_id"], set()).add(e["src_id"])

        all_ids = {r["id"] for r in self.all_prompts()}
        visited: set[int] = set()
        clusters: list[list[int]] = []

        for node_id in sorted(all_ids):
            if node_id in visited:
                continue
            cluster: list[int] = []
            queue: deque[int] = deque([node_id])
            while queue:
                n = queue.popleft()
                if n in visited:
                    continue
                visited.add(n)
                cluster.append(n)
                for neighbor in adjacency.get(n, set()):
                    if neighbor not in visited:
                        queue.append(neighbor)
            clusters.append(cluster)

        return clusters

    def stats(self) -> dict:
        with self._connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
            by_bucket = {
                row["bucket"]: row["cnt"]
                for row in conn.execute(
                    "SELECT bucket, COUNT(*) AS cnt FROM prompts GROUP BY bucket"
                ).fetchall()
            }
            edges = conn.execute("SELECT COUNT(*) FROM prompt_edges").fetchone()[0]
        return {"total_prompts": total, "by_bucket": by_bucket, "total_edges": edges}

    def clear(self) -> None:
        """Clear all prompts and edges from the database."""
        with self._connection() as conn:
            conn.execute("DELETE FROM prompt_edges")
            conn.execute("DELETE FROM prompts")
