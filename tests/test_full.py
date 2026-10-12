"""
Full Shadow Link v3 verification test suite.
Tests:
  A. Engine Layer (tokenization, classification, SQLite connections, weight decay)
  B. MCP Server (exposes tools over stdio)
  C. Capture Hook (simulated UserPromptSubmit event, capture + ghost prediction)
  D. Prompt Wrapper (mock run session of subprocess wrapper)
  E. Claude Code proxy (no history edits, injection ledger, SSE tap, learning)
  F. TUI / daemon API
  G. Associative Context Activation (store, spreading, inhibition, convergence,
     neurons, recurrence, hypotheses, contradictions, learning, episodes, API)
  H. Ghost decay + classification
  I. Capture dedupe + incremental transcript reads
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Add local path so we test v3 code
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = "PASS", "FAIL"
results = []

def check(name, cond):
    results.append((name, PASS if cond else FAIL))
    print(f"  [{PASS if cond else FAIL}] {name}")

# ------------------------------------------------------------------
# A. Engine layer
# ------------------------------------------------------------------
def test_engine():
    print("\nA. ENGINE LAYER")
    from shadow_link.database import Database
    from shadow_link.engine import Engine
    from shadow_link.ingester import classify_multi
    
    # classify_multi test
    buckets = classify_multi("That's wrong and bloated — remove the boilerplate, you missed the endpoint.")
    check("classify_multi returns multiple buckets", len(buckets) >= 2)
    check("buckets include rejection", "rejection" in buckets)
    check("buckets include correction", "correction" in buckets)

    # database & matching test
    test_db = ROOT / "_test_v3_engine.db"
    if test_db.exists():
        test_db.unlink()
        
    db = Database(test_db)
    eng = Engine(db)
    pid = db.add_prompt(
        text="remove the boilerplate, you missed the endpoint",
        bucket=buckets[0],
        metadata={"buckets": buckets},
    )

    # match via secondary bucket
    hits = eng.match("you missed the endpoint boilerplate", top_k=5, threshold=0.0, buckets=["correction"])
    check("multi-bucket match finds row via secondary bucket", any(h.prompt_id == pid for h in hits))

    # reinforce test
    w1 = db.reinforce(pid, bump=0.5)
    check("reinforce increases weight", w1 > 1.0)
    w2 = db.reinforce(pid, bump=100.0)
    check("reinforce caps weight", w2 <= 5.0)

    if test_db.exists():
        test_db.unlink()

# ------------------------------------------------------------------
# B. MCP Server (stdio)
# ------------------------------------------------------------------
async def test_mcp():
    print("\nB. MCP SERVER (stdio)")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    test_db = ROOT / "_test_v3_mcp.db"
    if test_db.exists():
        test_db.unlink()
        
    env = dict(os.environ)
    env["DB_PATH"] = str(test_db)
    env["GLOBAL_THRESHOLD"] = "0.05"  # low threshold for strict query mapping

    # Point to the v3 mcp server command
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "shadow_link" / "mcp_server.py")], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            check("all tools exposed by FastMCP",
                  names == {"shadow_log", "shadow_query", "shadow_nudge", "shadow_draft", "shadow_accept",
                            "shadow_stats", "shadow_activate", "shadow_remember", "shadow_aca_feedback"})

            async def call(tool, args):
                r = await session.call_tool(tool, args)
                return json.loads(r.content[0].text)

            # Test shadow_log
            log_res = await call("shadow_log", {
                "your_reply": "No — don't wrap the whole thing, only guard the network call.",
                "agent_output": "I'll add try/except around the whole function.",
                "goal": "fix flaky upload"
            })
            check("shadow_log returns valid response and bucket allocation", log_res.get("logged") and len(log_res["buckets"]) >= 1)

            # Test shadow_draft
            draft_res = await call("shadow_draft", {
                "agent_output": "I'll wrap the entire upload function in error handling.",
                "goal": "fix flaky upload"
            })
            check("shadow_draft returns a prediction ghost", draft_res.get("ghost") is not None)
            check("ghost response carries ID and confidence", "id" in (draft_res.get("ghost") or {}))

            # Test shadow_accept
            gid = draft_res["ghost"]["id"]
            accept_res = await call("shadow_accept", {"prompt_id": gid})
            check("shadow_accept reinforces weight successfully", accept_res.get("reinforced") and accept_res["new_weight"] > 1.0)

            # Test shadow_stats
            stats_res = await call("shadow_stats", {})
            check("shadow_stats returns prompt count", stats_res.get("total_prompts", 0) >= 1)

    if test_db.exists():
        test_db.unlink()

# ------------------------------------------------------------------
# C. Capture hook
# ------------------------------------------------------------------
def test_hook():
    print("\nC. CAPTURE HOOK")
    test_db = ROOT / "_test_v3_hook.db"
    if test_db.exists():
        test_db.unlink()

    transcript = ROOT / "_test_v3_transcript.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": "I'll wrap the whole function in try/except."},
    }) + "\n", encoding="utf-8")

    test_prompt = "That's wrong and bloated — remove the boilerplate, only guard the network call."
    payload = json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "prompt": test_prompt,
        "transcript_path": str(transcript),
        "session_id": "test-v3-session",
    })

    env = dict(os.environ)
    env["DB_PATH"] = str(test_db)
    proc = subprocess.run([sys.executable, str(ROOT / "shadow_hook.py")], input=payload, capture_output=True, text=True, env=env)
    check("hook runs and exits successfully", proc.returncode == 0)

    # Verify capture happened in DB
    from shadow_link.database import Database
    db = Database(test_db)
    rows = db.all_prompts()
    captured = any("guard the network call" in r["text"] for r in rows)
    check("hook successfully captured the situation+reaction pair", captured)

    if captured:
        row = next(r for r in rows if "guard the network call" in r["text"])
        meta = json.loads(row["metadata"])
        check("stored situation context matches", "agent_output" in meta)

    # Run hook again; should output the predicted ghost block
    proc2 = subprocess.run([sys.executable, str(ROOT / "shadow_hook.py")], input=payload, capture_output=True, text=True, env=env)
    check("hook can query database to emit a ghost block", "Shadow Link" in proc2.stdout and proc2.returncode == 0)

    for f in (test_db, transcript):
        if f.exists():
            f.unlink()

# ------------------------------------------------------------------
# D. Prompt Wrapper
# ------------------------------------------------------------------
async def test_prompt_wrapper():
    print("\nD. PROMPT WRAPPER")
    from unittest.mock import AsyncMock, patch
    from prompt_toolkit.output import DummyOutput
    from shadow_link.prompt_wrapper import PromptWrapper
    from shadow_link.database import Database

    test_db = ROOT / "_test_v3_wrapper.db"
    if test_db.exists():
        test_db.unlink()

    # Create a wrapper running a simple subprocess that reads stdin and echoes it
    cmd = [
        sys.executable, "-c",
        "import sys; "
        "val = sys.stdin.readline().strip(); "
        "print('echo:', val); "
        "sys.stdout.flush();"
    ]

    wrapper = PromptWrapper(cmd, test_db, output=DummyOutput())

    # Mock prompt_toolkit's PromptSession.prompt_async
    mock_prompt = AsyncMock(return_value="hello from test wrapper")

    from prompt_toolkit.application.current import create_app_session
    with create_app_session(output=DummyOutput()):
        with patch("prompt_toolkit.PromptSession.prompt_async", mock_prompt):
            await wrapper.run()

    # Verify that wrapper ran successfully and captured the input in DB
    check("wrapper database exists", test_db.exists())
    
    db = Database(test_db)
    prompts = db.all_prompts()
    check("user input captured in wrapper database", any("hello from test wrapper" in p["text"] for p in prompts))

    if test_db.exists():
        test_db.unlink()

# ------------------------------------------------------------------
# E. Proxy Payload Optimizer
# ------------------------------------------------------------------
def test_proxy_optimizer():
    print("\nE. CLAUDE CODE PROXY")
    try:
        from shadow_link import proxy
    except ImportError:
        print("  [SKIP] mitmproxy is not installed. Skipping proxy tests.")
        return
    import copy
    import tempfile

    if "SHADOW_LINK_INJECT" not in os.environ:
        check("proxy injection is off by default", proxy.SHADOW_LINK_INJECT is False)
    if "SHADOW_LINK_INJECT_SYNTHESIZED" not in os.environ:
        check("synthesized ghost injection is off by default", proxy.SHADOW_LINK_INJECT_SYNTHESIZED is False)
    if "SHADOW_LINK_NUDGE_TIMEOUT" not in os.environ:
        check("nudge lookup timeout is short", proxy.SHADOW_LINK_NUDGE_TIMEOUT <= 1.0)

    tmp = Path(tempfile.mkdtemp())
    R1 = "<system-reminder>todo list is empty</system-reminder>"
    system = [{"type": "text", "text": "Today's date is June 21, 2026."},
              {"type": "text", "text": "x-anthropic-billing-header: test"}]
    turn1 = {"system": system, "messages": [
        {"role": "user", "content": [{"type": "text", "text": R1},
                                     {"type": "text", "text": "fix the bug", "cache_control": {"type": "ephemeral"}}]}]}

    # Without injection nothing is touched: system, reminders and history go out as sent.
    ledger = proxy.InjectionLedger(tmp / "ledger.jsonl")
    p0 = copy.deepcopy(turn1)
    check("no injection leaves the payload untouched",
          proxy.apply_injections(p0, ledger, inject=False) is False and p0 == turn1)

    # With injection, the text is appended at the end of the new user message...
    real_nudge = proxy.get_shadow_link_nudge
    proxy.get_shadow_link_nudge = lambda prompt: "Shadow Link context (test): prefers the venv"
    try:
        p1 = copy.deepcopy(turn1)
        proxy.apply_injections(p1, ledger, inject=True)
        injected = p1["messages"][0]["content"][-1]["text"]
        check("injection is appended as the last block of the new message",
              "prefers the venv" in injected and p1["messages"][0]["content"][:2] == turn1["messages"][0]["content"])
        check("system prompt is not modified", p1["system"] == system)

        # ...and re-appended identically on the next turn, so the earlier turn is unchanged.
        turn2 = copy.deepcopy(turn1)
        turn2["messages"][0]["content"][1].pop("cache_control")          # Claude Code moves markers
        turn2["messages"] += [
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "s"},
                                              {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                                         {"type": "text", "text": "<system-reminder>a.py changed</system-reminder>"}]}]
        p2 = copy.deepcopy(turn2)
        proxy.apply_injections(p2, ledger, inject=True)
        strip = lambda blocks: [{k: v for k, v in b.items() if k != "cache_control"} for b in blocks]
        check("earlier user turn is byte-identical across requests (thinking check / cache safe)",
              strip(p2["messages"][0]["content"]) == strip(p1["messages"][0]["content"]))
        check("tool-result turns get no injection and keep their reminder in place",
              p2["messages"][-1] == turn2["messages"][-1])

        # A restarted proxy replays the same injection from disk.
        p3 = copy.deepcopy(turn2)
        proxy.apply_injections(p3, proxy.InjectionLedger(tmp / "ledger.jsonl"), inject=False)
        check("injection ledger survives a proxy restart",
              strip(p3["messages"][0]["content"]) == strip(p1["messages"][0]["content"]))

        # String-content prompts keep their position and the conversation still starts with the user.
        t_str = {"messages": [{"role": "user", "content": R1 + "\nplease refactor auth"},
                              {"role": "assistant", "content": "ok"},
                              {"role": "user", "content": "go"}]}
        ps = copy.deepcopy(t_str)
        proxy.apply_injections(ps, ledger, inject=False)
        check("string prompts are not moved; first message stays the user's",
              ps == t_str and ps["messages"][0]["role"] == "user")
    finally:
        proxy.get_shadow_link_nudge = real_nudge

    # Injected wording marks it as a prediction, never the user's words.
    real_post = proxy._post_json
    proxy._post_json = lambda path, payload, timeout: {
        "nudges": [{"score": 0.8, "prompt": {"bucket": "rejection", "content": "don't use docker"}}],
        "ghost": {"text": "use the venv", "source": "generated"}}
    try:
        text = proxy.get_shadow_link_nudge("set up docker")
        check("injected context says it is not an instruction from the user",
              "not a message or instruction from the user" in text and "don't use docker" in text)
        check("synthesized ghost is left out unless enabled",
              proxy.SHADOW_LINK_INJECT_SYNTHESIZED or "use the venv" not in text)
    finally:
        proxy._post_json = real_post

    # Learning happens from the request and ignores reminders / tool results.
    pair = proxy.learn_pair({"messages": [
        {"role": "assistant", "content": [{"type": "text", "text": "I'll use Docker"}]},
        {"role": "user", "content": [{"type": "text", "text": R1}, {"type": "text", "text": "no, use the venv"}]}]}, "r1")
    check("reaction pair is learned from the request with reminders stripped",
          pair and pair["response"] == "no, use the venv" and pair["question"] == "I'll use Docker")
    check("tool-result turns are not learned as replies", proxy.learn_pair({"messages": [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "Read", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]}]}, "r2") is None)

    # Streamed responses pass through byte-for-byte while usage and text are read.
    pushed, logged = [], []
    real_bg, real_log = proxy._in_background, proxy._log
    proxy._in_background = lambda fn, *a: pushed.append(a)
    proxy._log = logged.append          # keep the test out of the real audit dir
    try:
        sse = (b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":12}}}\n\n'
               b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Use the "}}\n\n'
               b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"venv."}}\n\n'
               b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":4}}\n\n'
               b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
        tap = proxy.SSETap("https://api.anthropic.com/v1/messages")
        out = b"".join(tap(sse[k:k + 37]) for k in range(0, len(sse), 37)) + tap(b"")
        check("streamed bytes pass through unchanged", out == sse)
        check("stream tap reads usage and reply text",
              tap.usage.get("input_tokens") == 12 and tap.usage.get("output_tokens") == 4
              and tap.text == "Use the venv.")
        check("stream tap pushes the reply once", pushed == [("Use the venv.",)])
        check("stream tap logs usage once", len(logged) == 1 and logged[0]["usage"]["output_tokens"] == 4)
    finally:
        proxy._in_background, proxy._log = real_bg, real_log

    class _H(dict):
        def get(self, k, d=None):
            return super().get(k.lower(), d)

    class _Req:
        method = "POST"
        headers = _H({"user-agent": "claude-cli/2", "content-type": "application/json"})
        def __init__(self, path):
            self.path = path

    class _Flow:
        def __init__(self, path):
            self.request = _Req(path)

    check("messages endpoint (with query) is handled", proxy._is_anthropic_messages(_Flow("/v1/messages?beta=true")))
    check("count_tokens is not intercepted", not proxy._is_anthropic_messages(_Flow("/v1/messages/count_tokens")))


def test_capture_paths():
    print("\nI. CAPTURE DEDUPE + INCREMENTAL READS")
    from shadow_link.database import Database
    from shadow_link.live import LiveWatcher, operator_text, read_tail_lines

    test_db = ROOT / "_test_v3_capture.db"
    transcript = ROOT / "_test_v3_capture.jsonl"
    for pth in (test_db, transcript):
        if pth.exists():
            pth.unlink()
    try:
        db = Database(test_db)
        meta_hook = {"agent_output": "I will set up Docker for local development.", "source": "hook"}
        meta_live = {"agent_output": "I will set up Docker for local development.\n", "source": "live"}
        a, created_a = db.add_prompt_unique("no, use the venv", "rejection", metadata=meta_hook)
        b, created_b = db.add_prompt_unique("no, use the venv ", "rejection", metadata=meta_live)
        check("same reply from two capture paths is stored once", created_a and not created_b and a == b)
        with db._connection() as conn:
            conn.execute("UPDATE prompts SET created_at = created_at - 7200")
        _, created_c = db.add_prompt_unique("no, use the venv", "rejection",
                                            metadata={"agent_output": "Shall I add a Dockerfile?"})
        check("same words in a different situation later are a new pair", created_c)
        db.add_prompt_unique("continue", "request", metadata={"agent_output": "Step 1 done. Next: migrations?"})
        _, again = db.add_prompt_unique("continue", "request", metadata={"agent_output": "Tests pass. Deploy now?"})
        check("a repeated short reply to a different situation is kept", again)
        _, nosit = db.add_prompt_unique("continue", "request", metadata={})
        check("a capture with no situation right after another is a duplicate", not nosit)

        check("operator text drops Claude Code reminders",
              operator_text([{"type": "text", "text": "<system-reminder>x</system-reminder>"},
                             {"type": "text", "text": "ship it"}]) == "ship it")

        lines = [json.dumps({"type": "user", "n": k, "pad": "x" * 50}) for k in range(40)]
        transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
        got = list(read_tail_lines(str(transcript), chunk=97))
        check("tail reader returns every line newest first across chunk boundaries",
              [json.loads(x)["n"] for x in got] == list(range(39, -1, -1)))

        transcript.write_text(json.dumps({"type": "assistant", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "I'll set up Docker."}]}}) + "\n",
            encoding="utf-8")
        w = LiveWatcher(str(test_db), str(transcript))
        w._prime()
        check("watcher primes at the end of the transcript", w._pos == transcript.stat().st_size)
        new_line = json.dumps({"type": "user", "message": {"role": "user", "content": "no, use the venv"}})
        with open(transcript, "a", encoding="utf-8") as f:
            f.write(new_line[:20])                    # a line still being written
        check("partial lines are held back", w._read_new_lines() == [])
        with open(transcript, "a", encoding="utf-8") as f:
            f.write(new_line[20:] + "\n")
        check("completed lines are read once", w._read_new_lines() == [new_line] and w._read_new_lines() == [])
    finally:
        for pth in (test_db, transcript, Path(str(test_db) + "-wal"), Path(str(test_db) + "-shm")):
            if pth.exists():
                pth.unlink()

# ------------------------------------------------------------------
# F. TUI Component Initialization
# ------------------------------------------------------------------
def test_tui_initialization():
    print("\nF. TUI COMPONENT INITIALIZATION")
    import threading
    from http.server import HTTPServer
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.document import Document
    from prompt_toolkit.formatted_text import to_formatted_text
    from prompt_toolkit.input import create_pipe_input
    from shadow_link.tui import DaemonAPIError, ShadowLinkConsole, WatchView
    from shadow_link.api import ShadowLinkAPIHandler
    from unittest.mock import patch

    test_db = ROOT / "_test_v3_tui.db"
    test_audit_dir = ROOT / "_test_v3_audit"
    if test_db.exists():
        test_db.unlink()
    test_audit_dir.mkdir(exist_ok=True)
    (test_audit_dir / "2026-06-21.jsonl").write_text(json.dumps({
        "timestamp": "2026-06-21T12:00:00+00:00", "phase": "request",
        "original_bytes": 1000, "optimized_bytes": 800,
    }) + "\n", encoding="utf-8")
    old_env = {key: os.environ.get(key) for key in
               ("DB_PATH", "SHADOW_LM_MODE", "SHADOW_QUERY_EMBED", "SHADOW_LINK_AUDIT_DIR")}
    os.environ["DB_PATH"] = str(test_db)
    os.environ["SHADOW_LM_MODE"] = "0"
    os.environ["SHADOW_QUERY_EMBED"] = "0"
    os.environ["SHADOW_LINK_AUDIT_DIR"] = str(test_audit_dir)
    server = HTTPServer(("127.0.0.1", 0), ShadowLinkAPIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        with create_app_session(output=DummyOutput()):
            console = ShadowLinkConsole()
            console.daemon_url = f"http://127.0.0.1:{server.server_address[1]}"
            check("Console instantiated successfully", console is not None)
            check("Console initialized config and db correctly", console.config is not None and console.db is not None)
            check("Console initialized PromptSession session", console.session is not None)
            check("Console exposes connected command set",
                  {"/status", "/predict", "/pairs", "/proxy", "/accept", "/reject"}.issubset(console.commands))
            check("Claude Shadow command replaces claude-watch",
                  "/claude-shadow" in console.commands and "/claude-watch" not in console.commands)
            root_completions = list(
                console.completer.get_completions(Document("/"), None)
            )
            root_completion_text = {item.text for item in root_completions}
            root_meta = {
                item.text: "".join(part[1] for part in to_formatted_text(item.display_meta))
                for item in root_completions
            }
            check("Slash palette preserves the complete command set",
                  root_completion_text == set(console.commands))
            check("Slash palette adds premium command descriptions",
                  all(root_meta.get(command) for command in console.commands))
            proxy_completions = list(
                console.completer.get_completions(Document("/proxy "), None)
            )
            check("Slash palette preserves nested proxy actions",
                  {item.text for item in proxy_completions} ==
                  {"status", "start", "stop", "view", "setup"})
            check("Slash palette yields to proxy arguments after an action",
                  not list(console.completer.get_completions(
                      Document("/proxy start "), None)))
            with patch("shadow_link.tui.time.monotonic", return_value=0.0):
                prompt_frame_a = "".join(fragment[1] for fragment in console._prompt_text())
            with patch("shadow_link.tui.time.monotonic", return_value=0.13):
                prompt_frame_b = "".join(fragment[1] for fragment in console._prompt_text())
            check("Console command prompt carries animated Braille motion",
                  prompt_frame_a != prompt_frame_b and "shadow-link" in prompt_frame_a)
            toolbar_text = "".join(fragment[1] for fragment in console._bottom_toolbar_text())
            check("Console toolbar exposes cached live telemetry",
                  "memories" in toolbar_text and "Tab" in toolbar_text and "LOCAL" in toolbar_text)
            with create_pipe_input() as shell_input:
                with create_app_session(input=shell_input, output=DummyOutput()):
                    premium_shell = ShadowLinkConsole()
                    with patch.object(premium_shell, "print_welcome"), \
                            patch.object(premium_shell, "_prompt_text",
                                         wraps=premium_shell._prompt_text) as prompt_renderer, \
                            patch.object(premium_shell, "_bottom_toolbar_text",
                                         wraps=premium_shell._bottom_toolbar_text) as toolbar_renderer:
                        shell_input.send_text("/exit\r\r")
                        premium_shell.run()
            check("Premium animated prompt and toolbar render in the live shell",
                  prompt_renderer.call_count > 0 and toolbar_renderer.call_count > 0)
            with patch("shadow_link.tui._print_formatted_text") as help_renderer:
                console.cmd_help()
            help_text = "".join(
                fragment[1] for fragment in to_formatted_text(help_renderer.call_args.args[0])
            )
            check("Help screen uses premium grouped information hierarchy",
                  all(section in help_text for section in
                      ("CORE MEMORY", "LIVE MIRROR", "FEEDBACK LOOP", "SYSTEM")))

            shadow_view = WatchView(console)
            splash_text = "".join(fragment[1] for fragment in shadow_view._splash_text())
            check("Claude Shadow opens on a Braille image splash",
                  shadow_view.splash_mode and "⣿" in splash_text and "CLAUDE SHADOW" not in splash_text)
            check("Claude Shadow splash prompts for Enter",
                  "PRESS ENTER TO CONTINUE" in splash_text)
            shadow_view._splash_frame += 1
            animated_splash = "".join(
                fragment[1] for fragment in shadow_view._splash_text()
            )
            check("Claude Shadow splash animates its Braille spinner",
                  animated_splash != splash_text and "⠙" in animated_splash)
            with patch("shadow_link.tui.get_app") as current_app:
                current_app.return_value.output.get_size.return_value.rows = 24
                compact_splash = "".join(
                    fragment[1] for fragment in shadow_view._splash_text()
                )
            check("Claude Shadow uses a compact raster on short terminals",
                  len(compact_splash.splitlines()) <= 23 and "⣿" in compact_splash)
            with patch.object(shadow_view, "_open_session_picker") as open_picker:
                shadow_view._continue_from_splash()
            check("Enter transition opens the session picker",
                  not shadow_view.splash_mode and open_picker.call_count == 1)
            shadow_view.ghost = {
                "id": 1, "text": "sample ghost", "confidence": 0.7,
                "bucket": "correction", "source": "test", "tier": "high",
            }
            ghost_text = "".join(
                fragment[1] for fragment in to_formatted_text(shadow_view._ghost_text())
            )
            check("Ghost panel renders a premium confidence meter",
                  "███████░░░" in ghost_text and "0.70" in ghost_text)

            with create_pipe_input() as pipe_input:
                with create_app_session(input=pipe_input, output=DummyOutput()):
                    keyed_view = WatchView(console)
                    with patch.object(keyed_view, "_fetch_daemon_state", return_value=None), \
                            patch.object(keyed_view, "_open_session_picker") as keyed_picker:
                        def feed_keys():
                            pipe_input.send_text("\r")
                            time.sleep(0.35)
                            pipe_input.send_text("\x03")
                        feeder = threading.Thread(target=feed_keys, daemon=True)
                        feeder.start()
                        keyed_view.run()
                        feeder.join(timeout=1.0)
            check("Full-screen Enter key binding reaches the session picker",
                  keyed_picker.call_count == 1 and not keyed_view.splash_mode)
            check("UI animation thread stops after leaving the view",
                  keyed_view._animation_thread is not None and
                  not keyed_view._animation_thread.is_alive())
            check("Premium motion continues across the session workspace",
                  keyed_view._splash_frame > 0)
            check("Console connects to daemon API", console.check_daemon(force=True))

            proxy = console._request_json("GET", "/proxy/status")
            check("TUI reads Claude Code proxy state from daemon",
                  {"running", "managed", "port", "available", "proxy_url"}.issubset(proxy))
            check("Proxy manager finds user-installed mitmdump", proxy.get("available"))
            audit = console._request_json("GET", "/proxy/audit?limit=5")
            check("TUI reads recent proxy traffic from daemon",
                  len(audit.get("events", [])) == 1 and audit["events"][0]["phase"] == "request")
            console._proxy_view(5)
            check("TUI renders proxy traffic view", True)

            original_db_path = console.config.db_path
            console.config.db_path = test_audit_dir / "shadow_link.db"
            launcher = console._write_proxy_launcher(8080)
            launcher_text = launcher.read_text(encoding="utf-8")
            check("Proxy setup generates scoped Claude PowerShell launcher",
                  "HTTPS_PROXY" in launcher_text and "& claude @ClaudeArgs" in launcher_text)
            launcher.unlink()
            console.config.db_path = original_db_path

            with patch.object(console, "_remote", side_effect=DaemonAPIError("Endpoint not found")):
                legacy_proxy, legacy_source = console._proxy_action("status", 8080)
            check("TUI falls back when connected daemon predates proxy API",
                  legacy_source == "TUI process" and "running" in legacy_proxy)

            learned = console._request_json("POST", "/ingest", {
                "question": "I will wrap the entire upload function in try/except.",
                "response": "That's wrong; only guard the network call.",
                "request_id": "tui-test",
            })
            check("TUI daemon ingest stores a reaction pair", isinstance(learned.get("prompt_id"), int))

            prediction = console._request_json("POST", "/predict", {
                "question": "I will wrap the entire upload function in try/except.",
            })
            ghost = prediction.get("ghost") or {}
            check("TUI daemon prediction uses situation context", "network call" in ghost.get("text", ""))

            feedback = console._request_json("POST", "/feedback", {
                "id": ghost.get("id"), "vote": "up", "text": "",
                "situation": "I will wrap the entire upload function in try/except.",
            })
            check("TUI feedback closes reinforcement loop", feedback.get("new_weight", 0) > 1.0)

            stats = console._request_json("GET", "/stats")
            check("Daemon stats expose prompts and graph edges",
                  stats.get("total_prompts") == 1 and "total_edges" in stats)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if test_db.exists():
            test_db.unlink()
        for path in test_audit_dir.glob("*"):
            path.unlink()
        test_audit_dir.rmdir()



# ------------------------------------------------------------------
# G. Associative Context Activation
# ------------------------------------------------------------------
def _auth_graph():
    from shadow_link.aca import ACA, CognitiveStore
    s = CognitiveStore(":memory:")
    aca = ACA(s)
    n = {
        "auth": s.add_node("Authentication subsystem: login, sessions, tokens", "architecture", "authentication"),
        "tok": s.add_node("Token system issues access and refresh tokens", "architecture", "token system"),
        "ref": s.add_node("Renews expired refresh tokens", "file", "refresh-token.ts"),
        "ses": s.add_node("Session manager", "file", "session.ts"),
        "bug": s.add_node("Previous auth bug: login broke because the refresh token expired", "fix", "previous token bug"),
        "ui": s.add_node("Old discussion about button colours", "conversation", "old UI discussion"),
        "old": s.add_node("Store sessions in memory", "decision", "in-memory sessions"),
        "new": s.add_node("Store sessions in redis", "decision", "redis sessions"),
    }
    s.link(n["auth"], n["tok"], "part_of", strength=0.9)
    s.link(n["tok"], n["ref"], "implemented_in", strength=0.85)
    s.link(n["auth"], n["ses"], "related_to", strength=0.6)
    s.link(n["bug"], n["ref"], "occurred_in", strength=0.9)
    s.link(n["bug"], n["auth"], "related_to", strength=0.8)
    s.link(n["ui"], n["auth"], "related_to", strength=0.1)
    s.link(n["ses"], n["old"], "related_to", strength=0.7)
    s.link(n["ses"], n["new"], "related_to", strength=0.7)
    s.link(n["new"], n["old"], "supersedes", strength=0.9)
    return s, aca, n


def test_aca():
    print("\nG. ASSOCIATIVE CONTEXT ACTIVATION")
    from shadow_link.aca import Budget, JevBackend
    from shadow_link.aca.activation import detect_seeds, spread, compete, extract_entities

    s, aca, n = _auth_graph()

    check("entity extraction finds files and error types",
          {"refresh-token.ts", "RefreshTokenExpired"} <= set(extract_entities(
              "login fails with RefreshTokenExpired in refresh-token.ts")))

    seeds = detect_seeds(s, "why did auth break again? check refresh-token.ts")
    seed_ids = {x.node_id for x in seeds}
    check("seeds come from entity match and prefix match ('auth' ~ 'authentication')",
          n["ref"] in seed_ids and n["auth"] in seed_ids)

    acts, trav = spread(s, [type(seeds[0])(n["auth"], 1.0, "test")], Budget())
    check("activation spreads to neighbours", n["tok"] in acts and n["ref"] in acts)
    check("activation decays with distance",
          acts[n["auth"]].activation > acts[n["tok"]].activation > acts[n["ref"]].activation)
    check("weak synapses pass little activation",
          acts.get(n["ui"]) is None or acts[n["ui"]].activation < acts[n["ses"]].activation)
    check("activation path is recorded for explainability",
          [nid for _, nid in acts[n["ref"]].path][-1] == n["ref"])
    check("supersedes inhibits the older decision", acts[n["old"]].inhibition > 0)

    cands = {c.id: c for c in compete(s, acts)}
    check("superseded/inhibited decision is suppressed below its successor",
          cands[n["old"]].score < cands[n["new"]].score)

    s.set_status(n["old"], "superseded")
    cands = {c.id: c for c in compete(s, acts)}
    check("superseded status suppresses the node", cands[n["old"]].state == "suppressed")

    out = aca.process("why did auth break again? login broke, refresh token", task_id="T1")
    ids = [i["id"] for i in out["context"]]
    check("process converges to a small context", 0 < len(ids) <= Budget().max_selected)
    check("converged context contains the previous bug", n["bug"] in ids)
    check("irrelevant UI discussion does not reach the context", n["ui"] not in ids)
    check("every pulled item explains why it is active",
          all(i["why"]["seed_reason"] and i["why"]["factors"] for i in out["context"]))
    check("funnel narrows", out["funnel"]["activated"] >= out["funnel"]["critical"])
    names = {x["name"] for x in out["signals"]}
    check("standard neurons fire in parallel",
          {"should_expand", "context_conflict", "needs_verification", "next_action", "remember"} <= names)
    check("signals carry firing levels", all(x["level"] in
          ("silent", "latent", "activate", "strong", "fire") for x in out["signals"]))
    check("jev state has the documented shape", {"current_task", "active_context", "memories",
          "hypotheses", "tool_results", "previous_failures", "project_constraints"} <= set(out["jev_state"]))

    # contradictions are first-class and travel together
    a_id = s.add_node("The API requires token rotation on refresh", "decision", "token rotation required")
    b_id = s.add_node("The API does not require token rotation on refresh", "decision", "token rotation disabled")
    check("polarity contradiction detected", a_id in aca.detect_contradictions(b_id))
    out_c = aca.process("token rotation on refresh", task_id="T2")
    cids = {i["id"] for i in out_c["context"]}
    check("both sides of a contradiction are activated together", {a_id, b_id} <= cids)
    check("conflict neuron fires on active contradiction",
          next(x for x in out_c["signals"] if x["name"] == "context_conflict")["value"] > 0.7)

    # hypothesis network
    h1 = aca.add_hypothesis("T1", "expired refresh token", 0.5, about=[n["ref"]])
    h2 = aca.add_hypothesis("T1", "external API outage", 0.3)
    up = aca.add_evidence("T1", h1, "stack trace: RefreshTokenExpired", kind="error")
    down = aca.add_evidence("T1", h2, "status page green, other endpoints fine", supports=False)
    check("supporting evidence strengthens a hypothesis", up["posterior"] > up["prior"])
    check("refuting evidence weakens a hypothesis", down["posterior"] < down["prior"])
    for _ in range(3):
        aca.add_evidence("T1", h2, "upstream health check ok", supports=False, kind="tool_result")
    check("hypothesis eliminated once implausible", s.get_node(h2)["status"] == "refuted")

    # learning loop
    out2 = aca.process("refresh-token.ts renewal", task_id="T1")
    target = next((i for i in out2["context"] if len(i["why"]["path"]) > 1), None)
    check("a context item reached through the graph exists", target is not None)
    if target:
        path = [p["node"] for p in target["why"]["path"]]
        before = {(e["src"], e["dst"], e["rel"]): e["strength"] for e in s.synapses_between(path)}
        fb = aca.feedback("T1", target["id"], True)
        after = {(e["src"], e["dst"], e["rel"]): e["strength"] for e in s.synapses_between(path)}
        check("useful feedback reinforces the activation path",
              fb["synapses"] and any(after[k] > before[k] for k in before))
        aca.feedback("T1", target["id"], False)
        check("useless feedback weakens the path",
              any(e["fail_count"] > 0 for e in s.synapses_between(path)))

    # inspector + replay + working brain
    insp = aca.inspect("T1", out2["context"][0]["id"])
    check("inspector explains activation", insp["in_context"] and insp["why"]["path"])
    kinds = {e["kind"] for e in s.replay("T1")}
    check("replay log records the cognition", {"event", "seed", "converged", "judgment",
          "hypothesis_created", "hypothesis_strengthened", "feedback"} <= kinds)
    snap = aca.snapshot("T1")
    check("working brain snapshot lists hypotheses", snap["hypotheses"][0]["id"] == h1)

    # episodes + consolidation
    ep = aca.close_task("T1", result="resolved", lesson="check refresh-token state before outages")
    check("episode stored and lesson consolidated into semantic memory",
          s.get_node(ep["episode"])["type"] == "episode" and s.get_node(ep["semantic"])["type"] == "semantic")
    out3 = aca.process("auth failing again, maybe an outage?", task_id="T3")
    check("consolidated lesson is reactivated on a later task",
          any(i["id"] in (ep["semantic"], ep["episode"]) for i in out3["context"])
          or ep["semantic"] in aca.brain("T3").active or ep["episode"] in aca.brain("T3").active)

    # Jev adapter: client answers some questions, the rest fall back
    calls = []
    def fake_client(state, questions):
        calls.append(len(questions))
        return {q["name"]: {"noul": 0.99} for q in questions if q["name"] == "should_expand"}
    from shadow_link.aca import ACA
    aca_j = ACA(s, backend=JevBackend(fake_client))
    out_j = aca_j.process("why did auth break", task_id="T4", remember=False)
    exp = next(x for x in out_j["signals"] if x["name"] == "should_expand")
    check("Jev backend answers its questions and the rest fall back",
          exp["backend"] == "jev" and any(x["backend"] == "heuristic" for x in out_j["signals"]))
    check("high should_expand triggers a recurrent expansion cycle", out_j["cycles"] >= 2)

    # corpus bridge: reaction pairs become graph nodes
    from shadow_link.database import Database
    test_db = ROOT / "_test_v3_aca.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(test_db) + suffix)
        if p.exists():
            p.unlink()
    db = Database(test_db)
    db.add_prompt("do not use docker for local dev, use the venv", "rejection",
                  metadata={"agent_output": "I will set up a Docker container for the dev environment"})
    from shadow_link.aca import open_aca
    aca_db = open_aca(test_db)
    out_db = aca_db.process("I'll create a Docker container for development", task_id="D1")
    check("reaction corpus is mirrored and a rejection activates despite its negation",
          any(i["id"] == "P-1" for i in out_db["context"]))
    db.add_prompt("use podman instead", "pivot",
                  metadata={"agent_output": "I will set up a Docker container for the dev environment"})
    out_two = open_aca(test_db).process("I'll set up a Docker container for the dev environment", task_id="D1b")
    two = {i["id"] for i in out_two["context"] if i["type"] == "reaction"}
    check("two different replies to the same situation are not treated as duplicates", {"P-1", "P-2"} <= two)
    check("reaction context items carry the situation they answered",
          all(i["situation"] for i in out_two["context"] if i["type"] == "reaction"))

    # bug fixes: orphans after clear, prompt memory opt-in, read-only reads, event pruning
    db.clear()
    out_cleared = open_aca(test_db).process("I'll create a Docker container for development", task_id="D2")
    check("cleared corpus replies no longer activate",
          not any(i["id"].startswith("P-") for i in out_cleared["context"]))
    check("orphaned reply nodes are deleted", aca_db.store.get_node("P-1") is None)
    db.add_prompt("use podman, not docker", "rejection",
                  metadata={"agent_output": "I'll create a Docker container for development"})
    out_new = open_aca(test_db).process("I'll create a Docker container for development", task_id="D3")
    check("replies added after a clear are mirrored again",
          any(i["type"] == "reaction" and "podman" in i["content"] for i in out_new["context"]))

    long_prompt = ("We need to migrate the billing service from the legacy payments gateway to the new "
                   "provider while keeping invoices, refunds, webhooks and reconciliation working")
    s2, aca2, _ = _auth_graph()
    check("plain prompts are not stored as memory by default",
          aca2.process(long_prompt, task_id="M1")["memory"] is None)
    check("prompts are stored when explicitly asked",
          aca2.process(long_prompt, task_id="M1", remember=True)["memory"] is not None)
    before = len(s2.replay("never-opened"))
    aca2.snapshot("never-opened")
    check("reading a task's brain writes nothing", len(s2.replay("never-opened")) == before
          and s2.load_task("never-opened") is None)
    s2.log("old", task_id="M1")
    with s2._conn() as c:
        c.execute("UPDATE aca_events SET ts = ts - 90 * 86400 WHERE kind = 'old'")
    check("old replay events are pruned", s2.prune_events(30) >= 1
          and not any(e["kind"] == "old" for e in s2.replay("M1")))

    bad_shapes = [{"noul": "not-a-number"}, {"noul": 3.0}, {"choice": {}}, "garbage"]
    for bad in bad_shapes:
        aca_bad = ACA(s2, backend=JevBackend(lambda st, qs, b=bad: {q["name"]: b for q in qs}))
        try:
            res = aca_bad.process("why did auth break", task_id="J1", remember=False)
            ok = all(x["backend"] == "heuristic" for x in res["signals"])
        except Exception:
            ok = False
        check(f"malformed Jev result {bad!r} falls back instead of crashing", ok)

    def boom(state, questions):
        raise RuntimeError("network down")
    res = ACA(s2, backend=JevBackend(boom)).process("why did auth break", task_id="J2", remember=False)
    check("a failing Jev client falls back to heuristics", len(res["signals"]) > 0)

    # HTTP API
    import threading
    import urllib.request
    from http.server import HTTPServer
    from shadow_link.api import ShadowLinkAPIHandler
    old = os.environ.get("DB_PATH")
    os.environ["DB_PATH"] = str(test_db)
    server = HTTPServer(("127.0.0.1", 0), ShadowLinkAPIHandler)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        req = urllib.request.Request(base + "/aca/event", method="POST",
                                     data=json.dumps({"text": "set up docker for dev", "task_id": "API1"}).encode(),
                                     headers={"Content-Type": "application/json"})
        res = json.loads(urllib.request.urlopen(req, timeout=10).read())
        check("POST /aca/event returns converged context", res["task_id"] == "API1" and "context" in res)
        rep = json.loads(urllib.request.urlopen(base + "/aca/replay?task_id=API1", timeout=10).read())
        check("GET /aca/replay returns the task's events", any(e["kind"] == "converged" for e in rep))
    finally:
        server.shutdown()
        server.server_close()
        if old is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = old
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(test_db) + suffix)
            if p.exists():
                p.unlink()


def test_ghost_fixes():
    print("\nH. GHOST DECAY + CLASSIFICATION")
    from shadow_link.database import Database
    from shadow_link.decay import effective_weight_for_row, WEIGHT_FLOOR_FRACTION
    from shadow_link.ingester import classify_multi
    from shadow_link.live import build_prediction

    check("explicit negation is classified as a rejection",
          classify_multi("do not use docker for local dev")[0] == "rejection")
    check("'never' is a rejection signal", "rejection" in classify_multi("never mock the database"))
    check("'wrong' alone is not also a correction", "correction" not in classify_multi("that's wrong"))

    now = time.time()
    day_old = effective_weight_for_row({"bucket": "rejection", "weight": 1.0, "created_at": now - 86400}, now)
    check("a day-old rejection keeps most of its weight (3-day half-life)", day_old > 0.75)
    month_old = effective_weight_for_row({"bucket": "rejection", "weight": 1.0, "created_at": now - 30 * 86400}, now)
    check("decay never drops below the weight floor", abs(month_old - WEIGHT_FLOOR_FRACTION) < 1e-9)

    test_db = ROOT / "_test_v3_ghost.db"
    for suffix in ("", "-wal", "-shm"):
        pth = Path(str(test_db) + suffix)
        if pth.exists():
            pth.unlink()
    old_lm = os.environ.get("SHADOW_LM_MODE")
    os.environ["SHADOW_LM_MODE"] = "0"
    try:
        import importlib
        import shadow_link.generate as gen
        importlib.reload(gen)
        db = Database(test_db)
        pid = db.add_prompt("that's wrong, don't use docker, use the venv", "rejection",
                            metadata={"agent_output": "I will set up a Docker container for the dev environment"})
        with db._connection() as conn:
            conn.execute("UPDATE prompts SET created_at = created_at - 86400")
        ghost, _ = build_prediction(str(test_db), "I'll set up a Docker container for the dev environment")
        check("a 24-hour-old rejection still produces a ghost", bool(ghost) and ghost["id"] == pid)
        check("surfacing a ghost re-arms its decay clock",
              (db.get_prompt(pid)["triggered_at"] or 0) > time.time() - 60)
    finally:
        if old_lm is None:
            os.environ.pop("SHADOW_LM_MODE", None)
        else:
            os.environ["SHADOW_LM_MODE"] = old_lm
        importlib.reload(gen)
        for suffix in ("", "-wal", "-shm"):
            pth = Path(str(test_db) + suffix)
            if pth.exists():
                pth.unlink()


def main():
    test_engine()
    asyncio.run(test_mcp())
    test_hook()
    asyncio.run(test_prompt_wrapper())
    test_proxy_optimizer()
    test_tui_initialization()
    test_aca()
    test_ghost_fixes()
    test_capture_paths()
    print("\n" + "=" * 40)
    failed = [n for n, s in results if s == FAIL]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED:", ", ".join(failed))
        sys.exit(1)
    print("ALL GREEN")

if __name__ == "__main__":
    main()
