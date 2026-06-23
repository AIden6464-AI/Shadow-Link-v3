"""
shadow_link/tokenizer.py

Special tokenizer that:
  - Preserves negative context modifiers (not, never, no, don't, avoid)
    to prevent context inversion during Jaccard matching.
  - Parses CellSmith markup tokens: CELL_PATCH, cell_id, REPLACE.
  - Generates character-level n-grams and word-level bi/trigrams.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

NEGATIVE_MODIFIERS: frozenset[str] = frozenset(
    {"not", "never", "no", "don't", "dont", "avoid", "without", "neither", "nor"}
)

_CELLSMITH_MARKER_RE = re.compile(
    r"#\s*%%\s*\[(?P<cell_type>[a-z_]+):(?P<cell_id>[^\]]+)\]"
)

@dataclass
class TokenSet:
    """Output of the tokenizer — everything needed for matching."""
    words: list[str] = field(default_factory=list)
    bigrams: list[tuple[str, str]] = field(default_factory=list)
    trigrams: list[tuple[str, str, str]] = field(default_factory=list)
    char_ngrams: set[str] = field(default_factory=set)
    cellsmith_cells: list[dict[str, str]] = field(default_factory=list)
    negative_flags: list[str] = field(default_factory=list)

    def as_flat_set(self) -> set[str]:
        """Flatten all tokens into a single set for Jaccard computation."""
        flat: set[str] = set(self.words)
        flat.update(f"{a}_{b}" for a, b in self.bigrams)
        flat.update(f"{a}_{b}_{c}" for a, b, c in self.trigrams)
        flat.update(self.char_ngrams)
        return flat

def tokenize(text: str, char_n: int = 3) -> TokenSet:
    """
    Tokenize *text* into a :class:`TokenSet`.
    """
    # Normalize unicode and lower-case.
    text = unicodedata.normalize("NFKC", text).lower().strip()

    # Extract CellSmith cell markers before stripping punctuation.
    cells = [m.groupdict() for m in _CELLSMITH_MARKER_RE.finditer(text)]

    # Tokenize into words — keep apostrophe contractions together.
    words = re.findall(r"[a-z0-9']+", text)

    # Flag negative modifiers.
    negative_flags = [w for w in words if w in NEGATIVE_MODIFIERS]

    # Build bigrams and trigrams.
    bigrams = [(words[i], words[i + 1]) for i in range(len(words) - 1)]
    trigrams = [(words[i], words[i + 1], words[i + 2]) for i in range(len(words) - 2)]

    # Character n-grams over the full normalized string (stripped to alphanum).
    clean = re.sub(r"[^a-z0-9 ]", "", text)
    char_ngrams = {clean[i : i + char_n] for i in range(len(clean) - char_n + 1)}

    return TokenSet(
        words=words,
        bigrams=bigrams,
        trigrams=trigrams,
        char_ngrams=char_ngrams,
        cellsmith_cells=cells,
        negative_flags=negative_flags,
    )

NEGATION_MISMATCH_PENALTY: float = 0.5

def jaccard(a: TokenSet, b: TokenSet) -> float:
    """Compute Jaccard similarity: |A ∩ B| / |A ∪ B|.

    Applies a polarity-mismatch penalty when one side carries negation
    modifiers and the other does not — "use docker" vs "do not use docker"
    share most tokens but mean opposite things, so the raw overlap is
    multiplied by NEGATION_MISMATCH_PENALTY to suppress false matches.
    """
    set_a = a.as_flat_set()
    set_b = b.as_flat_set()
    if not set_a and not set_b:
        return 1.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    base = intersection / union if union > 0 else 0.0
    if bool(a.negative_flags) != bool(b.negative_flags):
        return base * NEGATION_MISMATCH_PENALTY
    return base
