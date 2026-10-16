# Shadow Core plan

Status: phases 1 to 5 built. Phase 5 is the warning release: the old pieces still work and print what replaces them; removing them is a later release, with your sign-off.

Also built: decisions (a choice you state, or a yes/pick stored with the question it answered), handoff chains (the session that picks a brief up is linked to it), the Antigravity IDE as a fourth source, `tracking.ignore` / `tracking.enabled`, daily pruning, `shadow-link forget`, `sessions --rebuild`, database size in CORE MEMORY, read problems shown under AGENTS, named-session timelines for side questions, and a filter that keeps pasted terminal output and logs out of your replies.

Since then: OpenCode (its SQLite store, read-only, only the session/message/part tables) and the Gemini CLI are tracked and importable; scripted runs (`claude -p`, `codex exec`, subagents) are recorded but hidden from lists; Haiku's summary also picks out decisions; the sidebar was checked in a real Claude Code window.

Defaults taken for the open questions below: every project is tracked; digests name files without code diffs; Claude Code, Codex and Antigravity are covered, and any other agent can report through `shadow_report`.

## Goal

Shadow Link becomes one local core that every coding agent connects to. It does three jobs:

1. **Tracks** what every agent is doing: Claude Code, Codex CLI, Antigravity CLI, and anything else that can call MCP.
2. **Remembers** it, per project and across projects, in one memory with ACA as the brain.
3. **Hands work over** between agents, and only when you ask.

Shadow does not get its own app. You see it inside each agent CLI: the Claude Code sidebar, and MCP tools in the others. It never sends a prompt to an agent by itself.

## Architecture

```
 Claude Code ── plugin (sidebar, hooks) ──┐
 Codex CLI ──── session tail + MCP ───────┤
 Antigravity ── session tail + MCP ───────┼──▶  Shadow core (shadow-link daemon, localhost only)
 any MCP agent ─ MCP tools ───────────────┘       ├─ Tracker: agent events → sessions → activity
                                                  ├─ Memory: one SQLite store
                                                  │    reply pairs (prompts) + ACA graph (aca_*)
                                                  ├─ Handoff: builds packets on request
                                                  └─ API (HTTP for the plugin) + MCP server
```

**What already exists and gets reused:**
- `shadow_link/api.py`, the daemon. It was hardened in PR #3.
- `shadow_link/mcp_server.py`, which already has `shadow_activate`, `shadow_remember` and `shadow_query`.
- `shadow_link/aca/`: `CognitiveStore` (nodes, synapses, events, tasks) and `ACA.process`. These live in the same database file as the reply pairs, and `sync_from_prompts` already mirrors replies into the graph.
- The session readers in `ingester.py`: `_claude_code_sessions`, `_codex_sessions`, `_antigravity_sessions`.
- `LiveWatcher` in `live.py`, which tails Claude Code transcripts incrementally.
- The Claude Code plugin in `plugin/`.

## 1. Tracking

Each agent reports **events**. The core groups events into **sessions**: one agent, one working directory, one run.

| Agent | How events arrive | Notes |
|---|---|---|
| Claude Code | The daemon tails `~/.claude/projects/*/*.jsonl` (tool calls, files, commands and results are all in the transcript). | Built this way instead of plugin events, so a session is never counted twice; the plugin reads the result for its AGENTS list. |
| Codex CLI | The daemon tails `~/.codex/sessions/**/rollout-*.jsonl`, reusing `_codex_sessions` parsing incrementally. | Don't use Codex's `notify` setting: your config already uses its single slot for computer-use. |
| Antigravity CLI | The daemon tails `~/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl`. | Same parser as the import. |
| Any MCP agent | It calls the MCP tool `shadow.report(what, files, status)`. | Optional, for agents with no readable history. |

Generalise `LiveWatcher` into a `SessionTracker` that tails several sources at once: one cursor per file, stored in `aca_meta` so a restart resumes where it stopped.

**What gets recorded per session.** This is deterministic extraction. No model is needed.
- agent, project (working directory or repo root), session id, start and last activity, state (`active`, `idle`, `ended`)
- your requests (the existing user turns) and the agent's replies, trimmed
- files read and written, and commands run with their exit status, all taken from tool calls
- errors and failing tests, matched by pattern on tool results
- open threads: the agent's last question or offer that you haven't answered yet

## 2. Memory

One store, with the existing tables plus new ACA node types. No new database.

| Node type | One per | Content |
|---|---|---|
| `agent_session` | a run of one agent | agent, project, goal (your first request), state, timestamps |
| `activity` | a meaningful step | "edited `api.py` (security fix)", "ran pytest: 9 passed" |
| `decision` | something you settled | "use Haiku for ghosts", taken from your replies and the replies you accepted |
| `open_thread` | an unfinished item | "PR #3 awaiting merge", "categorizer over-labels rejection" |
| `handoff` | a handover you asked for | the packet, its source and target, and when it was used |
| `reaction` | a reply pair | already synced from `prompts` |

These are linked by synapses: `session → activity` (`did`), `activity → file:<path>` (`touched`), `session → project:<root>` (`in`), `handoff → session` (`from`, `to`), and `decision → project`. ACA's spreading activation then answers questions like "what touched `api.py` last week?" without a separate search index.

**Digests.** When a session goes idle, Shadow writes a short digest node: its goal, what changed, what's open. Whoever asks writes the digest. The Claude plugin uses Haiku (it already does for ghosts), and an MCP caller can summarise for itself. If neither is available, Shadow falls back to a template built from the extracted fields. The core never needs an API key.

**Hygiene:**
- Before storing anything, redact secrets: keys, tokens, `.env` values.
- Prune raw activity older than 30 days with `prune_events`. Digests, decisions and open threads are kept.
- Old sessions decay the same way reply pairs do now.

## 3. Handover (on request only)

You ask, Shadow builds a **handoff packet**, and you choose where it goes. Shadow never types into or sends to an agent.

**The packet:** goal, current state, files changed (with a one-line reason each), commands to rerun, decisions made, open threads, and the last 3 exchanges. The target is a markdown block under 2 KB.

**Ways to ask:**
- In Claude Code: the sidebar's **AGENTS** section has a `hand over` button on each session. There are also `/shadow handoff <agent or session>` and natural questions in ASK SHADOW, such as "what was Codex doing in famly AI? pick it up here". The packet goes into the prompt box, and you send it.
- In Codex or Antigravity: MCP tools `shadow.sessions()` and `shadow.handoff(from_session)`. You say "get the handoff from shadow" and the agent pulls the packet itself.
- From anywhere: `shadow-link handoff <session> --to-file` writes `.shadow/handoff.md` in the repo, ready to point any agent at.

Every handoff is stored as a `handoff` node. When the receiving session starts, Shadow links the two sessions, so later questions can follow the chain.

## 4. Surfaces

**Claude Code sidebar.** New sections, in the same green theme:
- **AGENTS:** one row per active or recent session, for example `codex · famly-ai · editing auth.ts · 4m ago`. Each row has `open` (shows the digest) and `hand over` buttons.
- **OPEN THREADS:** unfinished items across agents for the current project.
- GHOST, ASK SHADOW and CORE MEMORY stay as they are. ASK SHADOW also gets the tracker's context, so it can answer "what did Codex change today?".

**New MCP tools**, added next to the existing ones:

| Tool | Returns |
|---|---|
| `shadow.sessions(project?, agent?, active_only?)` | sessions with state and the latest activity |
| `shadow.activity(session or project, since?)` | the timeline of steps |
| `shadow.recall(question, project?)` | ACA-activated memory with the reasons it matched |
| `shadow.handoff(from_session, to_agent?)` | a handoff packet (and records it) |
| `shadow.report(what, files?, status?)` | manual tracking for any agent |
| `shadow.remember(content, type?)` | already exists, kept |

**New HTTP endpoints** for the plugin: `POST /track/event`, `GET /sessions`, `GET /sessions/<id>`, `GET /activity`, `POST /handoff`. They use the same localhost and Origin rules as the rest.

## 5. What happens to the old pieces

Nothing is deleted before Phase 5, and only then if you agree at that point.

| Piece | Plan |
|---|---|
| `tui.py` (`shadow-link ui`) | Frozen now; deprecated in Phase 5. The sidebar replaces it. |
| `proxy.py`, `proxy_runtime.py` (mitmproxy) | Frozen now; deprecated in Phase 5. Tracking uses hooks and transcripts instead of decrypting traffic. |
| `prompt_wrapper.py` (`shadow-link run`) | Deprecated in Phase 5. |
| `watcher.py` (`shadow-link watch`) and the OGI importers | Deprecated in Phase 5. |
| `shadow_hook.py` (UserPromptSubmit) | Folded into the plugin in Phase 2, then retired. |
| `embed_server.py`, `generate.py` | Kept as optional local-model backends. |

## 6. Phases

Each phase ends with tests passing and something you can see working.

1. **One memory.** Run `sync_from_prompts` on startup and after imports; add the node types above. Fix the leftover review items: threaded HTTP server, an atomic `add_prompt_unique`, and the categorizer.
   *Done when* ACA recall finds reply pairs and imported sessions together.
2. **Tracking.** Add `SessionTracker` for Claude Code, Codex and Antigravity. Add the plugin's `tool.call` reporting and `POST /track/event`. Add the session and activity nodes, plus secret redaction.
   *Done when* `GET /sessions` lists a Codex run and this Claude session live, with the files each touched.
3. **Sidebar AGENTS + digests.** Add the AGENTS and OPEN THREADS sections, digests written on idle, and ASK SHADOW answering from the tracker.
   *Done when* you can ask "what did Codex do today?" in Claude and get a correct answer.
4. **Handover.** Add packet building, `/shadow handoff`, the sidebar button, the MCP tools in Codex and Antigravity (registered in `~/.codex/config.toml` and `~/.gemini/antigravity/mcp_config.json`), and `--to-file`.
   *Done when* work started in Codex can be continued in Claude from one packet, and the reverse.
5. **Retire the old pieces.** Give the deprecated commands a warning release first, then remove them in the release after, with your sign-off.

## 7. Risks

- **Transcript formats change.** Codex and Antigravity don't publish their formats. Each reader gets fixture tests and fails soft: if one agent's history can't be read, that agent is skipped and the sidebar shows it.
- **Secrets in transcripts.** Redact before storing, keep the daemon on localhost (already enforced), and add a `shadow-link forget <session|project>` command.
- **Storage growth.** Prune raw events, keep digests, and show the database size in CORE MEMORY.
- **Model cost.** Digests and ghosts use Haiku, once per session idle and once per turn. Both can be turned off in `/config`.
- **Scope creep toward autopilot.** Handover stays pull-only: a packet you send, or one an agent fetches when you tell it to.

## Open questions

1. Should Shadow track all projects, or only ones you opt in (for example with a `.shadow` file in the repo)?
2. Should digests include code diffs, or only file names and reasons?
3. Do any other agents need to be covered now? OpenCode and the Gemini CLI are now covered; Cursor and Aider have no history on this machine.
