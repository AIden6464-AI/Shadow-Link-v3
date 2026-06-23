"""
shadow_link/proxy.py

Integrated mitmproxy addon for Shadow Link v3.
Optimizes Anthropic /v1/messages payloads for prompt-cache efficiency,
injects learned reminders in-flight via localhost:5000 API, and passively
ingests conversation pairs into the database on successful response.
"""

from __future__ import annotations

import json
import logging
import os
import sys
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
SHADOW_LINK_INJECT = os.getenv("SHADOW_LINK_INJECT", "true").lower() in ("true", "1")
SHADOW_LINK_LEARN = os.getenv("SHADOW_LINK_LEARN", "true").lower() in ("true", "1")
SHADOW_LINK_INJECT_SYNTHESIZED = os.getenv(
    "SHADOW_LINK_INJECT_SYNTHESIZED", "true"
).lower() in ("true", "1")

try:
    SHADOW_LINK_NUDGE_BUDGET = float(os.getenv("SHADOW_LINK_NUDGE_BUDGET", "4.0"))
except ValueError:
    SHADOW_LINK_NUDGE_BUDGET = 4.0
try:
    SHADOW_LINK_NUDGE_TIMEOUT = float(os.getenv("SHADOW_LINK_NUDGE_TIMEOUT", "5.0"))
except ValueError:
    SHADOW_LINK_NUDGE_TIMEOUT = 5.0

SHADOW_LINK_WATCH_PUSH = os.getenv(
    "SHADOW_LINK_WATCH_PUSH", "true"
).lower() in ("true", "1")

from shadow_link.proxy_runtime import get_audit_dir

AUDIT_DIR = get_audit_dir()
AUDIT_DIR.mkdir(parents=True, exist_ok=True)

CLAUDE_CODE_UA_MARKER = "claude-cli"


def get_shadow_link_nudge(context_prompt: str) -> str | None:
    """Query the local daemon for behavioral nudges based on context.

    When SHADOW_LINK_INJECT_SYNTHESIZED is on, the daemon also runs the local
    LLM to draft the operator's likely reply. The system-reminder leads with
    that ghost; the retrieved snippets follow as grounding evidence.
    """
    url = f"{SHADOW_LINK_URL}/nudge"
    payload = {
        "context": context_prompt,
        "synthesize": SHADOW_LINK_INJECT_SYNTHESIZED,
        "lm_timeout_seconds": SHADOW_LINK_NUDGE_BUDGET,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=SHADOW_LINK_NUDGE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            nudges = data.get("nudges", [])
            ghost = data.get("ghost") or None

            grounding_lines = []
            for n in nudges:
                score = n.get("score", 0.0)
                prompt_data = n.get("prompt", {})
                bucket = prompt_data.get("bucket", "request")
                text = prompt_data.get("content", "")

                tier = "high" if score >= 0.55 else "medium" if score >= 0.30 else "low"
                if tier != "low" and text:
                    grounding_lines.append(f"- ({tier}, {bucket}) {text}")

            ghost_text = (ghost or {}).get("text", "").strip() if ghost else ""
            ghost_source = (ghost or {}).get("source")

            if ghost_text and ghost_source == "generated":
                header = (
                    "Shadow Link - operator's likely reply (synthesized in operator's voice):\n"
                    f"{ghost_text}"
                )
                if grounding_lines:
                    header += "\n\nGrounded in past reactions:\n" + "\n".join(grounding_lines)
                return header

            if grounding_lines:
                return (
                    "Shadow Link - based on past reactions, the operator tends to say:\n"
                    + "\n".join(grounding_lines)
                )
    except Exception as e:
        logger.debug("Failed to query Shadow Link nudges: %s", e)
    return None


def optimize_payload(payload: dict, enable_pruning: bool = False) -> dict:
    """
    Strips cache_control from static blocks and hoists dynamic saboteurs
    (Today's date, billing headers, and system reminders) to the trailing user message.
    """
    if not isinstance(payload, dict) or "messages" not in payload:
        return payload

    if enable_pruning and "tools" in payload:
        bloat_signatures = [
            "mcp__claude_ai_Gmail",
            "mcp__claude_ai_Google_Calendar",
            "mcp__claude_ai_Google_Drive",
            "NotebookEdit",
            "PowerShell",
            "RemoteTrigger",
            "CronCreate",
        ]
        payload["tools"] = [
            tool for tool in payload["tools"]
            if not any(
                sig in tool.get("function", {}).get("name", tool.get("name", ""))
                for sig in bloat_signatures
            )
        ]

    saboteurs = []

    # 1. Clean system prompts
    if "system" in payload:
        static_system = []
        system_blocks = (
            payload["system"]
            if isinstance(payload["system"], list)
            else [{"type": "text", "text": payload["system"]}]
        )

        for block in system_blocks:
            text_content = block.get("text", "") if isinstance(block, dict) else block
            if "x-anthropic-billing-header" in text_content or "Today's date is" in text_content:
                saboteurs.append(text_content)
            else:
                static_system.append(block)

        payload["system"] = static_system

    # 2. Extract reminders from existing messages
    clean_messages = []
    for msg in payload.get("messages", []):
        content = msg.get("content", "")

        if isinstance(content, str):
            if "<system-reminder>" in content:
                saboteurs.append(content)
                continue
        elif isinstance(content, list):
            clean_content = []
            for block in content:
                if block.get("type") == "text" and "<system-reminder>" in block.get("text", ""):
                    saboteurs.append(block.get("text", ""))
                else:
                    clean_content.append(block)

            if not clean_content:
                continue
            msg["content"] = clean_content

        clean_messages.append(msg)

    payload["messages"] = clean_messages

    # 3. Retrieve and inject dynamic Shadow Link nudges
    if SHADOW_LINK_INJECT and payload["messages"]:
        # Find the last user input text
        last_user_prompt = ""
        for msg in reversed(payload["messages"]):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    last_user_prompt = content
                elif isinstance(content, list):
                    last_user_prompt = "\n".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                break
        
        if last_user_prompt:
            nudge = get_shadow_link_nudge(last_user_prompt)
            if nudge:
                saboteurs.append(f"<system-reminder>\n{nudge}\n</system-reminder>")

    # 4. Append all collected saboteurs/reminders to the trailing user message
    if saboteurs and payload["messages"]:
        final_message = payload["messages"][-1]
        saboteur_text = "\n\n".join(saboteurs)

        if isinstance(final_message.get("content"), str):
            final_message["content"] = f"{saboteur_text}\n\n{final_message['content']}"
        elif isinstance(final_message.get("content"), list):
            insert_idx = 0
            for block in final_message["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    insert_idx += 1
                else:
                    break

            final_message["content"].insert(
                insert_idx, {"type": "text", "text": saboteur_text}
            )

    return payload


def _is_claude_code(flow: http.HTTPFlow) -> bool:
    ua = flow.request.headers.get("user-agent", "").lower()
    return CLAUDE_CODE_UA_MARKER in ua


def _is_anthropic_messages(flow: http.HTTPFlow) -> bool:
    if not flow.request:
        return False
    if flow.request.method != "POST":
        return False
    if "/v1/messages" not in flow.request.path:
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


def request(flow: http.HTTPFlow) -> None:
    if not _is_anthropic_messages(flow):
        return

    try:
        body_str = flow.request.get_text()
        if not body_str:
            return

        original_bytes = len(body_str.encode("utf-8"))
        payload = json.loads(body_str)
        optimized = optimize_payload(payload)
        new_body = json.dumps(optimized, ensure_ascii=False)
        flow.request.set_text(new_body)
        optimized_bytes = len(new_body.encode("utf-8"))

        _log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phase": "request",
            "url": flow.request.pretty_url,
            "method": flow.request.method,
            "user_agent": flow.request.headers.get("user-agent", ""),
            "optimized": True,
            "original_bytes": original_bytes,
            "optimized_bytes": optimized_bytes,
        })
        logger.info(
            "OPTIMIZED Anthropic request: %d -> %d bytes",
            original_bytes,
            optimized_bytes,
        )
    except Exception as e:
        logger.error("optimize_payload failed: %s", e)


def response(flow: http.HTTPFlow) -> None:
    if not _is_anthropic_messages(flow):
        return
    if not flow.response or not flow.response.text:
        return

    usage = None
    claude_reply_text = ""
    try:
        data = json.loads(flow.response.text)
        if isinstance(data, dict):
            if "usage" in data:
                usage = data["usage"]
            content = data.get("content")
            if isinstance(content, list):
                claude_reply_text = "\n".join(
                    block.get("text", "") for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ).strip()
    except Exception:
        pass

    _log({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phase": "response",
        "url": flow.request.pretty_url,
        "status_code": flow.response.status_code,
        "usage": usage,
    })

    # Push Claude's reply to the daemon's watch stream so /claude-shadow in the
    # TUI surfaces it without waiting on the transcript JSONL flush. Fire-and-
    # forget: never block the response path.
    if (
        SHADOW_LINK_WATCH_PUSH
        and flow.response.status_code == 200
        and claude_reply_text
    ):
        try:
            push_req = urllib.request.Request(
                f"{SHADOW_LINK_URL}/watch/proxy_event",
                data=json.dumps({"claude_reply": claude_reply_text}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(push_req, timeout=0.5)
            except Exception:
                pass
        except Exception as e:
            logger.debug("watch push failed to construct: %s", e)

    # Ingest captured prompt-reply pairs passively
    if SHADOW_LINK_LEARN and flow.response.status_code == 200:
        try:
            req_body = json.loads(flow.request.get_text())
            res_body = json.loads(flow.response.text)
            
            messages = req_body.get("messages", [])
            if len(messages) >= 2:
                # Last message is the user prompt
                user_msg = messages[-1]
                # Second to last is the assistant output (situation)
                asst_msg = messages[-2]
                
                if user_msg.get("role") == "user" and asst_msg.get("role") == "assistant":
                    user_text = ""
                    u_content = user_msg.get("content", "")
                    if isinstance(u_content, str):
                        user_text = u_content
                    elif isinstance(u_content, list):
                        user_text = "\n".join(b.get("text", "") for b in u_content if isinstance(b, dict) and b.get("type") == "text")
                    
                    asst_text = ""
                    a_content = asst_msg.get("content", "")
                    if isinstance(a_content, str):
                        asst_text = a_content
                    elif isinstance(a_content, list):
                        asst_text = "\n".join(b.get("text", "") for b in a_content if isinstance(b, dict) and b.get("type") == "text")
                    
                    # Validate we're not recording trivia/markers
                    from shadow_link.live import is_real_reply
                    if user_text and asst_text and is_real_reply(user_text):
                        ingest_payload = {
                            "question": asst_text,
                            "response": user_text,
                            "request_id": f"proxy_{flow.id}"
                        }
                        
                        ingest_req = urllib.request.Request(
                            f"{SHADOW_LINK_URL}/ingest",
                            data=json.dumps(ingest_payload).encode("utf-8"),
                            headers={"Content-Type": "application/json"}
                        )
                        # Fire-and-forget: very low timeout
                        try:
                            urllib.request.urlopen(ingest_req, timeout=0.5)
                            logger.info("Passively ingested reaction pair from proxy flow.")
                        except Exception:
                            pass
        except Exception as e:
            logger.debug("Failed to ingest pair from proxy flow: %s", e)
