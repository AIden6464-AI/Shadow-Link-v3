"""
shadow_link/generate.py

LM mode: instead of returning a verbatim past reply (retrieval), draft a NEW
reply in the operator's voice using a local chat LLM, grounded in their most
similar past reactions (RAG).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

logger = logging.getLogger("shadow_link.generate")

CHAT_SERVER_URL = os.getenv("CHAT_SERVER_URL", "http://localhost:1234/v1/chat/completions")
CHAT_MODEL = os.getenv("CHAT_MODEL", "local-model")
CHAT_TIMEOUT = float(os.getenv("CHAT_TIMEOUT_SECONDS", "25"))
LM_MODE = os.getenv("SHADOW_LM_MODE", "1") not in ("0", "", "false", "False")

GHOST_MAX_TOKENS = int(os.getenv("SHADOW_GHOST_MAX_TOKENS", "96"))
GHOST_EXAMPLES = int(os.getenv("SHADOW_GHOST_EXAMPLES", "8"))
GHOST_SITUATION_CHARS = int(os.getenv("SHADOW_GHOST_SITUATION_CHARS", "600"))
GHOST_REPLY_CHARS = int(os.getenv("SHADOW_GHOST_REPLY_CHARS", "500"))

_cooldown_until = 0.0
_COOLDOWN_SECONDS = 30.0
_resolved_model = None

def lm_enabled() -> bool:
    return LM_MODE

def _chat_model() -> str:
    """Resolve the chat model id."""
    global _resolved_model
    env = os.getenv("CHAT_MODEL")
    if env and env != "local-model":
        return env
    if _resolved_model:
        return _resolved_model
    try:
        base = CHAT_SERVER_URL.rsplit("/chat/completions", 1)[0]
        with urllib.request.urlopen(base + "/models", timeout=3) as resp:
            ids = [m.get("id", "") for m in json.loads(resp.read()).get("data", [])]
        ids = [i for i in ids if i and "embed" not in i.lower()]
        skip = ("r1", "reasoning", "cot", "think", "deepseek", "distill")
        chat = [i for i in ids if not any(s in i.lower() for s in skip)]
        pool = chat or ids
        prefs = ("coder", "qwopus", "qwen", "opus", "32b", "27b", "70b",
                 "instruct", "8b", "7b", "3b", "mini", "1b")
        for pref in prefs:
            for i in pool:
                if pref in i.lower():
                    _resolved_model = i
                    return i
        if pool:
            _resolved_model = pool[0]
            return pool[0]
    except Exception as exc:
        logger.debug("Model auto-select failed (%s); using default.", exc)
    return CHAT_MODEL

def _build_messages(claude_output: str, pairs: list[dict],
                    voice_block: str | None = None) -> list[dict]:
    """Build the OpenAI `messages` array."""
    examples = []
    for p in pairs[:GHOST_EXAMPLES]:
        sit = (p.get("situation") or "").strip().replace("\n", " ")
        rep = (p.get("reply") or "").strip().replace("\n", " ")
        if sit and rep:
            examples.append(
                f"AGENT: {sit[:GHOST_SITUATION_CHARS]}\n"
                f"OPERATOR: {rep[:GHOST_REPLY_CHARS]}"
            )
    examples_block = "\n\n".join(examples) if examples else "(no prior examples yet)"

    voice_section = voice_block or (
        "Operator voice: terse, direct, lowercase-leaning, blunt; "
        "often a correction, rejection, or next-step request; no pleasantries."
    )

    system = (
        "You are Shadow Link, predicting how ONE specific operator will reply "
        "to an AI coding agent working on their behalf.\n\n"
        f"{voice_section}\n\n"
        "SYNTHESIZE A NEW reply that fits THIS specific situation. The examples "
        "below show style, not answers - do not copy any example verbatim. "
        "Match the voice. Be at least as terse as the operator typically is.\n\n"
        "Output ONLY the reply text: no preamble, no quotes, no explanation. "
        "One short message."
    )
    user = (
        f"How this operator has reacted to similar situations:\n\n{examples_block}\n\n"
        f"The agent just said:\n{claude_output[:1500]}\n\n"
        f"The operator's likely reply:"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

def _parse_output(data: dict) -> str | None:
    """Extract text from the custom API or OpenAI shapes."""
    out = data.get("output")
    if isinstance(out, list) and out:
        c = out[0].get("content")
        if isinstance(c, str) and c.strip():
            return c.strip()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        return None

def _resolve_voice_block(db) -> str | None:
    """Compute the voice block for `db` (None if unavailable / empty corpus)."""
    if db is None:
        return None
    try:
        from shadow_link.voice import voice_profile, render_voice_block
        return render_voice_block(voice_profile(db))
    except Exception as exc:
        logger.debug("Voice profile unavailable (%s); using fallback voice.", exc)
        return None


def generate_ghost(
    claude_output: str,
    pairs: list[dict],
    db=None,
    timeout_seconds: float | None = None,
) -> str | None:
    """
    Draft the operator's likely reply via the chat LLM.

    Parameters
    ----------
    claude_output:
        The AI output the operator is reacting to.
    pairs:
        Retrieved past reactions, in the shape `{situation, reply}`.
    db:
        Optional Database handle. If provided, a corpus-derived voice
        block replaces the generic style stereotype.
    timeout_seconds:
        Per-call wall-clock budget. Falls back to `CHAT_TIMEOUT_SECONDS`
        env / config default. No global mutation - safe under concurrency.
    """
    global _cooldown_until
    if not LM_MODE:
        return None
    if time.time() < _cooldown_until:
        return None

    voice_block = _resolve_voice_block(db)
    effective_timeout = timeout_seconds if timeout_seconds and timeout_seconds > 0 else CHAT_TIMEOUT

    payload = json.dumps({
        "model": _chat_model(),
        "messages": _build_messages(claude_output, pairs, voice_block=voice_block),
        "temperature": 0.6,
        "max_tokens": GHOST_MAX_TOKENS,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        CHAT_SERVER_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
            data = json.loads(resp.read())
        text = _parse_output(data)
        if not text:
            return None
        if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
            text = text[1:-1].strip()
        return text or None
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        logger.debug("Chat LLM unavailable (%s); cooldown %ss.", exc, _COOLDOWN_SECONDS)
        _cooldown_until = time.time() + _COOLDOWN_SECONDS
        return None
