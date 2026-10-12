"""
shadow_link/decay.py

Temporal Decay Model governing the effective utility of reactions over time.
"""

from __future__ import annotations

import math
import os
import time
from enum import Enum

class DecayClass(str, Enum):
    PERMANENT = "permanent"   # Architecture/style rules — λ → 0
    TACTICAL = "tactical"     # Requests / pivots — t_half = 7 days
    EPHEMERAL = "ephemeral"   # Rejections / corrections — t_half = 3 days

# Half-life in hours for each class (env-overridable).
_HALF_LIVES: dict[DecayClass, float] = {
    DecayClass.PERMANENT: math.inf,
    DecayClass.TACTICAL: float(os.getenv("SHADOW_HALF_LIFE_TACTICAL_HOURS", str(24 * 7))),
    DecayClass.EPHEMERAL: float(os.getenv("SHADOW_HALF_LIFE_EPHEMERAL_HOURS", str(24 * 3))),
}

# Decay never takes a reply below this fraction of its base weight, so an old
# correction stays findable; feedback (reject) is what removes a reply.
WEIGHT_FLOOR_FRACTION: float = float(os.getenv("SHADOW_WEIGHT_FLOOR", "0.25"))

# Map bucket names → decay class.
BUCKET_DECAY_CLASS: dict[str, DecayClass] = {
    "reminder": DecayClass.PERMANENT,
    "request": DecayClass.TACTICAL,
    "pivot": DecayClass.TACTICAL,
    "correction": DecayClass.EPHEMERAL,
    "rejection": DecayClass.EPHEMERAL,
}

def lambda_for(decay_class: DecayClass) -> float:
    """Compute λ = ln(2) / t_half for a given decay class."""
    t_half = _HALF_LIVES[decay_class]
    if math.isinf(t_half):
        return 0.0
    return math.log(2) / t_half

def effective_weight(
    w_base: float,
    last_triggered_at: float | None,
    decay_class: DecayClass,
    now: float | None = None,
) -> float:
    """
    Compute W_effective(t) = W_base * exp(-λ * Δt).
    """
    lam = lambda_for(decay_class)
    if lam == 0.0:
        return w_base  # Permanent — no decay.

    ref = last_triggered_at if last_triggered_at is not None else time.time()
    now = now or time.time()
    delta_hours = max(0.0, (now - ref) / 3600.0)
    return w_base * max(WEIGHT_FLOOR_FRACTION, math.exp(-lam * delta_hours))

def effective_weight_for_row(row: dict, now: float | None = None) -> float:
    """
    Convenience wrapper for a database prompt row dict.
    """
    bucket = row.get("bucket", "request")
    decay_class = BUCKET_DECAY_CLASS.get(bucket, DecayClass.TACTICAL)
    ref = row.get("triggered_at") or row.get("created_at") or time.time()
    return effective_weight(row.get("weight", 1.0), ref, decay_class, now)

def half_life_hours(decay_class: DecayClass) -> float:
    """Return configured half-life in hours."""
    return _HALF_LIVES[decay_class]
