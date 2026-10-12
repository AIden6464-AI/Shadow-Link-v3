"""
shadow_link/ingester.py

Handles parsing and ingesting of various log formats.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from shadow_link.database import Database, BUCKETS
from shadow_link.tokenizer import tokenize

KEYWORDS_REJECTION = {
    "too bloated", "bloated", "strip", "wipe", "boilerplate", "remove", 
    "clean", "cliché", "corporate jargon", "bad", "wrong", "revert", "delete",
    "stop writing", "don't need", "unnecessary"
}

KEYWORDS_REMINDER = {
    "remember", "keep concise", "concise", "attention", "always", 
    "limit", "guidelines", "constraint", "parameter", "strictly", "notice"
}

KEYWORDS_CORRECTION = {
    "missed", "error", "bug", "broken", "endpoint", "mistake", 
    "adjust", "gpu", "fail", "allocation", "fix", "incorrect", "correction",
    "missing", "not working", "fails to"
}

KEYWORDS_PIVOT = {
    "instead", "pivot", "change direction", "rebrand", "reframe", 
    "new approach", "move to", "switch", "alternative", "rethink"
}

# Explicit negation in a reply ("do not use docker", "never mock the db") is a
# rejection of what the agent proposed, whatever other keywords it contains.
_NEGATION_RE = re.compile(
    r"\b(?:do not|don't|dont|does not|doesn't|never|avoid|stop using|no need)\b")


def classify_by_heuristics(text: str) -> str:
    """Helper to classify text into a single primary bucket based on keywords."""
    return classify_multi(text)[0]

def classify_multi(text: str) -> list[str]:
    """
    Non-exclusive classification.
    """
    text_lower = text.lower()
    hits: list[str] = []
    if any(kw in text_lower for kw in KEYWORDS_REJECTION) or _NEGATION_RE.search(text_lower):
        hits.append("rejection")
    if any(kw in text_lower for kw in KEYWORDS_CORRECTION):
        hits.append("correction")
    if any(kw in text_lower for kw in KEYWORDS_PIVOT):
        hits.append("pivot")
    if any(kw in text_lower for kw in KEYWORDS_REMINDER):
        hits.append("reminder")
    return hits or ["request"]

class Ingester:
    def __init__(self, db: Database, provider: str = "none", url: str = "", model: str = ""):
        self.db = db
        self.provider = provider
        self.url = url
        self.model = model

    def _process_and_store(
        self,
        content: str,
        prompt_type: str,
        timestamp: str,
        metadata: Dict[str, Any],
        unique: bool = False,
    ) -> int:
        """Helper to tokenize and store in DB (`unique` skips already-captured pairs)."""
        ts = tokenize(content)
        meta = metadata.copy()
        meta['token_flat_set'] = list(ts.as_flat_set())
        meta['prompt_type'] = prompt_type
        meta['timestamp'] = timestamp

        buckets = classify_multi(content)
        meta.setdefault("buckets", buckets)
        bucket = buckets[0]
        
        if unique:
            prompt_id, _ = self.db.add_prompt_unique(text=content, bucket=bucket, weight=1.0, metadata=meta)
            return prompt_id
        prompt_id = self.db.add_prompt(
            text=content,
            bucket=bucket,
            weight=1.0,
            metadata=meta
        )
        return prompt_id

    def ingest_markdown(self, filepath: str | Path) -> int:
        """Parses a Markdown file of prompts."""
        filepath = Path(filepath)
        if not filepath.exists():
            raise FileNotFoundError(f"Markdown file not found: {filepath}")

        with open(filepath, "r", encoding="utf-8") as f:
            text = f.read()

        raw_blocks = re.split(r"###?\s*Prompt:?|---\n", text)
        count = 0

        for block in raw_blocks:
            block = block.strip()
            if not block:
                continue

            if block.startswith("#") or len(block) < 5:
                continue

            metadata = {"source": "markdown_import", "file": filepath.name}
            timestamp = datetime.utcnow().isoformat() + "Z"
            
            self._process_and_store(
                content=block,
                prompt_type="user_prompt",
                timestamp=timestamp,
                metadata=metadata
            )
            count += 1

        return count

    def ingest_oneiros_jsonl(self, filepath: str | Path) -> int:
        """Ingests OGI llm_calls.jsonl."""
        filepath = Path(filepath)
        if not filepath.exists():
            raise FileNotFoundError(f"Jsonl file not found: {filepath}")

        count = 0
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                    ts = data.get("ts", datetime.utcnow().isoformat() + "Z")
                    req = data.get("request", {})
                    messages = req.get("messages", [])

                    user_msgs = [m for m in messages if m.get("role") == "user"]
                    if not user_msgs:
                        continue

                    user_content = user_msgs[-1].get("content", "").strip()
                    if not user_content or len(user_content) < 3:
                        continue

                    metadata = {
                        "source": "oneiros_jsonl",
                        "model": data.get("model", ""),
                        "method": data.get("method", "")
                    }

                    if "error" in data and data["error"]:
                        metadata["error"] = data["error"]

                    self._process_and_store(
                        content=user_content,
                        prompt_type="user_prompt",
                        timestamp=ts,
                        metadata=metadata
                    )
                    count += 1

                except Exception:
                    continue

        return count

    def ingest_oneiros_human_history(self, intake_dir: str | Path) -> int:
        """Ingests paired OGI human question/response history."""
        intake_path = Path(intake_dir)
        if not intake_path.exists():
            return 0

        response_files = list(intake_path.glob("*.response.txt"))
        count = 0

        for r_file in response_files:
            req_id = r_file.name.split(".")[0]
            q_file = r_file.with_name(f"{req_id}.question.txt")

            if q_file.exists():
                try:
                    q_text = q_file.read_text(encoding="utf-8").strip()
                    r_text = r_file.read_text(encoding="utf-8").strip()

                    mtime = r_file.stat().st_mtime
                    ts = datetime.utcfromtimestamp(mtime).isoformat() + "Z"

                    metadata = {
                        "source": "oneiros_human_history",
                        "request_id": req_id,
                        "question": q_text
                    }

                    self._process_and_store(
                        content=r_text,
                        prompt_type="human_response",
                        timestamp=ts,
                        metadata=metadata
                    )
                    count += 1
                except Exception:
                    continue

        return count

    def ingest_claude_export(self, path: str | Path) -> dict:
        """
        Import reaction pairs from a Claude.ai data export.
        """
        import time
        from datetime import datetime
        from shadow_link.live import is_real_reply

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Export not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            convs = json.load(f)

        existing: set[tuple[str, str]] = set()
        for r in self.db.all_prompts():
            try:
                m = json.loads(r.get("metadata") or "{}")
            except Exception:
                m = {}
            existing.add(((m.get("agent_output") or "")[:1000], r["text"]))

        def asst_text(msg: dict) -> str:
            return "\n".join(
                b.get("text", "") for b in (msg.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()

        def human_text(msg: dict) -> str:
            if msg.get("text"):
                return msg["text"].strip()
            return "\n".join(
                b.get("text", "") for b in (msg.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()

        def epoch(s) -> float:
            try:
                return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
            except Exception:
                return time.time()

        now = time.time()
        items: list[dict] = []
        seen: set[tuple[str, str]] = set()
        skipped = 0

        for c in convs:
            name = c.get("name", "")
            last_situation = None
            for msg in c.get("chat_messages", []):
                sender = msg.get("sender")
                if sender == "assistant":
                    last_situation = asst_text(msg)
                elif sender == "human":
                    reply = human_text(msg)
                    if last_situation and reply and is_real_reply(reply):
                        key = (last_situation[:1000], reply)
                        if key in seen or key in existing:
                            skipped += 1
                        else:
                            seen.add(key)
                            buckets = classify_multi(reply)
                            items.append({
                                "text": reply,
                                "bucket": buckets[0],
                                "weight": 1.0,
                                "created_at": epoch(msg.get("created_at")),
                                "triggered_at": now,
                                "metadata": {
                                    "buckets": buckets,
                                    "source": "claude_export",
                                    "agent_output": last_situation[:1000],
                                    "conversation": name,
                                },
                            })
                    last_situation = None

        imported = self.db.bulk_add_reactions(items)
        return {"imported": imported, "skipped": skipped, "candidates": len(seen) + skipped}

    def ingest_interaction(self, question: str, response: str, request_id: str) -> int:
        """Ingests a single live human interaction (question/response pair)."""
        timestamp = datetime.utcnow().isoformat() + "Z"
        metadata = {
            "source": "oneiros_live_ingest",
            "request_id": request_id,
            "question": question,
            "agent_output": question[:1000],
        }
        return self._process_and_store(
            content=response,
            prompt_type="human_response",
            timestamp=timestamp,
            metadata=metadata,
            unique=True,   # live captures can arrive from the hook, watcher and proxy at once
        )
