"""
Associative Context Activation (ACA).

Something happens → ask "what does this activate?" → spread through the
cognitive graph → compete, inhibit, converge to the smallest strong context
→ fire bounded decision neurons (Jev-shaped) → propagate → activate again.
"""

from shadow_link.aca.activation import Budget
from shadow_link.aca.brain import ACA, WorkingBrain
from shadow_link.aca.neurons import (
    Choice, FiringPolicy, HeuristicBackend, JevBackend, Noul, Score,
)
from shadow_link.aca.store import CognitiveStore

__all__ = [
    "ACA", "Budget", "Choice", "CognitiveStore", "FiringPolicy", "HeuristicBackend",
    "JevBackend", "Noul", "Score", "WorkingBrain", "open_aca",
]


def open_aca(db_path=None) -> ACA:
    """ACA over the configured Shadow Link database."""
    if db_path is None:
        from shadow_link.config import Config
        db_path = Config().db_path
    return ACA(CognitiveStore(db_path))
