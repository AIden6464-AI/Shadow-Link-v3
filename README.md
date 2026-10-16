# Shadow Link v3

<p align="center">
  <img src="docs/assets/shadow-link-wordmark.png" alt="Shadow Link wordmark" width="760">
</p>

<p align="center">
  <strong>The shadow of you.</strong><br>
  Local. Terminal-native. Operator-controlled.<br><br>
  <a href="https://github.com/ThatReid23/Shadow-Link-v3">Repository</a> ·
  <a href="https://github.com/ThatReid23/Shadow-Link-v3/issues">Issues</a>
</p>

A local, terminal-native cognitive mirror for AI coding workflows.

Shadow Link learns situation → reply pairs from your past AI conversations, predicts how you are likely to respond, and surfaces those predictions as editable "ghost" replies. It includes a polished terminal UI, live Claude Code session mirroring, an MCP server, file watchers, and an optional Claude Code proxy.

## Disclaimer

Shadow Link can read local conversation history and, when proxy mode is enabled, decrypt Claude Code traffic through mitmproxy. Review the configuration before running it, keep the daemon bound to localhost, and use it at your own risk.

This is a personal workflow tool. Predictions are suggestions—not authorization to take actions on your behalf.

## Install

Requires Python 3.10+.

Clone and install:

```powershell
git clone https://github.com/ThatReid23/Shadow-Link-v3.git
cd Shadow-Link-v3
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

This installs the `shadow-link` command.

On Windows, if mitmproxy fails to install:

```powershell
pip install --only-binary :all: mitmproxy
```

## Shadow in Claude Code (recommended)

Shadow lives in a sidebar beside the conversation. After each Claude turn it shows the reply you would likely send, also as dim text in the chat bar (Tab to take it). It explains its guess, and it answers side questions about the session without adding anything to Claude's conversation.

- **Fullscreen layout** (`/tui fullscreen`, 110+ columns): the Shadow sidebar docks on the right and opens by itself.
- **Default layout**: a two-row strip above the chat bar shows the guess; `more` (or `/shadow`) opens the full panel above the prompt.

After each turn a Claude model (Haiku by default, `Ghost model` in `/config`; `off` for past replies only) drafts the reply in your voice from your closest past replies and your latest messages.

Install it from inside Claude Code (version 2.1.275 or later):

```
/plugin install shadow-link --marketplace AIden6464-AI/Shadow-Link-v3
```

Answer `y` to add the marketplace and pick the user scope. Then keep the daemon running in another terminal:

```powershell
shadow-link daemon
```

| In Claude Code | What it does |
|---|---|
| Tab | Take the ghost shown in the chat bar. |
| `use` / `next` / `why` / `wrong` | Buttons in the sidebar (click, or focus it with ctrl+x Tab and press `u` `n` `w` `x`). The default-layout strip has `use` / `next` / `wrong` / `more`. |
| **Ask Shadow** box | Ask about the session. Claude never sees it; the answer shows in the sidebar with **send to Claude**. |
| `/shadow` | Open the sidebar. `/shadow <question>` asks from the chat bar; `/shadow status` checks the daemon link. |

Shadow learns from what you actually send: a sent ghost is reinforced, `wrong` down-weights it, and every reply you type is saved as a reaction pair. Side questions run through your own Claude Code session (`$.model.fork` over the session transcript, served from the prompt cache), so no extra API key is needed. Settings (daemon URL, minimum confidence, chat-bar ghost on or off) are in `/config`.

The plugin lives in [`plugin/`](plugin/).

## Quick start

Start the daemon in one terminal:

```powershell
shadow-link daemon
```

Open the TUI in another:

```powershell
shadow-link ui
```

Type `/` to open the command palette. Use `/claude-shadow` for the full-screen Claude Code mirror.

To bootstrap the memory corpus from a Claude.ai export:

```powershell
shadow-link ingest-claude-export path\to\conversations.json
```

Or from your local coding-agent histories (Claude Code, Codex CLI, Antigravity CLI). It is safe to re-run; it only adds new chats:

```powershell
shadow-link ingest-sessions
shadow-link ingest-sessions --only codex agy
```

## CLI commands

| Command | What it does |
|---|---|
| `shadow-link ui` | Open the connected terminal UI. |
| `shadow-link daemon` | Start the local API and live transcript watcher. |
| `shadow-link daemon --proxy` | Start the daemon and managed Claude Code proxy. |
| `shadow-link stats` | Show corpus and bucket counts. |
| `shadow-link query <text>` | Search stored replies. |
| `shadow-link interactive` | Run the fast-reply simulator. |
| `shadow-link ingest-claude-export <file>` | Import reaction pairs from a Claude.ai export. |
| `shadow-link ingest-sessions [--only claude codex agy]` | Import reaction pairs from Claude Code, Codex CLI and Antigravity CLI histories. |
| `shadow-link ingest-jsonl <file>` | Import an OGI/oneiros trace log. |
| `shadow-link ingest-human <dir>` | Import paired question/response files. |
| `shadow-link ingest-md <file>` | Import a Markdown prompt log. |
| `shadow-link watch <dir>` | Watch for question files and write ghost/response files. |
| `shadow-link run <command...>` | Wrap another CLI with inline ghost suggestions. |
| `shadow-link proxy` | Run the mitmproxy addon in the foreground. |
| `shadow-link mcp` | Run the stdio MCP server. |
| `shadow-link aca <text> [--task id] [--kind error]` | Associative Context Activation: show what an event activates, why, and the decision signals. |
| `shadow-link aca --replay --task <id>` | Replay everything ACA did for a task. |
| `shadow-link clear` | Clear the corpus after confirmation. |
| `shadow-link --help` | Show CLI help. |

## TUI commands

Type `/` in `shadow-link ui` to open the themed command palette.

| Command | What it does |
|---|---|
| `/status` | Show daemon, models, watcher, proxy, and corpus health. |
| `/stats` | Show corpus, graph, bucket, decay, and weight statistics. |
| `/query <text>` | Search stored reply text. |
| `/predict <situation>` | Predict your likely response. Plain text does the same thing. |
| `/ingest <situation> \| <reply>` | Teach a situation → reply pair. |
| `/pairs [count]` | Browse recent learned pairs. |
| `/live` | Show watcher state, the active ghost, and recent activity. |
| `/claude-shadow` | Open the animated full-screen Claude Code mirror. |
| `/proxy status` | Show proxy state. |
| `/proxy start [port]` | Start the managed proxy. |
| `/proxy stop` | Stop the managed proxy. |
| `/proxy view [count]` | Show recent proxy traffic and token usage. |
| `/proxy setup [port]` | Start the proxy and create `claude-proxy.ps1`. |
| `/accept [id]` | Use and reinforce a ghost. |
| `/reject [id]` | Reject and down-weight a ghost. |
| `/edit <text>` | Approve a ghost after reshaping it. |
| `/fix <text>` | Reject a ghost and store your correction. |
| `/refresh` | Reconnect to the daemon. |
| `/clear` | Clear the terminal. |
| `/help` | Show the command guide. |
| `/exit`, `/quit` | Exit the TUI. |

Inside `/claude-shadow`:

| Key | Action |
|---|---|
| `a` / `r` | Accept or reject the selected ghost. |
| `e` / `f` | Edit or correct the selected ghost. |
| `n` / `p` | Cycle through ghost alternatives. |
| `s` | Switch Claude Code sessions. |
| `V` | Toggle the full Claude response viewer. |
| `P` | Start or stop the proxy. |
| `q` | Return to the main TUI. |

## Claude Code proxy

The optional proxy captures reaction pairs and records local request/usage audit data. It never edits earlier turns, so prompt caching and Claude's thinking-block history check keep working. Injecting Shadow Link context through the proxy is off by default (`proxy.inject_nudges`); the `UserPromptSubmit` hook (`shadow_hook.py`) is the preferred channel for that.

Easiest setup:

```text
/proxy setup
```

Then run the generated PowerShell launcher:

```powershell
& .\claude-proxy.ps1
```

Manual setup:

```powershell
$env:HTTPS_PROXY = "http://127.0.0.1:8080"
$env:NODE_EXTRA_CA_CERTS = "$HOME\.mitmproxy\mitmproxy-ca-cert.pem"
$env:NO_PROXY = "localhost,127.0.0.1,::1"
claude
```

The proxy only targets Anthropic message requests carrying the `claude-cli` user agent.

## Configuration

Create `config.json` in the project root. Environment variables override JSON values.

Minimal local configuration:

```json
{
  "database": { "path": "shadow_link.db" },
  "embeddings": {
    "server_url": "http://localhost:1235/v1/embeddings",
    "model": "",
    "provider": "lmstudio",
    "use_on_hot_path": false
  },
  "chat": {
    "server_url": "http://localhost:1234/v1/chat/completions",
    "model": "local-model",
    "lm_mode": true
  },
  "server": { "host": "localhost", "port": 5000 },
  "proxy": {
    "daemon_url": "http://localhost:5000",
    "port": 8080,
    "inject_nudges": false,
    "learn": true
  }
}
```

Set `SHADOW_LM_MODE=0` to disable generated replies and use retrieval only. Set `SHADOW_QUERY_EMBED=1` to enable semantic embeddings on prediction paths.

The included `embed_server.py` requires `torch` and `transformers`:

```powershell
pip install torch transformers
python embed_server.py
```

You can also point Shadow Link at LM Studio, Ollama, llama.cpp, or another OpenAI-compatible local endpoint.

## MCP tools

Run the server with:

```powershell
shadow-link mcp
```

It exposes:

- `shadow_log` — store a reaction pair.
- `shadow_query` — search stored replies.
- `shadow_nudge` — retrieve behavioral reminders.
- `shadow_draft` — predict a likely reply.
- `shadow_accept` — reinforce a used prediction.
- `shadow_stats` — inspect corpus health.
- `shadow_activate` — Associative Context Activation for an event.
- `shadow_remember` — store a typed cognitive object (decision, error, fix, …) with links.
- `shadow_aca_feedback` — tell ACA whether pulled context helped (reinforces the path).

## Associative Context Activation

ACA asks *"what does this event activate?"* instead of *"what should I search for?"*. It seeds a typed cognitive graph, spreads activation, suppresses stale/duplicate/superseded/irrelevant nodes, converges to a small explained context, fires Jev-shaped decision neurons, and loops. See [`docs/aca.md`](docs/aca.md).

## API

`shadow-link daemon` starts the JSON API at `http://localhost:5000` by default. The TUI, proxy, watcher, and external agents use this API for predictions, feedback, ingestion, live state, and proxy control.

The API has no authentication and uses permissive CORS. Do not expose it to other machines or untrusted browser content.

## Testing

```powershell
python tests\test_full.py
```

The suite covers the matcher, database, MCP server, capture hook, subprocess wrapper, Claude Code proxy, API, TUI, ACA, and ghost decay.

## Notes

- Local data is stored in `shadow_link.db`.
- Proxy audit logs are stored in `claude_audit/`.
- Embeddings and chat inference are local unless you configure remote endpoints.
- The project is primarily developed for Windows Terminal and PowerShell.
- Architecture details: [`docs/architecture.md`](docs/architecture.md).
- Associative Context Activation: [`docs/aca.md`](docs/aca.md).
- Build blueprint: [`docs/SHADOW_LINK_BUILD_BLUEPRINT.md`](docs/SHADOW_LINK_BUILD_BLUEPRINT.md).

## License

MIT — see [LICENSE](LICENSE).

Repository: [github.com/ThatReid23/Shadow-Link-v3](https://github.com/ThatReid23/Shadow-Link-v3)  
Issues: [github.com/ThatReid23/Shadow-Link-v3/issues](https://github.com/ThatReid23/Shadow-Link-v3/issues)
