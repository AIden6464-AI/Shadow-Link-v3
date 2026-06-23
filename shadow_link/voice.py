"""
shadow_link/voice.py

Corpus-grounded voice profile for the synthesizer.

Walks the operator's reply corpus (NOT the agent_output side - replies live
in `prompts.text`, agent_output lives in `metadata`; see ingester.py:269-278
for the gate that confirms this) and emits a small statistical paragraph
describing how the operator typically talks: length band, dominant reaction
buckets, common openings, recurring phrases.

The output is fed into the synthesizer's system prompt as style grounding,
not as templates. Phrases are surfaced as hints - never as forced repetition.
"""

from __future__ import annotations

import os
import re
import statistics
import time
from collections import Counter
from typing import Any

from shadow_link.tokenizer import NEGATIVE_MODIFIERS

_TTL_SECONDS = 600.0
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "of", "to", "for", "in", "on", "at",
    "by", "with", "from", "as", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "this", "that",
    "these", "those", "it", "its", "he", "she", "they", "we", "you", "i",
    "me", "my", "your", "our", "their", "his", "her", "him", "them", "us",
    "so", "if", "then", "than", "into", "out", "up", "down", "just",
})

_NOISE_TOKENS: frozenset[str] = frozenset({
    "ps", "q", "g", "c", "d", "e", "f",
    "users", "reid", "pojects", "projects", "buckups",
    "00", "000", "0000",
})

_WORD_RE = re.compile(r"[a-z0-9']+")
_PATH_LIKE_RE = re.compile(r"[\\/:]")
_NON_ALPHA_RE = re.compile(r"[^a-z0-9\s]")

_REPLY_MAX_WORDS = 80
_REPLY_MIN_WORDS = 3
_MIN_OPENING_LEN = 2
_MIN_PHRASE_COUNT = 3
_LENGTH_BAND_CAP = 40


def _words(text: str) -> list[str]:
    """Lowercase word tokenization; mirrors the tokenizer's word rules."""
    return _WORD_RE.findall(text.lower())


def _is_reaction_shaped(text: str, words: list[str]) -> bool:
    """Reject rows that look like file paths, code dumps, or pasted transcripts."""
    if not words:
        return False
    n = len(words)
    if n < _REPLY_MIN_WORDS or n > _REPLY_MAX_WORDS:
        return False
    if _PATH_LIKE_RE.search(text):
        return False
    # Mostly non-alpha (code/log/timestamp dump): drop.
    alpha_chars = sum(1 for c in text.lower() if c.isalpha())
    total_chars = sum(1 for c in text if not c.isspace())
    if total_chars and (alpha_chars / total_chars) < 0.65:
        return False
    return True


def _drop_stopword_bigram(a: str, b: str) -> bool:
    """Keep negation phrases; drop bigrams where both sides are stopwords."""
    if a in NEGATIVE_MODIFIERS or b in NEGATIVE_MODIFIERS:
        return False
    return a in _STOPWORDS and b in _STOPWORDS


def _is_noise_token(w: str) -> bool:
    """Single chars, pure digits, or known path/log fragments."""
    if len(w) < _MIN_OPENING_LEN:
        return True
    if w.isdigit():
        return True
    return w in _NOISE_TOKENS


def _drop_bigram(a: str, b: str) -> bool:
    """Filter pipeline for phrases - runs before counting."""
    if _is_noise_token(a) or _is_noise_token(b):
        return False if (a in NEGATIVE_MODIFIERS or b in NEGATIVE_MODIFIERS) else True
    if _drop_stopword_bigram(a, b):
        return True
    return False


def voice_profile(db) -> dict[str, Any]:
    """
    Compute the operator's voice profile from the reply corpus.

    Cached by db_path for _TTL_SECONDS. Pass any object whose `db_path`
    attribute identifies it (or rely on the underlying Database id).
    """
    cache_key = str(getattr(db, "db_path", "")) or str(id(db))
    now = time.time()
    hit = _cache.get(cache_key)
    if hit and (now - hit[0]) < _TTL_SECONDS:
        return hit[1]

    rows = db.all_prompts()
    replies: list[str] = []
    bucket_counts: Counter[str] = Counter()
    opening_counts: Counter[str] = Counter()
    bigram_counts: Counter[tuple[str, str]] = Counter()
    length_words: list[int] = []

    for row in rows:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        words = _words(text)
        if not _is_reaction_shaped(text, words):
            continue
        replies.append(text)
        length_words.append(len(words))
        bucket = row.get("bucket") or "unknown"
        bucket_counts[bucket] += 1
        if not _is_noise_token(words[0]):
            opening_counts[words[0]] += 1
        for i in range(len(words) - 1):
            a, b = words[i], words[i + 1]
            if _drop_bigram(a, b):
                continue
            bigram_counts[(a, b)] += 1

    total = len(replies)
    if total == 0:
        empty = {
            "total": 0, "avg_words": 0, "length_band": "unknown",
            "top_buckets": [], "top_openings": [], "signature_phrases": [],
        }
        _cache[cache_key] = (now, empty)
        return empty

    median_words = int(statistics.median(length_words))
    sorted_lens = sorted(length_words)
    p25 = sorted_lens[int(len(sorted_lens) * 0.25)]
    p75 = min(sorted_lens[int(len(sorted_lens) * 0.75)], _LENGTH_BAND_CAP)
    length_band = f"{p25}-{p75} words" if p25 != p75 else f"~{p25} words"

    top_buckets = [
        {"bucket": b, "count": c, "pct": round(100.0 * c / total, 1)}
        for b, c in bucket_counts.most_common(3)
    ]
    top_openings = [
        {"word": w, "count": c}
        for w, c in opening_counts.most_common(8)
    ]
    signature_phrases = [
        {"phrase": f"{a} {b}", "count": c}
        for (a, b), c in bigram_counts.most_common(40)
        if c >= _MIN_PHRASE_COUNT
    ][:6]

    profile = {
        "total": total,
        "avg_words": median_words,
        "length_band": length_band,
        "top_buckets": top_buckets,
        "top_openings": top_openings,
        "signature_phrases": signature_phrases,
    }
    _cache[cache_key] = (now, profile)
    return profile


def render_voice_block(profile: dict[str, Any]) -> str:
    """
    Format a voice profile as a system-prompt paragraph.

    Phrases are surfaced as style hints, never as templates. The model is
    told to match the voice, not parrot the snippets.
    """
    if not profile or profile.get("total", 0) == 0:
        return (
            "Operator voice: (no corpus yet - fall back to a terse, direct, "
            "lowercase-leaning, blunt senior-developer voice; one short reply.)"
        )

    n = profile["total"]
    band = profile["length_band"]
    buckets_txt = ", ".join(
        f"{b['bucket']} ({b['pct']:.0f}%)" for b in profile["top_buckets"]
    )
    openings_txt = ", ".join(
        f"\"{o['word']}\"" for o in profile["top_openings"]
    )
    phrases_txt = ", ".join(
        f"\"{p['phrase']}\"" for p in profile["signature_phrases"]
    ) or "(none surfaced yet)"

    return (
        f"Operator voice (derived from {n} past reactions):\n"
        f"- Replies typically run {band}.\n"
        f"- Most common reaction types: {buckets_txt}.\n"
        f"- Common openings: {openings_txt}.\n"
        f"- Recurring phrases (style hints, not templates): {phrases_txt}.\n"
        "Match this voice. Be at least as terse. Lead with the same kind of openings. "
        "Do not parrot the phrases verbatim - they show style, not the answer."
    )


def clear_cache() -> None:
    """Drop all cached profiles. Useful for tests."""
    _cache.clear()
