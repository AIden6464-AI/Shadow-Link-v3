"""
shadow_link/engine.py

Dual-Lens Hybrid Matcher Engine.
- Lens 1: Jaccard Lexical Similarity
- Lens 2: Cosine Semantic Similarity (OpenAI-compatible local server)
"""

from __future__ import annotations

import json
import logging
import math
import os
import urllib.error
import urllib.request
from typing import Sequence

from shadow_link.decay import effective_weight_for_row
from shadow_link.database import Database
from shadow_link.retrieval import dynamic_alpha, combined_score, threshold_for_bucket
from shadow_link.tokenizer import TokenSet, jaccard, tokenize

logger = logging.getLogger(__name__)

EMBED_SERVER_URL: str = os.getenv(
    "EMBED_SERVER_URL", "http://localhost:1235/v1/embeddings"
)
EMBED_MODEL: str = os.getenv("EMBED_MODEL", "")  # blank => auto-select from /v1/models
EMBED_TIMEOUT: float = float(os.getenv("EMBED_TIMEOUT_SECONDS", "5.0"))

_resolved_embed_model = None  # cached auto-selected embedding model id

def _embed_model() -> str:
    """Resolve the embedding model id."""
    global _resolved_embed_model
    if EMBED_MODEL:
        return EMBED_MODEL
    if _resolved_embed_model:
        return _resolved_embed_model
    try:
        base = EMBED_SERVER_URL.rsplit("/embeddings", 1)[0]  # -> .../v1
        with urllib.request.urlopen(base + "/models", timeout=3) as resp:
            ids = [m.get("id", "") for m in json.loads(resp.read()).get("data", [])]
        for i in ids:
            if "embed" in i.lower():
                _resolved_embed_model = i
                return i
    except Exception as exc:
        logger.debug("Embed model auto-select failed (%s).", exc)
    return "sentence-transformers/all-MiniLM-L6-v2"

def _embed(text: str) -> list[float] | None:
    """Request an embedding vector from the local embed server."""
    payload = json.dumps({"input": text, "model": _embed_model()}).encode()
    req = urllib.request.Request(
        EMBED_SERVER_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
            data = json.loads(resp.read())
            return data["data"][0]["embedding"]
    except (urllib.error.URLError, KeyError, json.JSONDecodeError, OSError) as exc:
        logger.debug("Embed server unavailable (%s); falling back to Jaccard-only.", exc)
        return None

def get_embedding(
    text: str,
    provider: str,
    url: str,
    model: str
) -> list[float] | None:
    """Wrapper to maintain compatibility with legacy ingester calls."""
    if provider == "none" or not url:
        return None
    payload = json.dumps({"input": text, "model": model}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
            data = json.loads(resp.read())
            if "embedding" in data:
                return data["embedding"]
            elif "data" in data and len(data["data"]) > 0:
                return data["data"][0]["embedding"]
            return None
    except Exception as exc:
        logger.debug("Failed to get embedding: %s", exc)
        return None

def _cosine(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two dense vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)

class MatchResult:
    """Single retrieval result from the engine."""

    __slots__ = (
        "prompt_id", "text", "bucket", "base_weight", "effective_weight",
        "jaccard_sim", "cosine_sim", "combined_score", "alpha_used",
    )

    def __init__(
        self,
        *,
        prompt_id: int,
        text: str,
        bucket: str,
        base_weight: float,
        effective_weight: float,
        jaccard_sim: float,
        cosine_sim: float,
        combined_score: float,
        alpha_used: float,
    ) -> None:
        self.prompt_id = prompt_id
        self.text = text
        self.bucket = bucket
        self.base_weight = base_weight
        self.effective_weight = effective_weight
        self.jaccard_sim = jaccard_sim
        self.cosine_sim = cosine_sim
        self.combined_score = combined_score
        self.alpha_used = alpha_used

    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}

class Engine:
    """Dual-Lens Hybrid Matcher."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def _row_embedding(self, row: dict) -> list[float] | None:
        """Return a row's embedding, using cached or computing fresh."""
        model = _embed_model()
        cached = self.db.get_embedding(row["id"], "embed_vec", model)
        if cached is not None:
            return cached
        vec = _embed(row["text"])
        if vec is not None:
            self.db.cache_embedding(row["id"], vec, model)
        return vec

    def _ctx_embedding(self, row: dict, ctx: str,
                       embed_if_missing: bool = True) -> list[float] | None:
        """Cached embedding of the SITUATION (agent_output)."""
        model = _embed_model()
        cached = self.db.get_embedding(row["id"], "embed_ctx_vec", model)
        if cached is not None:
            return cached
        if not embed_if_missing:
            return None
        vec = _embed(ctx)
        if vec is not None:
            self.db.cache_embedding(row["id"], vec, model, field="embed_ctx_vec")
        return vec

    def predict_replies(self, claude_output: str, top_k: int = 4,
                        candidate_k: int = 20) -> list[MatchResult]:
        """Reaction-pair prediction."""
        q_ts = tokenize(claude_output)

        prelim: list[tuple[float, dict, str]] = []
        for row in self.db.all_prompts():
            try:
                meta = json.loads(row.get("metadata") or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            ctx = (meta.get("agent_output") or meta.get("question") or "").strip()
            if not ctx:
                continue
            prelim.append((jaccard(q_ts, tokenize(ctx)), row, ctx))
        if not prelim:
            return []
        prelim.sort(key=lambda t: t[0], reverse=True)
        candidates = prelim[: max(candidate_k, top_k)]

        use_q_embed = os.getenv("SHADOW_QUERY_EMBED", "0") not in ("0", "", "false", "False")
        q_embed = _embed(claude_output) if use_q_embed else None
        alpha = dynamic_alpha(q_ts.words) if q_embed is not None else 1.0
        warm_budget = int(os.getenv("EMBED_WARM_PER_CALL", "2"))

        results: list[MatchResult] = []
        for j_sim, row, ctx in candidates:
            c_sim = 0.0
            if q_embed is not None:
                ctx_embed = self._ctx_embedding(row, ctx, embed_if_missing=False)
                if ctx_embed is None and warm_budget > 0:
                    ctx_embed = self._ctx_embedding(row, ctx, embed_if_missing=True)
                    warm_budget -= 1
                if ctx_embed is not None and len(ctx_embed) == len(q_embed):
                    c_sim = _cosine(q_embed, ctx_embed)
            score = combined_score(j_sim, c_sim, alpha)
            results.append(
                MatchResult(
                    prompt_id=row["id"],
                    text=row["text"],
                    bucket=row["bucket"],
                    base_weight=row["weight"],
                    effective_weight=effective_weight_for_row(row),
                    jaccard_sim=j_sim,
                    cosine_sim=c_sim,
                    combined_score=score,
                    alpha_used=alpha,
                )
            )
        results.sort(key=lambda r: r.effective_weight * r.combined_score, reverse=True)
        return results[:top_k]

    def match(
        self,
        query: str,
        top_k: int = 5,
        threshold: float | None = None,
        buckets: Sequence[str] | None = None,
    ) -> list[MatchResult]:
        """Run Dual-Lens matching against all stored prompts."""
        query_ts = tokenize(query)
        query_embed = _embed(query)
        alpha = dynamic_alpha(query_ts.words) if query_embed is not None else 1.0

        rows = self.db.all_prompts()
        if buckets:
            wanted = set(buckets)

            def _in_wanted(r: dict) -> bool:
                if r["bucket"] in wanted:
                    return True
                try:
                    extra = json.loads(r.get("metadata") or "{}").get("buckets", [])
                except (json.JSONDecodeError, TypeError):
                    extra = []
                return any(b in wanted for b in extra)

            rows = [r for r in rows if _in_wanted(r)]

        results: list[MatchResult] = []
        for row in rows:
            row_ts = tokenize(row["text"])
            j_sim = jaccard(query_ts, row_ts)

            c_sim = 0.0
            if query_embed is not None:
                row_embed = self._row_embedding(row)
                if row_embed is not None and len(row_embed) == len(query_embed):
                    c_sim = _cosine(query_embed, row_embed)

            score = combined_score(j_sim, c_sim, alpha)
            floor = threshold if threshold is not None else threshold_for_bucket(row["bucket"])
            if score < floor:
                continue

            eff_w = effective_weight_for_row(row)
            results.append(
                MatchResult(
                    prompt_id=row["id"],
                    text=row["text"],
                    bucket=row["bucket"],
                    base_weight=row["weight"],
                    effective_weight=eff_w,
                    jaccard_sim=j_sim,
                    cosine_sim=c_sim,
                    combined_score=score,
                    alpha_used=alpha,
                )
            )

        results.sort(
            key=lambda r: r.effective_weight * r.combined_score, reverse=True
        )
        return results[:top_k]
