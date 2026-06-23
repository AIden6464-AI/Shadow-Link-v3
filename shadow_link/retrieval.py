"""
shadow_link/retrieval.py

Hybrid Retrieval Engine.

Dynamic α blending:
    α(q) = α_base * exp(-γ * L_q)

Short queries  → α → 1  (heavy lexical)
Long queries   → α → 0  (heavy semantic)

Per-bucket thresholds:
    θ = per-bucket threshold
"""

from __future__ import annotations

import math
import os

ALPHA_BASE: float = float(os.getenv("ALPHA_BASE", "0.4"))
GAMMA: float = 0.05   # Decay rate for query length

# Per-bucket combined-score thresholds.
BUCKET_THRESHOLDS: dict[str, float] = {
    "rejection": float(os.getenv("THRESHOLD_REJECTION", "0.75")),   # strict lexical prevention
    "correction": float(os.getenv("THRESHOLD_CORRECTION", "0.75")),
    "reminder": float(os.getenv("THRESHOLD_REMINDER", "0.55")),    # structural style alignment
    "request": float(os.getenv("THRESHOLD_REQUEST", "0.55")),
    "pivot": float(os.getenv("THRESHOLD_PIVOT", "0.45")),       # contextual match
}
GLOBAL_THRESHOLD: float = float(os.getenv("GLOBAL_THRESHOLD", "0.60"))

def dynamic_alpha(query_tokens: list[str], alpha_base: float = ALPHA_BASE) -> float:
    """
    Compute query-length-aware α blending weight.
    """
    l_q = len(query_tokens)
    return alpha_base * math.exp(-GAMMA * l_q)

def combined_score(
    jaccard_sim: float,
    cosine_sim: float,
    alpha: float,
) -> float:
    """Compute S_combined = α * S_Jaccard + (1-α) * S_Cosine."""
    return alpha * jaccard_sim + (1.0 - alpha) * cosine_sim

def threshold_for_bucket(bucket: str) -> float:
    """Return the per-bucket θ threshold for a given memory bucket type."""
    env_name = f"THRESHOLD_{bucket.upper()}"
    if env_name in os.environ:
        try:
            return float(os.environ[env_name])
        except ValueError:
            pass

    global_floor = os.environ.get("GLOBAL_THRESHOLD")
    if global_floor is not None:
        try:
            return float(global_floor)
        except ValueError:
            pass

    return BUCKET_THRESHOLDS.get(bucket, GLOBAL_THRESHOLD)

def filter_by_threshold(
    candidates: list[dict],
    query_tokens: list[str],
) -> list[dict]:
    """
    Apply dynamic α and per-bucket thresholds to a candidate list.
    """
    alpha = dynamic_alpha(query_tokens)
    results: list[dict] = []

    for c in candidates:
        score = combined_score(c["jaccard_sim"], c.get("cosine_sim", 0.0), alpha)
        threshold = threshold_for_bucket(c.get("bucket", "request"))
        if score >= threshold:
            results.append({**c, "combined_score": score, "alpha_used": alpha})

    results.sort(key=lambda x: x["combined_score"], reverse=True)
    return results
