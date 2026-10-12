"""
shadow_link/live.py

Live transcript watcher. Tails the active Claude Code JSONL transcript,
predicts your likely reply on each assistant turn, and passively captures
reaction pairs on each user turn.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
from pathlib import Path

from shadow_link.engine import Engine, EMBED_MODEL
from shadow_link.database import Database
from shadow_link.ingester import classify_multi

ACTIVE_WATCHER = None

def build_prediction(db_path: str, claude_output: str, top_k: int = 4,
                     timeout_seconds: float | None = None):
    """
    Shared prediction logic.
    Retrieves the most relevant past reactions, then (in LM mode) drafts a NEW
    reply grounded in them.

    `timeout_seconds` (optional) caps the LM call wall-clock for this single
    invocation. No global env mutation - safe under concurrent /nudge calls.
    """
    from shadow_link.engine import Engine
    db = Database(db_path)
    engine = Engine(db)
    results = engine.predict_replies(claude_output, top_k=top_k)
    if not results:
        return None, []

    top = results[0]
    top_signal = top.combined_score * max(0.0, min(top.effective_weight, 2.0))
    if top_signal < float(os.getenv("GHOST_MIN_SIGNAL", "0.10")):
        return None, []

    def pack(r, source="retrieved"):
        signal = r.combined_score * max(0.0, min(r.effective_weight, 2.0))
        tier = "high" if signal >= 0.55 else "medium" if signal >= 0.30 else "low"
        return {
            "id": r.prompt_id, "text": r.text, "bucket": r.bucket,
            "confidence": round(r.combined_score, 3),
            "signal": round(signal, 3),
            "jaccard": round(r.jaccard_sim, 3), "cosine": round(r.cosine_sim, 3),
            "tier": tier, "source": source,
        }

    retrieved_ghost = pack(results[0])
    alternates = [pack(r) for r in results[1:]]
    grounding_ids = [r.prompt_id for r in results]
    # Surfacing a reply as the ghost is a use: re-arm its decay clock so replies
    # that keep applying stay strong instead of fading on wall-clock time alone.
    # Alternates are not re-armed, so weak runners-up still fade.
    db.update_triggered(results[0].prompt_id)

    from shadow_link import generate as gen
    if gen.lm_enabled():
        pairs = []
        for r in results:
            p = db.get_prompt(r.prompt_id) or {}
            try:
                m = json.loads(p.get("metadata") or "{}")
            except Exception:
                m = {}
            pairs.append({"situation": m.get("agent_output") or m.get("question") or "",
                          "reply": r.text})
        drafted = gen.generate_ghost(claude_output, pairs, db=db,
                                     timeout_seconds=timeout_seconds)
        if drafted:
            ghost = {"id": None, "text": drafted, "bucket": "generated",
                     "confidence": retrieved_ghost["confidence"],
                     "jaccard": retrieved_ghost["jaccard"],
                     "cosine": retrieved_ghost["cosine"],
                     "tier": retrieved_ghost["tier"], "source": "generated",
                     "grounding_ids": grounding_ids, "situation": claude_output}
            return ghost, [retrieved_ghost] + alternates

    return retrieved_ghost, alternates

def discover_transcript() -> str:
    """Return the most recently modified Claude Code transcript .jsonl, or ''."""
    root = Path.home() / ".claude" / "projects"
    if not root.exists():
        return ""
    newest, newest_mtime = "", 0.0
    for proj in root.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            if mt > newest_mtime:
                newest_mtime, newest = mt, str(f)
    return newest


def discover_all_transcripts(limit: int = 20) -> list[dict]:
    """Return the N most recent Claude Code transcripts with previews.

    Each item: {path, project, session_id, mtime, last_user, last_assistant}.
    Newest first. For each file we read only the last ~100KB to avoid
    blocking the TUI on multi-MB sessions.
    """
    root = Path.home() / ".claude" / "projects"
    if not root.exists():
        return []
    candidates: list[tuple[float, Path]] = []
    for proj in root.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            candidates.append((mt, f))
    candidates.sort(reverse=True)
    out: list[dict] = []
    for mt, f in candidates[:limit]:
        last_user = ""
        last_assistant = ""
        try:
            size = f.stat().st_size
            with open(f, "rb") as fp:
                if size > 100_000:
                    fp.seek(size - 100_000)
                    # Skip partial first line if we did not start at offset 0.
                    fp.readline()
                tail = fp.read().decode("utf-8", errors="ignore")
            for line in tail.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                msg = obj.get("message") or {}
                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue
                content = msg.get("content", "")
                if isinstance(content, list):
                    text = "\n".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                elif isinstance(content, str):
                    text = content
                else:
                    text = ""
                text = text.strip()
                if not text:
                    continue
                if role == "user":
                    last_user = text
                else:
                    last_assistant = text
        except Exception:
            pass
        out.append({
            "path": str(f),
            "project": f.parent.name,
            "session_id": f.stem,
            "mtime": mt,
            "last_user": last_user[:200],
            "last_assistant": last_assistant[:200],
        })
    return out

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def operator_text(content) -> str:
    """The text a person actually typed in a user message: text blocks only,
    with Claude Code's <system-reminder> spans removed. Tool results, images
    and reminder-only blocks contribute nothing."""
    if isinstance(content, str):
        return _REMINDER_RE.sub("", content).strip()
    if isinstance(content, list):
        parts = [
            _REMINDER_RE.sub("", b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p.strip() for p in parts if p.strip()).strip()
    return ""


def read_tail_lines(path: str, max_bytes: int = 4_000_000, chunk: int = 65_536):
    """Yield complete JSONL lines from the end of a file, newest first,
    reading backwards in chunks instead of loading the whole transcript."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fp:
            end, carry, read = size, b"", 0
            while end > 0 and read < max_bytes:
                start = max(0, end - chunk)
                fp.seek(start)
                buf = fp.read(end - start) + carry
                read += end - start
                end = start
                lines = buf.split(b"\n")
                # The first piece may be a partial line unless we reached the file start.
                carry = lines.pop(0) if start > 0 else b""
                for raw in reversed(lines):
                    if raw.strip():
                        yield raw.decode("utf-8", errors="ignore")
    except OSError:
        return


def is_real_reply(text: str) -> bool:
    """Filter out non-reactions: slash commands, CC wrappers, nudges, trivia."""
    t = (text or "").strip()
    if len(t) < 4 or t.startswith("/"):
        return False
    markers = (
        "<command-name>", "<command-message>", "<local-command", "<system-reminder",
        "<bash-", "[Request interrupted", "Shadow Link — based on past reactions",
        "Shadow Link context",
        "Caveat:", "Shell cwd was reset", "[Image",
        "A session-scoped Stop hook is now active",
        "CORPUS SHAPE", "SHADOW LINK", "GHOST (", "[CLAUDE]", "[GHOST",
    )
    if any(m in t for m in markers):
        return False
    if any(ch in t for ch in "│╭╮╰╯├┤┌┐└┘─━┃"):
        return False
    return True

def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""

class LiveWatcher:
    """Background tailer + predictor + passive capturer."""

    def __init__(self, db_path: str, transcript_path: str | None = None):
        self.db_path = db_path
        self.transcript_path = transcript_path or discover_transcript()
        self._lock = threading.Lock()
        self._pos = 0              # byte offset of the next unread line
        self._partial = b""        # trailing bytes of a line still being written
        self._primed = False
        self._last_claude = ""
        self._last_captured = ""
        self._running = False

        self._state = {
            "transcript": self.transcript_path,
            "claude_output": "",
            "ghost": None,
            "alternates": [],
            "learned_this_session": 0,
            "activity": [],
            "updated_at": time.time(),
        }
        self._activity = deque(maxlen=40)

    def start(self):
        if self._running:
            return
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._warm_embeddings, daemon=True).start()

    def stop(self):
        """Signal the background loops to exit. Threads die on next poll."""
        self._running = False

    def _warm_embeddings(self):
        """Background fill of situation embeddings into prompt_vectors."""
        import logging
        log = logging.getLogger("shadow_link.warm")
        if os.getenv("SHADOW_QUERY_EMBED", "0") in ("0", "", "false", "False"):
            log.info("warm: SHADOW_QUERY_EMBED off; skipping embedding warm.")
            return
        try:
            from shadow_link.engine import Engine, _embed, _embed_model
            ready = False
            for attempt in range(20):
                if not self._running:
                    return
                if _embed("warmup probe") is not None:
                    ready = True
                    break
                time.sleep(min(3 + attempt, 10))
            if not ready:
                log.info("warm: no embed server reachable; staying Jaccard-only.")
                return
            db = Database(self.db_path)
            eng = Engine(db)
            model = _embed_model()
            warmed = 0
            for row in db.all_prompts():
                if not self._running:
                    return
                if db.get_embedding(row["id"], "embed_ctx_vec", model) is not None:
                    continue
                try:
                    meta = json.loads(row.get("metadata") or "{}")
                except Exception:
                    meta = {}
                ctx = (meta.get("agent_output") or meta.get("question") or "").strip()
                if not ctx:
                    continue
                if eng._ctx_embedding(row, ctx, embed_if_missing=True) is not None:
                    warmed += 1
                    if warmed % 25 == 0:
                        log.info("warm: %d situation embeddings cached", warmed)
                time.sleep(0.05)
            log.info("warm: done (%d new embeddings; model=%s)", warmed, model)
        except Exception as e:
            log.warning("warm: aborted: %s", e)

    def get_state(self) -> dict:
        with self._lock:
            s = dict(self._state)
            s["activity"] = list(self._activity)
            return s

    def _log(self, kind: str, text: str):
        self._activity.appendleft({
            "kind": kind,
            "text": text[:200],
            "ts": time.time(),
        })

    def _predict(self, claude_output: str):
        return build_prediction(self.db_path, claude_output, top_k=4)

    def _capture(self, claude_output: str, reply: str):
        db = Database(self.db_path)
        buckets = classify_multi(reply)
        meta = {"buckets": buckets, "source": "live", "agent_output": claude_output[:1000]}
        # The hook and the proxy may capture the same reply; store it once.
        db.add_prompt_unique(text=reply, bucket=buckets[0], metadata=meta)

    def ingest_proxy_event(self, claude_reply: str) -> dict:
        """Proxy push path: Claude's response was just captured at the wire.
        Skips the JSONL tail latency. Returns the ghost (or None) for the
        proxy to optionally surface in its own logs.
        """
        text = (claude_reply or "").strip()
        if not text:
            return {"ghost": None, "skipped": "empty"}
        with self._lock:
            if text == self._last_claude:
                return {"ghost": self._state.get("ghost"), "skipped": "duplicate"}
        ghost, alts = self._predict(text)
        with self._lock:
            self._last_claude = text
            self._state["claude_output"] = text
            self._state["ghost"] = ghost
            self._state["alternates"] = alts
            self._state["updated_at"] = time.time()
            self._log("claude", text)
            if ghost:
                self._log("ghost", f"({ghost['tier']}) {ghost['text']}")
        return {"ghost": ghost, "alternates": alts}

    def _handle(self, role: str, text: str):
        if role == "assistant":
            with self._lock:
                self._last_claude = text
            ghost, alts = self._predict(text)
            with self._lock:
                self._state["claude_output"] = text
                self._state["ghost"] = ghost
                self._state["alternates"] = alts
                self._state["updated_at"] = time.time()
                self._log("claude", text)
                if ghost:
                    self._log("ghost", f"({ghost['tier']}) {ghost['text']}")
        elif role == "user":
            if not is_real_reply(text):
                return
            with self._lock:
                claude = self._last_claude
                dup = (text == self._last_captured)
            if claude and not dup:
                try:
                    self._capture(claude, text)
                    with self._lock:
                        self._last_captured = text
                        self._state["learned_this_session"] += 1
                        self._log("learn", text)
                        self._state["updated_at"] = time.time()
                except Exception:
                    pass

    def _read_new_lines(self) -> list[str]:
        """Complete lines appended since the last read (incremental tail)."""
        size = os.path.getsize(self.transcript_path)
        if size < self._pos:          # truncated or replaced: start over
            self._pos, self._partial = 0, b""
        if size == self._pos:
            return []
        with open(self.transcript_path, "rb") as f:
            f.seek(self._pos)
            data = f.read(size - self._pos)
        self._pos = size
        data = self._partial + data
        pieces = data.split(b"\n")
        self._partial = pieces.pop()  # incomplete last line (or b"")
        return [p.decode("utf-8", errors="ignore") for p in pieces if p.strip()]

    def _prime(self):
        """Start at the end of the transcript and predict on its last assistant turn."""
        self._pos = os.path.getsize(self.transcript_path)
        self._partial = b""
        self._primed = True
        last_assistant = ""
        for line in read_tail_lines(self.transcript_path):
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") == "assistant":
                txt = _extract_text(o.get("message", {}).get("content", ""))
                if txt:
                    last_assistant = txt
                    break
        if last_assistant:
            # Run prediction on prime so /claude-shadow shows a ghost
            # immediately, not only after the next assistant turn.
            ghost, alts = self._predict(last_assistant)
            with self._lock:
                self._last_claude = last_assistant
                self._state["claude_output"] = last_assistant
                self._state["ghost"] = ghost
                self._state["alternates"] = alts
                self._state["updated_at"] = time.time()
                self._log("claude", last_assistant)
                if ghost:
                    self._log("ghost", f"({ghost['tier']}) {ghost['text']}")

    def _loop(self):
        while self._running:
            if not self.transcript_path or not os.path.exists(self.transcript_path):
                self.transcript_path = discover_transcript()
                with self._lock:
                    self._state["transcript"] = self.transcript_path
                self._primed = False
                time.sleep(1.0)
                continue
            try:
                if not self._primed:
                    self._prime()
                    time.sleep(1.0)
                    continue
                lines = self._read_new_lines()
            except OSError:
                time.sleep(1.0)
                continue

            for line in lines:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                role = o.get("message", {}).get("role")
                typ = o.get("type")
                if typ in ("user", "assistant") and role in ("user", "assistant"):
                    content = o.get("message", {}).get("content", "")
                    # User turns: only what the person typed, without Claude Code's reminders.
                    txt = operator_text(content) if role == "user" else _extract_text(content)
                    if txt.strip():
                        self._handle(role, txt.strip())
            time.sleep(1.0)
