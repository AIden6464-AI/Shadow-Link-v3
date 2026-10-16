"""
shadow_link/cli.py

CLI entry point for Shadow Link v3 commands.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
import os
import sys
from pathlib import Path

from shadow_link.config import Config
from shadow_link.database import Database
from shadow_link.engine import Engine
from shadow_link.ingester import Ingester
from shadow_link.watcher import FileWatcher
from shadow_link.prompt_wrapper import run_prompt_wrapper

def get_services():
    """Initializes and returns shared config, database, and matcher services."""
    config = Config()
    db = Database(config.db_path)
    matcher = Engine(db)
    ingester = Ingester(
        db,
        provider=config.embedding_provider,
        url=config.embedding_api_url,
        model=config.embedding_model
    )
    return config, db, matcher, ingester

def handle_ingest_jsonl(args):
    _, _, _, ingester = get_services()
    print(f"Ingesting OGI trace JSONL from: {args.path}")
    try:
        count = ingester.ingest_oneiros_jsonl(args.path)
        print(f"Successfully imported {count} prompts from OGI logs.")
    except Exception as e:
        print(f"Error during ingestion: {e}", file=sys.stderr)
        sys.exit(1)

def handle_ingest_md(args):
    _, _, _, ingester = get_services()
    print(f"Ingesting Markdown prompts from: {args.path}")
    try:
        count = ingester.ingest_markdown(args.path)
        print(f"Successfully imported {count} prompts from Markdown.")
    except Exception as e:
        print(f"Error during ingestion: {e}", file=sys.stderr)
        sys.exit(1)

def handle_ingest_human(args):
    _, _, _, ingester = get_services()
    print(f"Ingesting OGI human interaction files from: {args.path}")
    try:
        count = ingester.ingest_oneiros_human_history(args.path)
        print(f"Successfully imported {count} paired human interactions.")
    except Exception as e:
        print(f"Error during ingestion: {e}", file=sys.stderr)
        sys.exit(1)

def handle_ingest_claude_export(args):
    _, _, _, ingester = get_services()
    print(f"Importing Claude export reaction pairs from: {args.path}")
    try:
        res = ingester.ingest_claude_export(args.path)
        print(f"Imported {res['imported']} reaction pairs "
              f"(skipped {res['skipped']} duplicates of {res['candidates']} candidates).")
    except Exception as e:
        print(f"Error during import: {e}", file=sys.stderr)
        sys.exit(1)

def handle_ingest_sessions(args):
    _, _, _, ingester = get_services()
    sources = tuple(args.only) if args.only else ("claude", "codex", "agy")
    print(f"Importing reaction pairs from agent CLI histories: {', '.join(sources)}")
    try:
        report = ingester.ingest_agent_sessions(sources)
    except Exception as e:
        print(f"Error during import: {e}", file=sys.stderr)
        sys.exit(1)
    labels = {"claude": "Claude Code", "codex": "Codex CLI", "agy": "Antigravity CLI"}
    for src, r in report.items():
        print(f"  {labels[src]:<16} {r['sessions']:>4} sessions  {r['imported']:>5} new pairs  "
              f"({r['skipped']} already stored)")

def handle_query(args):
    _, _, matcher, _ = get_services()
    print(f"Querying database for: '{args.text}'")
    matches = matcher.match(args.text, top_k=args.top_k, threshold=args.threshold)
    
    if not matches:
        print("No matches found above threshold.")
        return
        
    print(f"\nTop {len(matches)} Matches:")
    for idx, r in enumerate(matches):
        sem_str = f"{r.cosine_sim:.2f}" if r.cosine_sim >= 0 else "N/A"
        print(f"[{idx+1}] Score: {r.combined_score:.2f} (Jaccard: {r.jaccard_sim:.2f}, Semantic: {sem_str})")
        print(f"    Bucket: {r.bucket}")
        print(f"    Content: '{r.text}'")
        print("-" * 50)

def handle_watch(args):
    _, _, matcher, _ = get_services()
    watcher = FileWatcher(
        watch_dir=args.dir,
        matcher=matcher,
        poll_interval=args.poll_interval,
        auto_approve_threshold=args.auto_approve
    )
    watcher.start_loop()

def handle_stats(args):
    _, db, _, _ = get_services()
    print("=== Shadow Link SQLite Statistics ===")
    print(f"Database Location: {db.db_path}")
    
    try:
        prompts = db.all_prompts()
        print(f"Total Prompts Stored: {len(prompts)}")
        counts = {}
        for p in prompts:
            b = p.get("bucket", "request")
            counts[b] = counts.get(b, 0) + 1
        print("\nBuckets:")
        for b in ("request", "reminder", "rejection", "correction", "pivot"):
            print(f"  - {b}: {counts.get(b, 0)} prompts")
    except Exception as e:
        print(f"Error reading stats: {e}", file=sys.stderr)
        sys.exit(1)

def handle_clear(args):
    _, db, _, _ = get_services()
    confirm = input("Are you sure you want to clear ALL prompts? [y/N]: ").strip().lower()
    if confirm in ("y", "yes"):
        db.clear()
        print("Database cleared successfully.")
    else:
        print("Canceled.")

def handle_interactive(args):
    _, _, matcher, _ = get_services()
    print("=================================================================")
    print("           Shadow Link Interactive Fast-Reply Simulator           ")
    print("=================================================================")
    print("Type your simulated agent prompt/question to see fast-reply predictions.")
    print("Type 'exit' or 'quit' to close.")
    print("-" * 65)
    
    try:
        while True:
            query_text = input("\nSimulated Prompt> ").strip()
            if not query_text:
                continue
            if query_text.lower() in ("exit", "quit"):
                break
                
            matches = matcher.match(query_text, top_k=3, threshold=args.threshold)
            if not matches:
                print("  [No Suggestion] No matching behavioral patterns above threshold.")
                continue
                
            best_match = matches[0]
            sem_str = f"{best_match.cosine_sim:.2f}" if best_match.cosine_sim >= 0 else "N/A"
            
            print(f"  [Match Found] Score: {best_match.combined_score:.2f} (Jaccard: {best_match.jaccard_sim:.2f}, Semantic: {sem_str})")
            print(f"  [Category]    {best_match.bucket.upper()}")
            print(f"  [Ghost Suggestion pill]:")
            print(f"    >>> [ Tab ] {best_match.text} <<<")
            
            if len(matches) > 1:
                print("  Alternates:")
                for idx, alt in enumerate(matches[1:]):
                    print(f"    [{idx+2}] ({alt.combined_score:.2f}) -> {alt.text}")
                    
    except KeyboardInterrupt:
        print("\nExiting interactive mode.")

def _resolve_mitmdump():
    from shadow_link.proxy_runtime import resolve_mitmdump
    return resolve_mitmdump()

def handle_start_api(args):
    from shadow_link.api import run_api_server
    from shadow_link.live import LiveWatcher, ACTIVE_WATCHER
    import shadow_link.live as live_mod
    import sys
    
    config = Config()
    
    # In v3, start the background live tailer watcher automatically with the daemon
    print(f"[Shadow Link Daemon] Starting background LiveWatcher on transcript...")
    watcher = LiveWatcher(str(config.db_path))
    watcher.start()
    live_mod.ACTIVE_WATCHER = watcher

    # Start transparent proxy in background if requested
    if getattr(args, "proxy", False):
        from shadow_link.proxy_runtime import PROXY_RUNTIME
        try:
            print(f"[Shadow Link Daemon] Starting background transparent proxy on port {args.proxy_port}...")
            PROXY_RUNTIME.start(args.proxy_port)
        except Exception as exc:
            print(f"[Shadow Link Warning] Background proxy disabled: {exc}", file=sys.stderr)
    
    run_api_server(host=args.host, port=args.port)

def handle_proxy(args):
    import subprocess
    import sys
    from pathlib import Path
    
    mitmdump_bin = _resolve_mitmdump()
    if not mitmdump_bin:
        print("[Shadow Link Error] mitmdump binary not found. Please ensure mitmproxy is installed.", file=sys.stderr)
        sys.exit(1)
        
    addon_path = Path(__file__).resolve().parent / "proxy.py"
    cmd = [
        str(mitmdump_bin),
        "-s", str(addon_path),
        "--listen-port", str(args.port),
        "-q"
    ]
    
    print(f"[Shadow Link] Starting integrated transparent proxy on port {args.port}...")
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        print("\n[Shadow Link] Proxy stopped.")

def handle_run(args):
    config = Config()
    if not args.cmd:
        print("[Shadow Link Error] Please specify a command to run (e.g. shadow-link run cmd.exe)", file=sys.stderr)
        sys.exit(1)
    run_prompt_wrapper(args.cmd, config.db_path)

def handle_mcp(args):
    from shadow_link.mcp_server import run_mcp_server
    run_mcp_server()

def handle_tui(args):
    from shadow_link.tui import run_tui
    run_tui()

def handle_aca(args):
    from shadow_link.aca import open_aca
    aca = open_aca()
    if args.replay:
        if not args.task:
            print("--replay needs --task")
            return
        for e in aca.store.replay(args.task):
            stamp = datetime.fromtimestamp(e["ts"]).strftime("%H:%M:%S")
            val = "" if e["value"] is None else f" {e['value']:.2f}"
            print(f"{stamp} c{e['cycle']} {e['kind']:<22} {e['node_id']:<16}{val}  "
                  f"{json.dumps(e['detail'], default=str)[:100]}")
        return
    text = " ".join(args.text).strip()
    if not text:
        print("Provide event text, e.g. shadow-link aca \"why did auth break again?\"")
        return
    out = aca.process(text, kind=args.kind, task_id=args.task or None)
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return
    f = out["funnel"]
    print(f"task {out['task_id']}  ·  {out['cycles']} cycle(s)  ·  {out['elapsed_ms']} ms  ·  backend {out['backend']}")
    print(f"funnel: {f['graph_nodes']} nodes → {f['activated']} activated → {f['significant']} significant"
          f" → {f['highly_relevant']} highly relevant → {f['critical']} critical ({f['suppressed']} suppressed)")
    print("\nACTIVE CONTEXT")
    for i in out["context"]:
        path = " → ".join(p["title"][:24] for p in i["why"]["path"]) or i["title"][:24]
        flag = f"  ⚡ conflicts {', '.join(i['conflicts'])}" if i["conflicts"] else ""
        print(f"  {i['score']:.2f} [{i['type']}] {i['title'][:60]}{flag}")
        print(f"       why: {i['why']['seed_reason']} · {path}")
    if out["hypotheses"]:
        print("\nHYPOTHESES")
        for h in out["hypotheses"]:
            print(f"  {h['confidence']:.2f} {h['title']} ({h['status']})")
    print("\nSIGNALS")
    for s in out["signals"]:
        top = f" → {s['top']}" if s.get("top") else ""
        print(f"  {s['value']:.2f} {s['level']:<8} {s['name']}{top}")
    if out["actions"]:
        print("\nACTIONS")
        for a in out["actions"]:
            print("  " + ", ".join(f"{k}={v}" for k, v in a.items()))

def main():
    parser = argparse.ArgumentParser(
        description="Shadow Link v3 — the local, terminal-native cognitive mirror"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Ingest JSONL
    ingest_jsonl_parser = subparsers.add_parser("ingest-jsonl", help="Ingest OGI trace JSONL log")
    ingest_jsonl_parser.add_argument("path", type=Path, help="Path to llm_calls.jsonl")
    ingest_jsonl_parser.set_defaults(func=handle_ingest_jsonl)

    # Ingest MD
    ingest_md_parser = subparsers.add_parser("ingest-md", help="Ingest Markdown prompt log")
    ingest_md_parser.add_argument("path", type=Path, help="Path to markdown file")
    ingest_md_parser.set_defaults(func=handle_ingest_md)

    # Ingest OGI Human Responses
    ingest_human_parser = subparsers.add_parser("ingest-human", help="Ingest paired OGI human response files")
    ingest_human_parser.add_argument("path", type=Path, help="Path to OGI intake/state folder")
    ingest_human_parser.set_defaults(func=handle_ingest_human)

    # Ingest Claude.ai export (conversations.json)
    ce_parser = subparsers.add_parser("ingest-claude-export", help="Import reaction pairs from a Claude.ai data export")
    ce_parser.add_argument("path", type=Path, help="Path to conversations.json")
    ce_parser.set_defaults(func=handle_ingest_claude_export)

    # Agent CLI histories (Claude Code, Codex CLI, Antigravity CLI)
    sessions_parser = subparsers.add_parser("ingest-sessions", help="Import reaction pairs from Claude Code, Codex CLI and Antigravity CLI histories")
    sessions_parser.add_argument("--only", nargs="+", choices=["claude", "codex", "agy"], help="Limit to these sources")
    sessions_parser.set_defaults(func=handle_ingest_sessions)

    # Query Matcher
    query_parser = subparsers.add_parser("query", help="Query the hybrid matcher for predictions")
    query_parser.add_argument("text", help="Query string to search")
    query_parser.add_argument("--top-k", type=int, default=5, help="Number of results to return")
    query_parser.add_argument("--threshold", type=float, default=None, help="Override score threshold")
    query_parser.set_defaults(func=handle_query)

    # Directory Watcher
    watch_parser = subparsers.add_parser("watch", help="Watch a directory for question files")
    watch_parser.add_argument("dir", type=Path, help="Directory to monitor")
    watch_parser.add_argument("--poll-interval", type=float, default=1.0, help="Polling interval in seconds")
    watch_parser.add_argument("--auto-approve", type=float, default=0.90, help="Confidence threshold to auto-reply")
    watch_parser.set_defaults(func=handle_watch)

    # Stats command
    stats_parser = subparsers.add_parser("stats", help="Show database and bucket statistics")
    stats_parser.set_defaults(func=handle_stats)

    # Clear command
    clear_parser = subparsers.add_parser("clear", help="Wipe all prompts from the database")
    clear_parser.set_defaults(func=handle_clear)

    # Interactive simulation
    interactive_parser = subparsers.add_parser("interactive", help="Run interactive Ghost Reply simulation")
    interactive_parser.add_argument("--threshold", type=float, default=None, help="Override default threshold")
    interactive_parser.set_defaults(func=handle_interactive)

    # Defaults below fall back to config.json (applied into the environment on
    # package import), so ports/host can be set once in config.json.
    cfg_host = os.environ.get("SHADOW_LINK_HOST", "localhost")
    cfg_port = int(os.environ.get("SHADOW_LINK_PORT", "5000"))
    cfg_proxy_port = int(os.environ.get("SHADOW_LINK_PROXY_PORT", "8080"))

    # Start API/Daemon command
    api_parser = subparsers.add_parser("daemon", help="Start the API + watcher background daemon")
    api_parser.add_argument("--host", type=str, default=cfg_host, help="Host interface to bind to")
    api_parser.add_argument("--port", type=int, default=cfg_port, help="Port number to listen on")
    api_parser.add_argument("--proxy", action="store_true", help="Start transparent proxy in background")
    api_parser.add_argument("--proxy-port", type=int, default=cfg_proxy_port, help="Port number for background proxy")
    api_parser.set_defaults(func=handle_start_api)

    # Start Proxy command (foreground)
    proxy_parser = subparsers.add_parser("proxy", help="Start the integrated transparent MITM proxy")
    proxy_parser.add_argument("--port", type=int, default=cfg_proxy_port, help="Port to listen on")
    proxy_parser.set_defaults(func=handle_proxy)

    # Run Subprocess command
    run_parser = subparsers.add_parser("run", help="Run a CLI agent interactively with inline completions")
    run_parser.add_argument("cmd", nargs="+", help="Command to run (e.g. shadow-link run antigravity-cli)")
    run_parser.set_defaults(func=handle_run)

    # MCP server command
    mcp_parser = subparsers.add_parser("mcp", help="Run stdio MCP server")
    mcp_parser.set_defaults(func=handle_mcp)

    # Associative Context Activation
    aca_parser = subparsers.add_parser("aca", help="Activate context for an event (Associative Context Activation)")
    aca_parser.add_argument("text", nargs="*", help="Event text (prompt, error, observation, ...)")
    aca_parser.add_argument("--task", type=str, default="", help="Task id to continue (recurrent context)")
    aca_parser.add_argument("--kind", type=str, default="prompt", help="Event kind: prompt, error, tool_result, file_change, test_failure, ...")
    aca_parser.add_argument("--replay", action="store_true", help="Print the replay log for --task instead")
    aca_parser.add_argument("--json", action="store_true", help="Print the full JSON result")
    aca_parser.set_defaults(func=handle_aca)

    # TUI command
    ui_parser = subparsers.add_parser("ui", help="Start the interactive terminal user interface dashboard")
    ui_parser.set_defaults(func=handle_tui)

    args = parser.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
