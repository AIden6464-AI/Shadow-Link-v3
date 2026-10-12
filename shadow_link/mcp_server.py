"""
shadow_link/mcp_server.py

Model Context Protocol (MCP) server for Shadow Link.
Provides tools over stdio to learn, query, and predict reaction shapes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Add the parent directory to sys.path to enable absolute imports of the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shadow_link.config import Config
from shadow_link.database import Database
from shadow_link.engine import Engine
from shadow_link.ingester import classify_multi

from mcp.server.fastmcp import FastMCP

cfg = Config()
db = Database(cfg.db_path)
engine = Engine(db)

mcp = FastMCP("shadow-link")

PRIORITY_BUCKETS = ["rejection", "correction", "reminder", "pivot"]

def _confidence(score: float, weight: float) -> str:
    """Tier a prediction so callers know how much to trust it."""
    signal = score * max(0.0, min(weight, 2.0))
    if signal >= 0.55:
        return "high"
    if signal >= 0.30:
        return "medium"
    return "low"

def _predict(text: str, top_k: int, buckets: list[str] | None):
    """Threshold-free retrieval (floor=0) so we always return a best guess."""
    return engine.match(text, top_k=top_k, threshold=0.0, buckets=buckets)

def _row_to_dict(r) -> dict:
    return {
        "id": r.prompt_id,
        "text": r.text,
        "bucket": r.bucket,
        "score": round(r.combined_score, 3),
        "weight": round(r.effective_weight, 3),
        "jaccard": round(r.jaccard_sim, 3),
        "cosine": round(r.cosine_sim, 3),
        "confidence": _confidence(r.combined_score, r.effective_weight),
    }

@mcp.tool()
def shadow_log(your_reply: str, agent_output: str = "", goal: str = "") -> str:
    """
    Record a reaction pair — what you said back to an agent, optionally with the
    agent output that prompted it and the active goal.
    """
    buckets = classify_multi(your_reply)
    meta = {"buckets": buckets}
    if agent_output:
        meta["agent_output"] = agent_output[:1000]
    if goal:
        meta["goal"] = goal
    pid = db.add_prompt(text=your_reply, bucket=buckets[0], metadata=meta)
    return json.dumps({"logged": True, "id": pid, "buckets": buckets})

@mcp.tool()
def shadow_query(text: str, top_k: int = 5, bucket: str = "") -> str:
    """
    Strict retrieval — find stored replies similar to *text* that clear the
    per-bucket confidence threshold.
    """
    buckets = [bucket] if bucket else None
    results = engine.match(text, top_k=top_k, buckets=buckets)
    return json.dumps([_row_to_dict(r) for r in results], indent=2)

@mcp.tool()
def shadow_nudge(context: str, top_k: int = 3) -> str:
    """
    Surface behavioral nudges relevant to the current context — past rejections,
    corrections, reminders, and pivots you've expressed in similar situations.
    """
    results = _predict(context, top_k, PRIORITY_BUCKETS)
    return json.dumps([_row_to_dict(r) for r in results], indent=2)

@mcp.tool()
def shadow_draft(agent_output: str, goal: str = "", top_k: int = 3) -> str:
    """
    Ghost-draft: predict what you'd most likely SAY in response to *agent_output*.
    """
    query = f"{goal}\n{agent_output}" if goal else agent_output
    results = _predict(query, top_k, None)
    if not results:
        return json.dumps({"ghost": None, "alternates": [],
                           "note": "No reaction pairs stored yet — log some first."})
    ghost = _row_to_dict(results[0])
    alternates = [_row_to_dict(r) for r in results[1:]]
    return json.dumps({"ghost": ghost, "alternates": alternates}, indent=2)

@mcp.tool()
def shadow_accept(prompt_id: int, bump: float = 0.25) -> str:
    """
    Feedback loop — tell Shadow Link you accepted/used a predicted reply.
    """
    try:
        new_weight = db.reinforce(prompt_id, bump=bump)
    except ValueError as exc:
        return json.dumps({"reinforced": False, "error": str(exc)})
    return json.dumps({"reinforced": True, "id": prompt_id,
                       "new_weight": round(new_weight, 3)})

@mcp.tool()
def shadow_stats() -> str:
    """
    Corpus health — total stored reaction pairs and per-bucket breakdown.
    """
    return json.dumps(db.stats(), indent=2)

_aca = None

def _get_aca():
    global _aca
    if _aca is None:
        from shadow_link.aca import open_aca
        _aca = open_aca(cfg.db_path)
    return _aca

@mcp.tool()
def shadow_activate(text: str, task_id: str = "", kind: str = "prompt", goal: str = "",
                    refs: list[str] | None = None) -> str:
    """
    Associative Context Activation — ask "what does this activate?" instead of
    "what should I search for?". Seeds the cognitive graph from an event
    (prompt, error, tool result, file change, test failure, finding), spreads
    activation, suppresses noise, and returns the small converged context with
    why each item is active, plus decision signals and suggested actions.
    """
    out = _get_aca().process(text, kind=kind, task_id=task_id or None, goal=goal, refs=refs)
    out.pop("jev_state", None)
    return json.dumps(out, indent=2, default=str)

@mcp.tool()
def shadow_remember(content: str, type: str = "memory", title: str = "",
                    links: list[str] | None = None, importance: float = 0.5) -> str:
    """
    Store a typed cognitive object (decision, error, fix, requirement, finding,
    procedure, ...) and link it to existing node ids so ACA can activate it later.
    """
    aca = _get_aca()
    nid = aca.store.add_node(content, type=type, title=title, importance=importance, source="mcp")
    for other in links or []:
        aca.store.link(nid, other, "related_to", strength=0.6, source="mcp")
    return json.dumps({"id": nid, "contradicts": aca.detect_contradictions(nid)})

@mcp.tool()
def shadow_aca_feedback(task_id: str, node_id: str, useful: bool = True) -> str:
    """
    Learning loop — say whether a context item ACA pulled actually helped.
    Reinforces or weakens the synapse path that activated it.
    """
    return json.dumps(_get_aca().feedback(task_id, node_id, useful), default=str)

def run_mcp_server():
    mcp.run(transport="stdio")

if __name__ == "__main__":
    run_mcp_server()
