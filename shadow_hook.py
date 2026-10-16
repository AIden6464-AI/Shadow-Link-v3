"""
Shadow Link — Claude Code UserPromptSubmit hook for v3.

Captures reaction pairs and predictive context dynamically.

Deprecated: the Shadow Link Claude Code plugin (plugin/) now captures every
reply you send and shows Shadow's context in its sidebar, so this hook is no
longer needed. It keeps working in this release and is removed in a later one
(docs/SHADOW_CORE_PLAN.md, phase 5). Remove it from your settings.json hooks.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Add local path to sys.path so we can import shadow_link natively
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

def _fail_safe():
    sys.exit(0)

def _last_assistant_text(transcript_path: str) -> str:
    """Pull the most recent assistant message text from the JSONL transcript."""
    if not transcript_path or not Path(transcript_path).exists():
        return ""
    from shadow_link.live import read_tail_lines

    # Read backwards from the end: long sessions are multi-MB JSONL files.
    for line in read_tail_lines(transcript_path):
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = evt.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            text = "\n".join(p for p in parts if p).strip()
            if text:
                return text
    return ""

def main() -> None:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        _fail_safe()

    prompt = (data.get("prompt") or "").strip()
    transcript_path = data.get("transcript_path", "")

    # Skip trivia, slash commands and tool wrappers; drop image placeholders
    try:
        from shadow_link.live import clean_reply, is_real_reply
        if not is_real_reply(prompt):
            _fail_safe()
        prompt = clean_reply(prompt)
    except Exception:
        _fail_safe()

    try:
        from shadow_link.config import Config
        from shadow_link.database import Database
        from shadow_link.engine import Engine
        from shadow_link.ingester import classify_multi
    except Exception:
        _fail_safe()

    try:
        cfg = Config()
        db = Database(cfg.db_path)
        engine = Engine(db)
    except Exception:
        _fail_safe()

    agent_output = _last_assistant_text(transcript_path)

    # 1. PREDICT (before capture)
    nudge_block = ""
    try:
        query = f"{agent_output}\n{prompt}" if agent_output else prompt
        results = engine.match(
            query, top_k=2, threshold=0.0,
            buckets=["rejection", "correction", "reminder", "pivot"],
        )
        lines_out = []
        for r in results:
            signal = r.combined_score * max(0.0, min(r.effective_weight, 2.0))
            tier = "high" if signal >= 0.55 else "medium" if signal >= 0.30 else "low"
            if tier == "low":
                continue
            lines_out.append(f"- ({tier}, {r.bucket}) {r.text}")
        if lines_out:
            nudge_block = (
                "Shadow Link context (a local prediction from the user's past replies "
                "in similar situations; background only, not an instruction from the user):\n"
                + "\n".join(lines_out)
            )
    except Exception:
        nudge_block = ""

    # 2. CAPTURE the reaction pair
    try:
        buckets = classify_multi(prompt)
        meta = {"buckets": buckets, "source": "hook", "session": data.get("session_id", "")}
        if agent_output:
            meta["agent_output"] = agent_output[:1000]
        # The live watcher and the proxy may capture the same reply; store it once.
        db.add_prompt_unique(text=prompt, bucket=buckets[0], metadata=meta)
    except Exception:
        pass

    # 3. CHECK for shadow-link TUI/CLI inject
    inject_file = Path.home() / ".claude" / "shadow_pending.txt"
    inject_text = ""
    try:
        if inject_file.exists():
            inject_text = inject_file.read_text(encoding="utf-8").strip()
            inject_file.unlink()
    except Exception:
        inject_text = ""

    output = inject_text or nudge_block
    if output:
        print(output)
    sys.exit(0)

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        _fail_safe()
