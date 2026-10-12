# Associative Context Activation (ACA)

> Something happens. The system does not ask *"what should I search for?"*
> It asks *"what does this activate?"*

ACA replaces `question → search → top result → answer` with a recurrent loop
that activates, connects, expands, compares, suppresses and converges context
before committing to a decision. The goal is not maximum context. It is the
**smallest, strongest connected context field** that explains the situation.

```
event ─► seeds ─► spreading activation ─► competition + inhibition
   ▲                                              │
   │                                    context pull + convergence
   │                                              │
   │                                        Working Brain
   │                                              │
   │                                         state builder
   │                                              │
   └── propagation ◄── firing policy ◄── decision neurons (Jev-shaped)
```

Code lives in `shadow_link/aca/`:

| File | Responsibility |
|---|---|
| `store.py` | Cognitive Store (typed nodes), Synapse Graph (typed, learnable relationships), replay log, task state, corpus bridge |
| `activation.py` | Seed detection, spreading activation, competition + inhibition, context pull, convergence, provenance |
| `neurons.py` | Noul / Choice / Score questions, Jev state builder, heuristic + Jev backends, firing thresholds |
| `brain.py` | The ACA loop, Working Brain, hypotheses, contradictions, learning loop, memory formation, episodes, inspector |

## How each stage works

**Cognitive Store.** Nodes carry `type` (memory, file, function, error, fix,
decision, requirement, hypothesis, episode, semantic, procedure, finding,
test_result, …), `importance`, `trust`, `status` (active, temporary,
superseded, incorrect, refuted, archived), `project`, `source`, usage
counters and metadata. The existing reaction-pair corpus is mirrored in
automatically. Every `prompts` row becomes a `reaction` node (`P-<id>`) and
its Jaccard edges become `similar_to` synapses, so what Shadow Link has
already learned is the starting graph.

**Synapse Graph.** Each relationship is directed and typed (`caused_by`,
`occurred_in`, `fixed_by`, `related_to`, `supports`, `refutes`,
`contradicts`, `supersedes`, `derived_from`, `involved_in`, `co_active`,
…). It stores strength, confidence, frequency, success/fail counts, source,
created/last-used times. Symmetric relations are stored both ways.

**Seed detection.** In order of strength, seeds come from:
1. explicit node references on the event
2. entity matches (file paths, `*Error` types, identifiers, quoted terms)
   against node titles
3. lexical containment of the event's content terms, with 4-character
   prefix matching (`auth` ~ `authentication`) and a polarity penalty
4. activation carried over from the Working Brain

**Spreading activation.** Activation spreads in layers out to `max_depth`.
Each hop transmits:

```
a(u) · hop_decay(0.82) · efficacy(synapse) · fan_damping · (1 + 0.3·attention(v))
efficacy = strength · confidence · recency · reliability · frequency
reliability = 0.5 + (success+1)/(success+fail+2)
```

Several paths into the same node combine by noisy-OR, so they add up but
never exceed 1. Hub nodes lend little to any one neighbour (fan damping,
in the style of ACT-R). `supersedes` and `refutes` send *inhibition*
instead of activation. The best path to every node is recorded.

**Competition + inhibition.** Score = activation × freshness × importance
× trust × learned usefulness × (1 − inhibition). Suppressors, each recorded
as a reason:
- status (superseded, incorrect, refuted)
- inhibitory synapses
- stale
- low trust
- other project
- duplicates
- inhibited by the Working Brain
- outcompeted by the leader

The result is three states: active, latent and suppressed.

**Convergence.** A greedy MMR pick under `max_selected` and `max_chars`
balances score against redundancy. Contradiction partners always come in
together. When exploring, one slot goes to the best latent node from a
different neighbourhood. The output includes a funnel:
`graph_nodes → activated → significant → highly_relevant → critical`.
Every pulled item carries `why`: the seed, the seed reason, the path with
relation types, the factor breakdown, why it was selected, its conflicts,
and its provenance chain.

**Decision neurons.** Each cycle builds one state object (`current_task`,
`active_context`, `memories`, `hypotheses`, `tool_results`,
`previous_failures`, `project_constraints`, `conflicts`, `funnel`,
`metacognition`, `agents`). Small typed questions are evaluated against it:

- `should_expand`, `context_conflict`, `needs_verification`,
  `context_too_large` and `stuck` (Noul)
- `next_action` and `wake_agent` (Choice)
- `remember` (Score)
- `relevant:<node>` for each pulled item (Noul)

`FiringPolicy` maps each probability to silent / latent / activate /
strong / fire, with per-question thresholds. `destructive` needs 0.97 to
fire.

**Propagation (recurrence).** Fired signals reshape the next cycle:
- `should_expand` raises the budget and seeds from latent nodes.
- `relevant:X` going silent inhibits X.
- `next_action=explore_alternative` reserves an exploration slot.

The loop stops when nothing new fires, when the selection stops changing,
or when `max_cycles` is reached.

**Hypotheses.** These are nodes with a confidence. Evidence moves that
confidence in log-odds space, scaled by the evidence's trust
(`±1.5·weight·trust`). A hypothesis below 0.05 is eliminated (status
`refuted`). Evidence links (`supports` / `refutes`) take part in spreading.

**Contradictions.** These are first-class: a `contradicts` synapse, found
automatically when two nodes overlap heavily but have opposite polarity.
Nothing is overwritten. Both sides activate together, and the
`context_conflict` neuron fires.

**Learning loop.** `feedback(task, node, useful)` adjusts every synapse on
the path that activated the node: `s += lr·(1−s)` if useful,
`s −= lr·s` if not. It also updates the node's usefulness counts, links
co-useful nodes with `co_active`, and inhibits useless nodes for the rest
of the task.

**Memory formation.** The `remember` Score neuron decides store, temporary
or discard for observations, errors, tool results and findings. Plain
prompts are never stored unless the caller passes `remember=True`, so the
graph does not fill with your own questions; `remember=False` stores
nothing. Temporary memories that are never used are pruned after 48 h. New
memories are wired to whatever was co-active with them.

**Corpus mirror.** Each event first syncs the reply corpus: new replies are
added, and replies deleted from the corpus (or wiped by `shadow-link clear`)
are removed from the graph with their links.

**Episodes → semantic / procedural memory.** `close_task` stores an
episode linked to the context and hypotheses that mattered. It
consolidates the lesson into a `semantic` node and, optionally, a
`procedure` node, each `derived_from` the episode, so later tasks
reactivate them.

**Global attention.** Activation feeds an exponential moving average
across tasks, and that average biases where spreading goes.

**Replay + inspector.** Every seed, traversal, suppression, convergence,
judgment, inhibition, expansion, contradiction, hypothesis update, memory
decision, feedback and agent wake-up is written to `aca_events`.
`inspect(task, node)` answers "why is this active?". `snapshot(task)`
returns the Working Brain panel. Both are read-only. Events older than 30
days are pruned, at most once an hour.

## Jev

`JevBackend(client)` sends the state and the question list
(`Question.to_dict()`) to a client function you provide. It fans batches
out in parallel. Any question the client doesn't answer, answers with a
malformed result, or fails on (an exception) falls back to the heuristic
backend, and the problem is logged. The TypeSafe wire format is **not hard-coded**: the
official API reference (docs.typesafe.ai) couldn't be reached while this
was written. Wrap the official SDK or API in a function that returns
`{name: {"noul": p} | {"choice": {opt: p}} | {"score": v, "levels": {...}}}`
and pass it in:

```python
from shadow_link.aca import ACA, CognitiveStore, JevBackend
aca = ACA(CognitiveStore("shadow_link.db"), backend=JevBackend(my_jev_client))
```

Without a client, `HeuristicBackend` answers the built-in neurons from
measurable features: scores, trust, conflict count, funnel size, hypothesis
margins, failure streak and repetition. It is deterministic. It is not a
language model, and it leaves unknown questions unanswered rather than
guessing.

## Using it

```powershell
shadow-link aca "why did auth break again?" --task auth-1
shadow-link aca "RefreshTokenExpired in refresh-token.ts" --kind error --task auth-1
shadow-link aca --replay --task auth-1
```

API (daemon): `POST /aca/event | node | link | feedback | hypothesis | evidence | action | close`,
`GET /aca/brain | inspect | replay | stats` (query params `task_id`, `node_id`).

MCP: `shadow_activate`, `shadow_remember`, `shadow_aca_feedback`.

## Status against the plan

| # | Area | Status |
|---|---|---|
| 1 | ACA from any event kind | **Built**. Events are typed (prompt, error, tool_result, file_change, test_failure, finding, …) |
| 2 | Cognitive Store | **Built** |
| 3 | Synapse Graph with properties | **Built**. Strength, confidence, frequency, recency, direction, type, success/fail, source, contradiction |
| 4 | Seed detection | **Built**. Lexical, prefix and entity matching only; embeddings are not yet used for seeding |
| 5 | Spreading activation | **Built** |
| 6 | Competition | **Built** |
| 7 | Inhibition | **Built** |
| 8 | Context pull with "why" | **Built** |
| 9 | Convergence + funnel | **Built** |
| 10 | Working Brain | **Built**. Goal, active nodes, hypotheses, signals, inhibitions, actions |
| 11 | Jev state builder | **Built** |
| 12–13 | Decision neurons, parallel firing | **Built**. Heuristic backend; Jev through an adapter you supply |
| 14 | Per-question thresholds | **Built** |
| 15–16 | Synaptic propagation, recurrence | **Built** |
| 17 | Hypothesis network | **Built** |
| 18 | Contradiction network | **Built**. Detection is polarity-based; deeper semantic contradictions need a model |
| 19 | Memory formation | **Built** |
| 20–21 | Reinforcement, decay | **Built** |
| 22–24 | Episodic, semantic, procedural memory | **Built**. The lesson is supplied by the caller; it is not extracted automatically |
| 25 | Agent swarm activation | **Partial**. `wake_agent` emits a signal; no agent runtime is launched |
| 26 | Shared cognitive fabric | **Partial**. One shared store and API; agents must call it |
| 27 | Global attention | **Built** |
| 28 | Metacognition | **Partial**. Repetition, fail streak, expand, too-large and conflict signals |
| 29 | Firing Brain visualisation | **Not built**. The replay log and snapshot are its data source |
| 30 | Brain Inspector | **Built** (API/CLI data; no TUI panel yet) |
| 31 | Context replay | **Built** |
| 32 | Context budget | **Built**. Nodes, depth, seeds, chars, cycles, neurons; spends more when expanding |
| 33 | Exploration vs exploitation | **Built**. Exploration slot |
| 34 | Evidence provenance | **Built**. `derived_from` / `discussed_in` / `observed_in` chains |
| 35 | Trust / confidence | **Built**. Defaults by type, used in scoring and in hypothesis updates |
| 36 | ACA learning loop | **Built** (explicit feedback; usefulness is not yet inferred automatically) |

Not yet wired: the proxy and the TUI still use the original matcher. ACA is
reachable through the CLI, the API and MCP, and it runs on the same
database.
