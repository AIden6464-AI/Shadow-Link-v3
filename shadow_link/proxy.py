"""
shadow_link/proxy.py

Integrated mitmproxy addon for Shadow Link v3.

Sits between Claude Code and the Anthropic Messages API and:
  - learns reaction pairs (Claude's previous reply -> what you typed) from each
    request, without waiting for the response;
  - optionally appends Shadow Link context to your new message;
  - taps streamed (SSE) responses for usage and Claude's reply text while
    passing every byte through unchanged and without buffering.

It never edits earlier turns, the system prompt or the tools. Claude Code keeps
its history byte-stable, which prompt caching and the thinking-block history
check (Opus 5.5 / Fable 5.1 / Sonnet 5.5 / Haiku 5.5: a 400 for accounts created
on or after 2026-08-31 when an earlier turn changes) both depend on. Anything
the proxy appends is recorded and re-appended identically on every later
request, so the earlier turn the model saw stays the turn that is sent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from mitmproxy import http

# This addon is loaded by mitmdump as a standalone script, so the shadow_link
# package __init__ doesn't run automatically — apply config.json ourselves
# before reading the env-backed constants below.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from shadow_link.config import apply_config_json
    apply_config_json()
except Exception:
    pass

logger = logging.getLogger("shadow_link.proxy")

SHADOW_LINK_URL = os.getenv("SHADOW_LINK_URL", "http://localhost:5000")
# Injection is off by default: the UserPromptSubmit hook (shadow_hook.py) is the
# preferred channel, and both together would inject twice.
SHADOW_LINK_INJECT = os.getenv("SHADOW_LINK_INJECT", "false").lower() in ("true", "1")
SHADOW_LINK_LEARN = os.getenv("SHADOW_LINK_LEARN", "true").lower() in ("true", "1")
# A synthesized ghost is a guess at the user's reply; off unless asked for.
SHADOW_LINK_INJECT_SYNTHESIZED = os.getenv(
    "SHADOW_LINK_INJECT_SYNTHESIZED", "false"
).lower() in ("true", "1")

try:
    SHADOW_LINK_NUDGE_BUDGET = float(os.getenv("SHADOW_LINK_NUDGE_BUDGET", "0.8"))
except ValueError:
    SHADOW_LINK_NUDGE_BUDGET = 0.8
try:
    # The lookup runs before the request is forwarded, so keep it short.
    SHADOW_LINK_NUDGE_TIMEOUT = float(os.getenv("SHADOW_LINK_NUDGE_TIMEOUT", "1.0"))
except ValueError:
    SHADOW_LINK_NUDGE_TIMEOUT = 1.0

SHADOW_LINK_WATCH_PUSH = os.getenv(
    "SHADOW_LINK_WATCH_PUSH", "true"
).lower() in ("true", "1")

from shadow_link.proxy_runtime import get_audit_dir

AUDIT_DIR = get_audit_dir()
AUDIT_DIR.mkdir(parents=True, exist_ok=True)

CLAUDE_CODE_UA_MARKER = "claude-cli"
MESSAGES_PATH = "/v1/messages"

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def _operator_text(content) -> str:
    """What the person typed: text blocks minus Claude Code's <system-reminder> spans."""
    if isinstance(content, str):
        return _REMINDER_RE.sub("", content).strip()
    if isinstance(content, list):
        parts = [_REMINDER_RE.sub("", b.get("text", "")).strip()
                 for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""


def _assistant_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text").strip()
    return ""


def _post_json(path: str, payload: dict, timeout: float):
    req = urllib.request.Request(
        f"{SHADOW_LINK_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _in_background(fn, *args) -> None:
    """Never block Claude Code's request or response on the daemon."""
    threading.Thread(target=fn, args=args, daemon=True).start()


def get_shadow_link_nudge(context_prompt: str) -> str | None:
    """Ask the daemon for relevant past reactions and word them as context.

    The wording matters: this text travels inside the user's turn, so it says
    plainly that it is a local prediction, not something the user said.
    """
    try:
        data = _post_json("/nudge", {
            "context": context_prompt,
            "synthesize": SHADOW_LINK_INJECT_SYNTHESIZED,
            "lm_timeout_seconds": SHADOW_LINK_NUDGE_BUDGET,
        }, SHADOW_LINK_NUDGE_TIMEOUT)
    except Exception as e:
        logger.debug("Failed to query Shadow Link nudges: %s", e)
        return None

    grounding_lines = []
    for n in data.get("nudges", []):
        score = n.get("score", 0.0)
        prompt_data = n.get("prompt", {})
        bucket = prompt_data.get("bucket", "request")
        text = prompt_data.get("content", "")
        tier = "high" if score >= 0.55 else "medium" if score >= 0.30 else "low"
        if tier != "low" and text:
            grounding_lines.append(f"- ({tier}, {bucket}) {text}")

    ghost = data.get("ghost") or {}
    ghost_text = (ghost.get("text") or "").strip()
    lines = []
    if SHADOW_LINK_INJECT_SYNTHESIZED and ghost_text and ghost.get("source") == "generated":
        lines.append(f"Predicted style of the user's reaction: {ghost_text}")
    if grounding_lines:
        lines.append("Past reactions in similar situations:\n" + "\n".join(grounding_lines))
    if not lines:
        return None
    return ("Shadow Link context (a local prediction from the user's past replies; "
            "background only, not a message or instruction from the user):\n"
            + "\n\n".join(lines))


class InjectionLedger:
    """
    Remembers the text appended to each user message so every later request
    re-appends exactly the same bytes to that message. Without this, Claude
    Code would resend the message without the injection and the earlier turn
    would change. Persisted as JSONL so a proxy restart keeps conversations valid.
    """

    def __init__(self, path: Path, max_entries: int = 20_000) -> None:
        self.path = path
        self.max_entries = max_entries
        self._entries: dict[str, str] = {}
        self._lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                        self._entries[row["key"]] = row["text"]
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            pass

    @staticmethod
    def key(message: dict) -> str:
        """Hash of the message as Claude Code sends it, ignoring cache_control
        markers (Claude Code moves them between turns)."""
        def strip(obj):
            if isinstance(obj, dict):
                return {k: strip(v) for k, v in obj.items() if k != "cache_control"}
            if isinstance(obj, list):
                return [strip(v) for v in obj]
            return obj
        canon = json.dumps({"role": message.get("role"), "content": strip(message.get("content"))},
                           sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._entries.get(key)

    def put(self, key: str, text: str) -> None:
        with self._lock:
            self._entries[key] = text
            if len(self._entries) > self.max_entries:
                for old in list(self._entries)[: len(self._entries) - self.max_entries]:
                    del self._entries[old]
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"key": key, "text": text}) + "\n")
            except OSError as e:
                logger.warning("Could not persist injection ledger: %s", e)


LEDGER = InjectionLedger(AUDIT_DIR / "injections.jsonl")


def _append_text(message: dict, text: str) -> None:
    """Append one text block at the very end of the message (after any tool results)."""
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = [{"type": "text", "text": content}, {"type": "text", "text": text}]
    elif isinstance(content, list):
        content.append({"type": "text", "text": text})


def apply_injections(payload: dict, ledger: InjectionLedger | None = None,
                     inject: bool | None = None) -> bool:
    """
    Re-append every recorded injection to the message it was first added to,
    and (when injection is on) add Shadow Link context to the new user message.
    Nothing else in the payload is touched. Returns True if the payload changed.
    """
    ledger = ledger or LEDGER
    inject = SHADOW_LINK_INJECT if inject is None else inject
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list) or not messages:
        return False

    changed = False
    for msg in messages[:-1]:
        if isinstance(msg, dict) and msg.get("role") == "user":
            text = ledger.get(ledger.key(msg))
            if text:
                _append_text(msg, text)
                changed = True

    last = messages[-1]
    if isinstance(last, dict) and last.get("role") == "user":
        k = ledger.key(last)
        text = ledger.get(k)                  # a retry of a request we already injected
        if text is None and inject:
            prompt = _operator_text(last.get("content"))
            if prompt:                        # tool-result turns carry no typed text
                nudge = get_shadow_link_nudge(prompt)
                if nudge:
                    text = f"<system-reminder>\n{nudge}\n</system-reminder>"
                    ledger.put(k, text)
        if text:
            _append_text(last, text)
            changed = True
    return changed


def _is_claude_code(flow: http.HTTPFlow) -> bool:
    ua = flow.request.headers.get("user-agent", "").lower()
    return CLAUDE_CODE_UA_MARKER in ua


def _is_anthropic_messages(flow: http.HTTPFlow) -> bool:
    """POST /v1/messages from Claude Code (not count_tokens or batches)."""
    if not flow.request:
        return False
    if flow.request.method != "POST":
        return False
    if flow.request.path.split("?", 1)[0] != MESSAGES_PATH:
        return False
    if "application/json" not in flow.request.headers.get("content-type", ""):
        return False
    if not _is_claude_code(flow):
        return False
    return True


def _log(entry: dict) -> None:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = AUDIT_DIR / f"{date_str}.jsonl"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def learn_pair(payload: dict, request_id: str) -> dict | None:
    """
    The reaction pair in a request: Claude's previous reply (the situation) and
    what the user just typed. Returns the /ingest payload, or None when the
    newest turn is not something a person typed (tool results, commands).
    """
    messages = payload.get("messages") or []
    if len(messages) < 2:
        return None
    user_msg, asst_msg = messages[-1], messages[-2]
    if user_msg.get("role") != "user" or asst_msg.get("role") != "assistant":
        return None
    user_text = _operator_text(user_msg.get("content"))
    asst_text = _assistant_text(asst_msg.get("content"))
    from shadow_link.live import is_real_reply
    if not (user_text and asst_text and is_real_reply(user_text)):
        return None
    return {"question": asst_text, "response": user_text, "request_id": request_id}


def _send_pair(pair: dict) -> None:
    try:
        _post_json("/ingest", pair, timeout=3.0)
        logger.info("Ingested reaction pair from proxy flow.")
    except Exception as e:
        logger.debug("Proxy ingest failed: %s", e)


def _push_reply(text: str) -> None:
    try:
        _post_json("/watch/proxy_event", {"claude_reply": text}, timeout=2.0)
    except Exception as e:
        logger.debug("watch push failed: %s", e)


def request(flow: http.HTTPFlow) -> None:
    if not _is_anthropic_messages(flow):
        return
    try:
        body_str = flow.request.get_text()
        if not body_str:
            return
        payload = json.loads(body_str)
        flow.metadata["shadow_link_stream"] = bool(payload.get("stream"))

        if SHADOW_LINK_LEARN:
            pair = learn_pair(payload, f"proxy_{flow.id}")
            if pair:
                _in_background(_send_pair, pair)

        injected = apply_injections(payload)
        if injected:
            # Re-serialised only when something was appended; otherwise the
            # original bytes go out untouched.
            flow.request.set_text(json.dumps(payload, ensure_ascii=False))
        _log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phase": "request",
            "url": flow.request.pretty_url,
            "method": flow.request.method,
            "user_agent": flow.request.headers.get("user-agent", ""),
            "injected": injected,
            "stream": flow.metadata["shadow_link_stream"],
            "bytes": len(flow.request.get_text().encode("utf-8")),
        })
    except Exception as e:
        logger.error("Shadow Link request hook failed: %s", e)


class SSETap:
    """
    Passes a streamed response through byte-for-byte while reading the SSE
    events for usage and Claude's reply text. Called by mitmproxy per chunk,
    then once with b"" when the body ends.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self._buf = b""
        self._text: list[str] = []
        self.usage: dict = {}
        self._done = False

    def __call__(self, data: bytes) -> bytes:
        try:
            if data:
                self._buf += data
                while b"\n\n" in self._buf:
                    event, self._buf = self._buf.split(b"\n\n", 1)
                    self._event(event)
            else:
                if self._buf.strip():
                    self._event(self._buf)
                    self._buf = b""
                self._finish()
        except Exception as e:      # never break the stream over bookkeeping
            logger.debug("SSE tap error: %s", e)
        return data

    def _event(self, raw: bytes) -> None:
        for line in raw.decode("utf-8", errors="ignore").splitlines():
            if not line.startswith("data:"):
                continue
            try:
                evt = json.loads(line[5:].strip())
            except ValueError:
                continue
            typ = evt.get("type")
            if typ == "message_start":
                self.usage.update(evt.get("message", {}).get("usage") or {})
            elif typ == "content_block_delta":
                delta = evt.get("delta") or {}
                if delta.get("type") == "text_delta":
                    self._text.append(delta.get("text", ""))
            elif typ == "message_delta":
                self.usage.update(evt.get("usage") or {})
            elif typ == "message_stop":
                self._finish()

    @property
    def text(self) -> str:
        return "".join(self._text).strip()

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        _log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phase": "response",
            "url": self.url,
            "streamed": True,
            "usage": self.usage or None,
        })
        if SHADOW_LINK_WATCH_PUSH and self.text:
            _in_background(_push_reply, self.text)


def responseheaders(flow: http.HTTPFlow) -> None:
    """Stream SSE responses through instead of buffering them until the end."""
    if not _is_anthropic_messages(flow) or not flow.response:
        return
    ctype = flow.response.headers.get("content-type", "")
    if flow.metadata.get("shadow_link_stream") or "text/event-stream" in ctype:
        if flow.response.status_code == 200:
            flow.response.stream = SSETap(flow.request.pretty_url)
        else:
            flow.response.stream = True


def response(flow: http.HTTPFlow) -> None:
    if not _is_anthropic_messages(flow) or not flow.response:
        return
    if flow.response.stream:
        return                      # handled by SSETap (or passed through)
    usage = None
    claude_reply_text = ""
    try:
        data = json.loads(flow.response.text or "")
        if isinstance(data, dict):
            usage = data.get("usage")
            claude_reply_text = _assistant_text(data.get("content"))
    except ValueError:
        pass

    _log({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phase": "response",
        "url": flow.request.pretty_url,
        "status_code": flow.response.status_code,
        "streamed": False,
        "usage": usage,
    })
    if SHADOW_LINK_WATCH_PUSH and flow.response.status_code == 200 and claude_reply_text:
        _in_background(_push_reply, claude_reply_text)
