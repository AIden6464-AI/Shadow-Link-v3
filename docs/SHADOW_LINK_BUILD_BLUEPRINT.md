# Shadow Link — Build Blueprint

> *"The shadow of you."* A digital entity that mirrors and anticipates how **you**
> talk to AI, so you stop drafting corrections and start accepting them.

---

## 0. About this document

**Who this is for:** any human engineer or AI model who wants to build Shadow Link
from scratch. You do **not** need access to the original prototype code, and this
document deliberately contains **no file paths, no class names, and no language
bindings.** It describes *the idea* — the concepts, the math, the data model, the
UX, and the rationale — at a level you can implement in any language on any stack.

**What this is NOT:** it is not a reverse-engineering of the existing prototypes
(there is a Python implementation, a deprecated Go TUI, and a separate MITM
"prompt-cache" proxy). Those are *one* realization of the idea. This blueprint is
the idea itself, cleaned of implementation accidents and internal contradictions.

**How to read it:** Sections 1–2 are the vision and the shape of the system.
Section 3 is the **core engine** — the must-build spine. Section 4 is the UX.
Section 5 is the agentic layer Shadow Link rides on top of. Section 6 is the
end-to-end workflow. Section 7 is a build roadmap. Section 8 is the design-rationale
ledger. Section 9 separates **what's core from what's aspirational** — read it before
you scope. Section 10 is a glossary.

Throughout, anything tagged **[CORE]** belongs in the MVP spine. Anything tagged
**[ASPIRATIONAL]** is researched-but-speculative — build it later, or never.

---

## 1. The vision

### 1.1 The metaphor

Borrowed from *The Legend of Zelda*: **Shadow Link** is a mirrored entity that moves
as you move. Here it is a *cognitive mirror* — a middleware layer that learns the
distinctive "shape" of how a specific person reacts to AI output, and then **predicts
their next reaction before they type it.**

### 1.2 The problem it solves

Working with an AI agent is a loop of *generative drafting*: the AI produces
something, you read it, and you draft a correction ("too bloated, strip it", "that's
the v2 endpoint, not v1", "remember we're targeting GCP now"). You do this dozens of
times a day, and you re-explain the same preferences across every new conversation.

Three frictions drive the project:

1. **Cognitive load** — composing corrective feedback over and over is fatiguing.
2. **Token / iteration cost** — every round-trip of "no, not like that" burns tokens
   and time.
3. **Style amnesia** — generic models don't know *how you talk*, so you keep
   re-teaching them your structure, tone, and standing rules.

### 1.3 The north star

> Shift the operator's role from **generative drafting** to **single-stroke
> verification.**

Instead of writing the correction, you see it pre-drafted inline as a dimmed "ghost"
suggestion and accept it with one keystroke (Tab). The system learns from what you
accept, edit, or reject, and gets better at being you. The end-state is a layer that
sits on top of whatever agentic CLI / local LLM you already use and makes the
human side of the loop nearly frictionless.

---

## 2. System shape

Shadow Link is **middleware**, not a model. It sits *between* you and an agent you
already run. There are three conceptual planes; only the first is Shadow Link proper.

```
            ┌──────────────────────────────────────────────┐
   YOU ───► │  SHADOW LINK  (the behavioral mirror)         │
            │  • learns your "shapes" from past reactions    │
            │  • predicts your next reaction (ghost reply)   │
            │  • one-keystroke accept / edit / reject        │
            └───────────────┬──────────────────────────────┘
                            │ optimized prompt / accepted reply
                            ▼
            ┌──────────────────────────────────────────────┐
            │  AGENTIC SUBSTRATE  (the "doer")              │
            │  e.g. an anti-gravity-style CLI, or a custom  │
            │  Body/Mind/Soul agent — see §5                │
            └───────────────┬──────────────────────────────┘
                            │ chat / completion calls
                            ▼
            ┌──────────────────────────────────────────────┐
            │  LOCAL LLM RUNTIME  (LM Studio / Ollama / …)  │
            │  OpenAI-compatible endpoint, runs on YOUR box │
            └──────────────────────────────────────────────┘
```

Two important scoping notes:

- **The substrate is pluggable.** Shadow Link's job is to model *you*, not to be the
  agent. The stated target substrate is a local-LLM CLI you drive day-to-day. A
  full self-improving agent (§5) is *one* thing it can wrap, but Shadow Link does not
  require it and the substrate need not know Shadow Link exists.
- **Cost/cache optimization is a separate, optional plane.** Because everything is
  local-first, you can also insert a transparent layer that rewrites outbound LLM
  payloads to keep a stable, cacheable prefix (see §6.4). Treat it as an independent
  utility, not part of the core engine.

---

## 3. The core engine — modeling "you" **[CORE]**

This is the spine. Build this first. Everything else is presentation or substrate.

### 3.1 The central idea: reactions have *shape*

Every reaction you give an AI ("strip the boilerplate", "you missed the second
endpoint", "let's pivot to GCP") has a recurring structure — a signature in its
vocabulary, syntax, and intent. Represent each past reaction as a point in a
similarity space. When a new situation arises that is *close* to past situations,
retrieve the reactions you gave then and offer them as predictions.

You are building, in effect, a **predictive cache of the user keyed by linguistic
similarity.**

### 3.2 What gets stored — the unit of memory

The atom of memory is a **reaction record**:

| Field            | Meaning                                                              |
|------------------|----------------------------------------------------------------------|
| `situation`      | The context that prompted the reaction (recent turns + agent output).|
| `reaction_text`  | What the user actually said/typed in response.                       |
| `buckets`        | One or more behavioral categories (§3.5). Non-exclusive.             |
| `created_at`     | When it was first recorded.                                          |
| `last_triggered` | When it was last retrieved/reinforced (drives decay, §3.6).          |
| `weight`         | Base salience; rises when accepted, decays over time.                |
| `token_shape`    | Precomputed token set for fast matching (§3.3).                      |
| `embedding`      | Optional cached semantic vector (§3.4).                              |

Records are mined from two sources: **historical chat exports** (cold-start corpus)
and **live capture** of the user's reactions as they work.

### 3.3 Tokenization — turning text into a "shape" **[CORE]**

Matching quality is dominated by *how you tokenize*, not by the similarity formula.
The pipeline:

1. **Normalize** — Unicode-normalize (NFKC) and lowercase, so `Reject` and `reject`
   are one token.
2. **Clean** — strip punctuation/markup that doesn't carry meaning.
3. **Split** — extract word tokens (preserving contractions like `don't`).
4. **Build a hybrid token set** — combine, into a single set:
   - word unigrams,
   - word **bigrams** and **trigrams** (capture short phrases / cadence),
   - **character n-grams** (default length 3) for spelling/morphology robustness.
5. **Preserve negation — do not treat negators as stopwords.** Words like
   *not, never, no, don't, avoid, without, neither, nor* must survive tokenization
   and be flagged. This is load-bearing: "do **not** use Docker" and "use Docker"
   share almost every token, and stripping the negator makes the matcher invert the
   user's meaning. Keep negators, and treat them as a strong signal that flips the
   sense of a match.

**Why hybrid (and not just one tokenization)?** Empirically, a combined
"all-of-the-above" token set is the most accurate (see the benchmark in §3.8). Word
features give phrasal *precision*; character + structural features give a recall
*cushion* so near-misses in spelling or word order still match.

### 3.4 Lens 1 + Lens 2 — the dual-lens matcher **[CORE]**

Use **two complementary similarity lenses**, by analogy to a dual-camera system
(a wide-angle lens for breadth + a telephoto lens for depth):

- **Lens 1 — Lexical (Jaccard).** Treat each text as its token set; similarity is the
  overlap ratio:

  ```
  J(A, B) = |A ∩ B| / |A ∪ B|
  ```

  Fast, interpretable, no network, no model. Blind to synonymy (it can't tell that
  "too bloated" ≈ "strip the boilerplate").

- **Lens 2 — Semantic (embeddings).** Embed the text with a small local embedding
  model and compare with cosine similarity. Catches same-intent / different-words
  matches. Costs a model call, so use it sparingly.

> **Distance form for indexing:** `d(A,B) = 1 − J(A,B)` is a true metric (obeys the
> triangle inequality), which lets you put reactions in metric/ANN index trees later
> if the corpus grows.

#### How to combine the two lenses — pick one, know the others

This is the single most contradiction-prone decision in the design. There are three
valid strategies; **choose the cascade as your default** and treat the others as
upgrades:

1. **Cascade / two-pass (RECOMMENDED DEFAULT, lowest latency).**
   Run cheap Lens-1 Jaccard over *all* candidates first. Only if the best lexical
   match is ambiguous (or you explicitly want semantic confirmation) do you embed the
   query once and run Lens-2 cosine over the *top-k* lexical candidates. The hot path
   stays network-free; embeddings are an opt-in second pass. If the embedding backend
   is unavailable, you gracefully degrade to pure lexical. This matches the
   "instant literal match, semantic safety-net" intuition and the latency budget in
   §4.

2. **Weighted blend with adaptive α (single combined score).**
   When you need one number per candidate:

   ```
   score = α · Jaccard + (1 − α) · cosine
   ```

   Make **α shrink as the query gets longer**: short queries are dominated by exact
   words, so trust lexical; long queries have more room for paraphrase, so trust
   semantics. A workable curve is `α = α₀ · e^(−γ · L)` with `α₀ ≈ 0.4`, `γ ≈ 0.05`,
   `L` = query token count. Tune on your own data.

3. **Reciprocal Rank Fusion (RRF) — zero-tuning ranked merge.**
   If you maintain *two separate retrievers* (a sparse/lexical one and a dense/vector
   one) and just want to merge their ranked lists without calibrating score scales:

   ```
   RRF(d) = Σ_retriever  1 / (k + rank_retriever(d))     with k ≈ 60
   ```

   Robust and needs no labeled data; best when you don't trust your score scales to
   be comparable. Use this if/when you adopt a real two-retriever architecture.

**Tradeoff summary:** cascade = cheapest + lowest latency (start here); adaptive
blend = one tunable score (needs calibration); RRF = scale-free merge (needs two
retrievers). They are *different algorithms* — do not silently mix them.

### 3.5 Behavioral buckets — non-exclusive taxonomy **[CORE]**

Classify each reaction into one or more of these buckets. **A reaction can belong to
several at once** — model it as multi-label (independent binary classifiers), not a
single category.

| Bucket          | What it is                                              | Example |
|-----------------|----------------------------------------------------------|---------|
| **Request**     | A direct task / procedural demand.                       | "Generate a deploy script for AWS." |
| **Reminder**    | A standing rule, constraint, or style guideline.         | "Always run tests before proposing a PR." |
| **Rejection**   | A veto / strip-it-out / refactor-away.                   | "Too bloated, strip the boilerplate." |
| **Correction**  | A factual or structural fix.                             | "The endpoint is v2, not v1." |
| **Pivot**       | A strategic redirection / scope shift.                   | "Stop on AWS — target GCP instead." |

Example of multi-label: *"The DB port is wrong, fix it and remember to update the env
template"* → **Correction** (fix the port) **+ Reminder** (update env going forward).

**Why a graph/tag-cloud, not folders:** rigid folders force false either/or choices
("is this a Rejection or a Correction?"). Store reactions as **nodes**, attach
multiple bucket **tags**, and connect nodes with weighted **edges** (edge weight =
similarity). This mirrors how human feedback actually overlaps, and it lets you do
theme discovery (§3.7).

### 3.6 Per-bucket thresholds and temporal decay **[CORE]**

Two knobs make retrieval feel intelligent: *how strict the match must be*, and *how
fast old reactions fade*. **Both vary by bucket**, and that variation is the point.

**Per-bucket match threshold.** Don't use one global cutoff — the *cost of a wrong
match differs by bucket*:

- **Rejection / Correction → strict.** A false positive here actively inverts the
  user's meaning (especially with negation), so demand a high-confidence match.
- **Reminder / Request → moderate.** These are about structural alignment; a
  moderate bar is fine.
- **Pivot → lenient.** Strategic redirections are looser in phrasing; allow a softer
  match.

> **On the actual numbers — read this carefully.** The original plan guessed a single
> "60% fuzzy match" cutoff. Benchmarking (§3.8) then showed that on a 3-turn,
> hybrid-tokenized score the *optimal global* cutoff is much lower — around **0.30–
> 0.35** — because longer contexts deflate Jaccard scores (the "context collapse"
> effect, §3.8). Production then abandons a single cutoff entirely in favor of the
> per-bucket ordering above. **The lesson for you:** the absolute threshold is an
> artifact of *your* tokenization, context length, and score normalization — so
> **calibrate it empirically against labeled examples**, and keep the *relative*
> ordering (rejection/correction strictest → pivot loosest). Don't hard-code 0.60.

**Temporal decay.** A reaction's effective salience fades over time unless reinforced:

```
W_effective(t) = W_base · e^(−λ · Δt)
```

`Δt` = time since the reaction was last triggered/reinforced; `λ` = a per-category
decay rate derived from a half-life (`λ = ln 2 / half_life`). Accepting a prediction
**reinforces** the node: bump its weight and reset its decay clock.

Use **category-aware half-lives**, because different kinds of feedback age
differently:

| Decay class | Buckets                  | Half-life (reference) | Why |
|-------------|--------------------------|------------------------|-----|
| **Permanent** | Reminder                 | ∞ (no decay)           | Architectural/style rules are evergreen. |
| **Tactical**  | Request, Pivot           | ~12 h                  | Useful for a while, then context moves on. |
| **Ephemeral** | Rejection, Correction    | ~1.5 h                 | Often one-off, tied to the moment. |

(Half-lives are starting points — tune to the user's rhythm.)

### 3.7 Theme discovery — the background distiller **[CORE-ish]**

Because reactions form a weighted graph, you can periodically cluster it (e.g. find
connected components among edges above a similarity floor like ~0.30) to surface
**recurring themes** the user hasn't named — "you keep rejecting deeply nested loops",
"you always ask for type hints". A background process distills these into an updated,
human-readable **preference summary** that can be injected into the agent's system
context. This is what turns a passive cache into something that *notices patterns*.

### 3.8 Research-backed parameters (use these as your priors)

These came out of empirical benchmarking and should shortcut a lot of your tuning:

**Context window = 3 turns is the sweet spot.** Match against the last *three* turns
of conversation (intent → AI response → user feedback), not just the current line.

| Context size | Target-match score | Noise score | Margin | Top-1 accuracy |
|:---:|:---:|:---:|:---:|:---:|
| 1 turn  | ~1.00 | 0.22 | 0.78 | 100% (but no memory) |
| **3 turns** | ~0.42 | 0.12 | **0.30** | **100%** |
| 5 turns | ~0.28 | 0.08 | 0.20 | 91% |

- **"Context collapse":** as you include more turns, the union of tokens grows faster
  than the intersection, so Jaccard scores *fall* and the margin between true matches
  and noise shrinks. Three turns keeps a wide, safe margin.
- **If you must exceed 3 turns,** down-weight older turns with exponential decay
  (e.g. `weight(d) = 0.5^d` for turn `d` back) so distant context can't drown the
  signal.
- **Hybrid tokenization (§3.3) scored best**, reaching ~0.98 F1 at the 3-turn window.
- On that 3-turn hybrid score, the **single best global threshold was ≈ 0.32** — see
  the threshold caveat in §3.6.

### 3.9 The feedback loop — learning without training **[CORE]**

No gradient training is required for the core experience. Learning is bookkeeping:

- **Accept** (user hits Tab) → reinforce that node (bump weight, reset decay clock).
- **Edit** (user accepts then changes it) → partial credit; nudge edge weights toward
  whatever they actually typed, and store the edited text as a new/updated reaction.
- **Reject** (user dismisses / types something unrelated) → let it decay; optionally
  down-weight.

Over time the graph's weights converge on the user's current style.

---

## 4. The experience — ghost responses **[CORE for usability]**

The engine is invisible; the UX is the product. Design goals: **inline, dimmed,
single-keystroke, never-blocking.**

### 4.1 Ghost tokens

When the agent produces output, Shadow Link predicts the user's likely reaction and
renders the top prediction as **dimmed, low-contrast text inline at the cursor** —
not a dropdown, not a modal. Optionally show 2–3 candidates as expandable chips.

Example: the agent dumps a verbose function; the ghost shows
`Too bloated — strip the boilerplate.` in faint italic. The user reads it, agrees,
hits Tab, and it's sent. They never composed the sentence.

### 4.2 Keyboard contract (zero-friction)

| Key | Action |
|-----|--------|
| **Tab** | Accept the full ghost suggestion. |
| **Ctrl+→** | Accept the next word/segment only. |
| **Esc** | Dismiss; keep typing manually. |

### 4.3 Latency budget

Prediction must feel instant — target **keystroke-to-suggestion ≤ ~50 ms** on the hot
path. This is *why* Lens 1 (Jaccard) runs first and Lens 2 (embeddings) is an opt-in
second pass (§3.4): you cannot afford a model round-trip on every keystroke.

### 4.4 Visual language (reference aesthetic)

Dark **glassmorphism**: frosted translucent panels over deep gradients; soft white
text; subtle light borders; graceful fallback to a solid dark background where
backdrop-blur isn't supported. Pair the ghost field with a HUD-style readout: task
status, token usage, reasoning log. (This is a stylistic reference, not a
requirement — the UX rules in §4.1–4.3 are what matter.)

### 4.5 Two operating modes

1. **Loose / copy-paste mode (build this first).** Shadow Link is a standalone surface
   (web or terminal). It produces the optimized prompt or the chosen reaction; the
   user copies it into their CLI. Trivial to integrate, good for proving the engine.
2. **Integrated / passthrough mode (the goal).** Shadow Link runs the agent CLI as a
   subprocess (or proxies its I/O), injecting ghosts inline and streaming output back.
   One unified surface; the substrate is "behind" Shadow Link.

---

## 5. The agentic substrate Shadow Link rides on **[CONTEXT — pluggable]**

Shadow Link models *you*; it still needs a *doer* underneath. The stated day-to-day
target is a **local-LLM CLI** (an "anti-gravity"-style Go/CLI agent pointed at an
OpenAI-compatible local endpoint). That's all the core experience needs.

However, the same author designed a far richer agent the layer *can* sit on, and it's
worth describing because it shows the ceiling of the idea and because it exposes a
clean **integration seam** (§6.3). Treat this entire section as **optional context** —
none of it is required to ship Shadow Link, and a substrate need not know Shadow Link
exists.

### 5.1 Body / Mind / Soul — a self-improving local agent

A three-part agentic framework that *receives work, reasons through it, executes it,
and learns from it*. Named for what it does at rest: **dream**.

- **Body — sense & act.** Ingests work (watched folders / APIs), runs tools, and
  records every action as a minimal narrative atom (an **Actor–Action–Target–
  Timestamp–Origin** record), which it compresses hierarchically. It also holds an
  **operational constitution** — a semi-mutable set of policies/roles/norms/intents
  that every decision runs *under* (think "employee handbook", not a knowledge base).
- **Mind — reason & decide.** Distills the task into a formal goal, makes a high-level
  plan, runs a **plan debate** (a "complexity advocate" vs. a "simplicity advocate",
  arbitrated) as a quality gate, then **fractally decomposes** the plan into a tree of
  sub-problems and solves it **post-order** (children before parents, parents
  synthesize children). A QA/coherence check can trigger bounded **refinement**.
- **Soul — learn & grow.** Improves at three tempos: **rumination** (real-time, while
  working), **reflection** (end-of-run lesson extraction), and **dreaming** (offline,
  resource-intensive consolidation). Lessons are crystallized into immutable
  principles via **SEAL** (Synthesize → Extract → Align → Lock-in) and injected into
  future runs as the agent's "identity".

### 5.2 The 9-phase lifecycle

`RECALL → DISTILL → PLAN → DECOMPOSE → EXECUTE → VERIFY → REFINE → RUMINATE → REMEMBER`

It mirrors human problem-solving: pull up relevant memory, formalize the goal, sketch
a plan, break it down, do it, check it, fix it, analyze what happened, and only
*then* commit lessons to memory. **Learning happens last**, after the work is done.

### 5.3 Nudges — evidence-gated self-improvement

Softer than rules: a **nudge** is a phase-specific hint ("for comparison tasks, prefer
2–3 focused steps") extracted when a run shows *friction* (lots of QA cycles or
refinement loops). A nudge must **prove itself** — re-run past tasks with the nudge
injected (a "shadow run") and only if it reduces friction across multiple domains does
it graduate into a durable lesson and enter SEAL. Self-improvement is *evidence-based,
not speculative.*

### 5.4 Human-as-a-tool

Crucially for Shadow Link: when the agent hits a **known unknown**, it can call the
human as a *tool* — pause, emit a question, and wait for an answer (e.g. by writing a
`question` file and watching for a `response` file). This is the natural seam where
Shadow Link plugs in (§6.3).

---

## 6. End-to-end: how it all connects **[CORE workflow]**

### 6.1 Cold start (corpus bootstrap)

1. Ingest historical AI chat exports.
2. Tokenize each user reaction (§3.3), classify into buckets (§3.5), embed if desired.
3. Build the reaction graph: nodes + bucket tags + similarity edges.
4. Run theme discovery (§3.7) to seed an initial preference summary.

### 6.2 Live loop (the everyday experience)

```
 ┌──► agent produces output
 │         │
 │         ▼
 │   Shadow Link builds a query = [last 3 turns] + [stated goals] + [agent output]
 │         │
 │         ▼
 │   Lens-1 Jaccard over all reactions  →  (optional) Lens-2 semantic on top-k
 │         │
 │         ▼
 │   rank by combined score × effective weight (decay) ; filter by per-bucket threshold
 │         │
 │         ▼
 │   render top prediction as an inline ghost (≤50ms)
 │         │
 │         ▼
 │   user: Tab (accept) / edit / Esc (reject)
 │         │
 │         ▼
 └─── reinforce / adjust weights (§3.9)  ──┘  (loop)
```

### 6.3 The integration seam (when wrapping a full agent)

If the substrate supports **human-as-a-tool** (§5.4), the connection is clean and
file-based: the agent writes a *question* artifact when it needs human input; Shadow
Link watches for it, matches the question against the user's reaction graph, and can
**auto-draft (or auto-answer) the response** in the user's voice — writing back the
*response* artifact. This is how a passive predictor becomes an active autopilot that
acts *as the user would*. (Note: this seam is opportunistic. A generic CLI substrate
won't expose it; in that case Shadow Link operates in passthrough/copy-paste mode.)

### 6.4 The optional cost/cache plane

Because all of this is local-first and may also talk to a hosted model, you can insert
a transparent layer that **stabilizes the cacheable prefix** of outbound LLM requests:
keep the large static portion of the prompt byte-identical across turns, and move the
small dynamic bits (timestamps, per-turn reminders) to the very end. Stable prefixes
maximize prompt-cache hit rates and cut cost. **Build this only if cost is a real
constraint — it is an independent utility, not part of the matcher.**

---

## 7. Build roadmap

Phased so each milestone is independently useful. The original plan envisioned
"agentic sub-pipelines" — a **research agent** to do benchmarking and a **building
agent** to scaffold schemas and UI — handing artifacts to a human integrator. You can
literally delegate the marked steps to coding agents.

**Milestone 1 — CLI prototype (engine spine). [CORE]**
- Tokenizer (hybrid, negation-preserving). *(research agent can benchmark n-gram mix)*
- Jaccard Lens 1; cascade matcher; 3-turn context window.
- Reaction store with buckets + decay.
- Validate against real chat-log exports; calibrate per-bucket thresholds.

**Milestone 2 — Local API + reaction graph. [CORE]**
- Graph data layer (nodes/edges/tags) with CRUD. *(building agent can scaffold)*
- HTTP API: query/predict, stats, feedback/reinforce, watcher endpoints.
- Semantic Lens 2 via a local embedding endpoint (with pure-lexical fallback).
- Background theme discovery → preference summary.

**Milestone 3 — Glassy UI + ghost responses. [CORE for usability]**
- Inline ghost rendering, Tab/Ctrl-→/Esc contract, ≤50ms hot path.
- Feedback loop wired to reinforcement.
- Copy-paste mode first; then integrated passthrough.

**Milestone 4+ — Substrate integration & beyond. [MIXED]**
- Wrap the local-LLM CLI (passthrough mode).
- If using a human-as-a-tool agent: wire the question/response seam (§6.3).
- Scaling + aspirational features (§9) as needed.

---

## 8. Design-rationale ledger (the "why")

| Decision | Rationale |
|----------|-----------|
| **Two lenses, lexical first** | Jaccard is instant and interpretable; embeddings catch paraphrase but cost a call. Cascade gives speed *and* a semantic safety net. |
| **Preserve negation** | Stripping "not/never/avoid" makes the matcher invert the user's meaning — the single most dangerous tokenization bug. |
| **Hybrid tokenization** | Word features give phrasal precision; char/structural features give a recall cushion. Empirically best (≈0.98 F1). |
| **3-turn context** | Enough history to capture intent→response→feedback; more turns cause "context collapse" (scores deflate, margin shrinks). |
| **Calibrated, per-bucket thresholds** | A false match costs more for a rejection (meaning inversion) than a pivot. One global cutoff is the wrong abstraction. |
| **Category-aware decay** | Standing rules are evergreen; tactical asks fade in hours; one-off rejections fade fastest. Recency should track each kind's real lifespan. |
| **Graph / non-exclusive buckets** | Real feedback is multi-faceted ("fix it *and* remember…"). Folders force false single-category choices. |
| **No training in the core loop** | Accept/edit/reject + weight bookkeeping is enough to personalize; gradient methods are an aspirational upgrade, not a dependency. |
| **Inline ghosts, single keystroke** | Turns "drafting" into "verifying" — the entire point. Dropdowns/modals reintroduce the friction we're removing. |
| **Local-first** | A tool whose job is logging private dev conversations must keep the corpus on-device. Privacy is load-bearing, not a feature. |
| **Substrate is pluggable** | Shadow Link models the user, not the agent. Coupling it to one agent would defeat the "sits on top of whatever you use" goal. |

---

## 9. Core vs. aspirational — scope honestly

**CORE (MVP spine — Milestones 1–3):**
- Hybrid, negation-preserving tokenizer.
- Jaccard Lens 1 + cascade to a local-embedding Lens 2 (with lexical fallback).
- 3-turn context window; per-bucket calibrated thresholds; category-aware decay.
- Reaction graph with non-exclusive buckets; theme discovery.
- Accept/edit/reject reinforcement loop.
- Inline ghost UX with the keystroke contract and latency budget.
- Local-first storage.

**ASPIRATIONAL (research / later — don't let these block the MVP):**
- **Token-steering personalization (CoPe-style):** bias the local model's *decoding*
  toward the user's style via a likelihood ratio between a personalized and a base
  model — instead of (or alongside) RAG-style template injection. Open question:
  steering vs. in-context injection, which is cheaper locally.
- **Stylistic-autonomy guardrails (Coh-Metrix):** measure the user's syntactic
  complexity / cohesion / lexical sophistication and verify personalization *preserves*
  their voice rather than homogenizing it toward "neutral" English. An ethics-grade
  guardrail, not MVP.
- **Scaling (MinHash LSH + HNSW):** if the corpus reaches millions of reactions,
  replace exact all-pairs matching with MinHash sketches (approximate Jaccard) and an
  ANN index for embeddings. Not needed at small scale.
- **Cross-modal reactions:** align voice/tone feedback with text reactions.
- **Surgical-patch awareness:** detect *code-level* rejections by tokenizing patch
  structure (cell/function-level markers) so "strip the boilerplate" can map to a
  precise edit. (Companion idea: give the LLM a vocabulary to return surgical JSON
  patches instead of re-emitting whole files.)
- **Self-improving substrate (the full Body/Mind/Soul agent, §5)** including offline
  "dreaming" / QLoRA weight-level adaptation. Powerful but orthogonal to Shadow Link's
  core; integrate via the §6.3 seam only if you want it.

---

## 10. Glossary

- **Shape** — the token-set representation of a piece of text; the thing Shadow Link
  compares.
- **Reaction record** — a stored unit: situation + the user's reaction + buckets +
  weight + decay clock.
- **Lens 1 / Lens 2** — lexical (Jaccard) and semantic (embedding) similarity.
- **Bucket** — a behavioral category (Request / Reminder / Rejection / Correction /
  Pivot); non-exclusive.
- **Ghost / ghost token** — the dimmed, inline, single-keystroke-acceptable prediction.
- **Context collapse** — the drop in Jaccard scores as the context window grows,
  because the token union grows faster than the intersection.
- **Effective weight** — a node's base weight after temporal decay; reinforced on
  accept.
- **Theme discovery** — clustering the reaction graph to surface unnamed recurring
  preferences.
- **Substrate** — the agentic CLI / LLM runtime Shadow Link sits on top of.
- **Human-as-a-tool** — an agent pattern where the agent pauses to ask the human a
  question; Shadow Link's natural integration seam.
- **(Substrate-side, §5) CAN** — Canonicalized Atomic Narrative: a minimal
  Actor-Action-Target-Timestamp-Origin record of an agent action.
- **(Substrate-side, §5) SEAL** — Synthesize / Extract / Align / Lock-in: turning
  transient lessons into immutable principles.

---

---

## 11. Appendix — 2026 technology validation & recommended concrete stack

*Added 2026-06-20. The body above (§§1–10) is the language-agnostic idea; this appendix
maps it to real, current tools and corrects a few recommendations against 2025–2026
best practice. Every nontrivial claim is sourced. Where an item updates an earlier
section, it says so. (Research note: web search was intermittently degraded when this
was compiled, so citations resolve to primary vendor/paper pages rather than search
snippets.)*

### 11.1 Lexical lens — prefer BM25 over raw Jaccard for short reactions *(updates §3.4)*

The project's conceptual heart is "Jaccardian shape matching," and Jaccard is still the
right thing to *teach* the idea: it's exact, interpretable, and dependency-free, so keep
it as the MVP lens. **But for production short-text matching, BM25 is the stronger
lexical lens.** Raw Jaccard has no term-importance weighting and no length
normalization, so on one-liners a single shared stopword or one differing word swings
the score wildly; BM25 adds IDF + length normalization and is the standard sparse
ranker for short text.
- Use **`bm25s`** (pure-NumPy, very fast) as the lexical lens; `rank-bm25` is fine for a
  prototype but slow; Tantivy/Lucene for a heavyweight engine.
  ([github.com/xhluca/bm25s](https://github.com/xhluca/bm25s),
  [pinecone hybrid-search intro](https://www.pinecone.io/learn/hybrid-search-intro/))
- **MinHash + LSH is a *scale* optimization for approximate Jaccard, not your primary
  matcher** — reserve it for near-duplicate detection / dedup once the corpus is large.
  Production library: **`datasketch`**. ([github.com/ekzhu/datasketch](https://github.com/ekzhu/datasketch))
- Net: *Jaccard to explain and to MVP; BM25 to ship; MinHash/LSH only at scale.*

### 11.2 Fusion — RRF k≈60 confirmed; relative-score blend is the tunable upgrade *(confirms §3.4)*

- **RRF with k≈60 is still the robust, zero-tuning default** (traces to Cormack et al.,
  SIGIR 2009; reaffirmed in current Azure AI Search docs). It uses only ranks, so it
  needs no score normalization — ideal when BM25 (unbounded) and cosine (~0.3–1.0) live
  on different scales.
  ([learn.microsoft.com hybrid-search-ranking](https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking),
  [Cormack SIGIR'09](http://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf))
- **Newer option:** Weaviate's **Relative Score Fusion** (min-max normalize each list,
  then sum) reports ~6% better recall than RRF because it preserves score *magnitudes*
  RRF discards — use it if you're willing to calibrate.
  ([weaviate.io hybrid fusion](https://weaviate.io/blog/hybrid-search-fusion-algorithms))

### 11.3 Dense lens — embedding models & ANN *(updates §3.4, §9 scaling)*

**Recommended default embedding model: EmbeddingGemma (308M, 768-dim, Matryoshka-
truncatable to 256/128).** Purpose-built for on-device serving (<200MB RAM with QAT),
multilingual, first-class LM Studio / Ollama / llama.cpp support.
([developers.googleblog.com EmbeddingGemma](https://developers.googleblog.com/en/introducing-embeddinggemma/))
Strong alternatives: **Qwen3-Embedding-0.6B** (Apache-2.0, multilingual),
**bge-small-en-v1.5** (384-dim, tiny, *symmetric* — good for English-only),
**nomic-embed-text-v1.5** (ships a `clustering` prefix for symmetric use),
**all-MiniLM-L6-v2** (smallest/fastest). MTEB numbers are vendor-self-reported across
dates — treat as directional and check the live
[MTEB leaderboard](https://huggingface.co/spaces/mteb/leaderboard).

> **Subtle but important — symmetric vs. asymmetric.** Matching a reaction against
> *stored reactions* is **symmetric** (text-vs-text of the same kind). Most top
> retrieval models (E5, BGE, mxbai, arctic, Qwen3) are trained **asymmetrically** with
> `query:`/`document:` (or instruction) prefixes; using the wrong prefix mode silently
> degrades short-text similarity. Either pick a symmetric model or apply the **same
> prefix to both sides**. ([SBERT semantic textual similarity](https://sbert.net/docs/sentence_transformer/usage/semantic_textual_similarity.html))

**ANN strategy:** at small/medium corpus size, **brute-force exact cosine is exact and
fast enough** — use NumPy or **`sqlite-vec`** (pure-C, portable, currently brute-force
KNN). Graduate to **`hnswlib`** or **`usearch`** (header-only, aggressive quantization)
only when scan latency bites. If you'd rather have vector + full-text + metadata in one
embedded store, **LanceDB** or **Chroma**.
([sqlite-vec](https://github.com/asg017/sqlite-vec),
[usearch](https://github.com/unum-cloud/usearch),
[lancedb](https://github.com/lancedb/lancedb))

**Cosine thresholds are model-specific — calibrate on your own labeled pairs** (no
universal cutoff; ~0.5 ≈ loosely related, ~0.7–0.8+ ≈ strongly similar, as priors only).
This reinforces §3.6's "don't hard-code a threshold" rule.

### 11.4 Local serving — all three runtimes expose OpenAI-compatible embeddings *(confirms §4.3, §6)*

- **LM Studio:** OpenAI-compatible `POST /v1/embeddings`, default port **1234**.
  ([lmstudio.ai openai-compat](https://lmstudio.ai/docs/app/api/endpoints/openai))
- **Ollama:** native `POST /api/embed` *and* OpenAI-compatible `POST /v1/embeddings`,
  port **11434**; bundles `nomic-embed-text`, `mxbai-embed-large`, `all-minilm`.
  ([docs.ollama.com openai-compat](https://docs.ollama.com/api/openai-compatibility))
- **llama.cpp `llama-server`:** native `POST /embedding` + OpenAI `POST /v1/embeddings`
  (run with `--embeddings`, choose `--pooling`).
  ([llama.cpp server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md))

So Shadow Link points one OpenAI client at `localhost` and gets cheap on-device
embeddings — no cloud, consistent with the local-first / privacy rationale.

### 11.5 Ghost-text UX — concrete implementations *(updates §4)*

- **Web — Monaco** has a **first-class inline-completions (ghost-text) API**
  (`registerInlineCompletionsProvider`) with built-in `debounceDelayMs` and, crucially,
  **`handleItemDidShow` / `handlePartialAccept` / `handleRejection` telemetry hooks** —
  these are exactly your accept/edit/reject feedback signals (§3.9), for free.
  ([Monaco InlineCompletionsProvider](https://microsoft.github.io/monaco-editor/typedoc/interfaces/editor_editor_api.languages.InlineCompletionsProvider.html))
- **Web — CodeMirror 6** has only dropdown completion natively (and deliberately does
  *not* bind Tab); for dimmed inline Tab-to-accept use the **`codemirror-copilot`**
  extension (`inlineCopilot(...)`, default 1000ms debounce + local caching).
  ([codemirror-copilot](https://github.com/asadm/codemirror-copilot))
- **Terminal** — the UX bar is **fish / zsh-autosuggestions**: muted-gray text after the
  cursor, accept-all with →/Ctrl-F (fish) or →/End (zsh), accept-one-word with
  Alt-→/forward-word. These feel free because they're **instant and local** (history/
  prefix match, no network) — mirror that with a local fallback suggestion while any
  model call catches up.
  ([fish](https://fishshell.com/docs/current/interactive.html),
  [zsh-autosuggestions](https://github.com/zsh-users/zsh-autosuggestions))
- **Injecting into another CLI's stream** — the canonical prior art is Fig → Amazon Q's
  **`figterm`** PTY shim (intercepts the edit buffer, paints ANSI suggestions, needs a
  per-emulator detection layer). Build it on **`node-pty`** (or Python `pexpect`/
  `ptyprocess`).
  ([node-pty](https://github.com/microsoft/node-pty),
  [Amazon Q terminal integration](https://deepwiki.com/aws/amazon-q-developer-cli-autocomplete/5.1-terminal-integration))
- **Latency targets** — Nielsen's thresholds (0.1s instant / 1s flow / 10s lost) still
  govern. Aim to render the ghost **<~100ms** using the local lexical fallback; a model-
  backed suggestion realistically lands in the hundreds of ms, so **debounce ~75–300ms
  and only show the ghost after a typing pause.**
  ([NN/g response times](https://www.nngroup.com/articles/response-times-3-important-limits/))

### 11.6 The substrate — "anti-gravity CLI" is real: Google Antigravity *(updates §5, §6.3)*

The user's "anti-gravity CLI" is **Google Antigravity**, a real agent-first development
platform (public preview **2025-11-18**, alongside Gemini 3; a heavily modified VS Code
fork). By **Antigravity 2.0** it ships four surfaces, including an **Antigravity CLI
"built in Go"** (matches the user's "built on Go" recollection), an **Antigravity IDE**,
and — most importantly for integration — a **Python SDK** on a shared agent harness and
**first-class MCP support**.
([Wikipedia: Google Antigravity](https://en.wikipedia.org/wiki/Google_Antigravity),
[Google Cloud: choosing your Antigravity surface](https://cloud.google.com/blog/topics/developers-practitioners/choosing-your-surface-antigravity-20-antigravity-cli-antigravity-ide-or-antigravity-sdk),
[antigravity.google/docs/mcp](https://antigravity.google/docs/mcp))

**Correction to the integration plan:** because Antigravity exposes an **SDK + MCP**,
**PTY-wrapping its Go binary is the *least* clean integration**, not the first choice.
Pick the seam by what you need:

| Need | Best seam |
|------|-----------|
| Inline ghost-text + keystroke-level accept/reject | **PTY shim** (only the PTY/host editor sees keystrokes) — or Monaco's inline API if you own the editor surface |
| Structured prediction + reaction capture | **MCP server** (Antigravity-supported, stable contract) or the **Antigravity Python SDK** |
| Observe/inject at the model layer, language-agnostic | **OpenAI-compatible proxy** on the model endpoint (reuses the §11.4 local stack) — *only if the agent's model routing is user-redirectable* |

Realistic architecture = **hybrid**: PTY (or owned editor) for the inline UI, a local
OpenAI-compatible endpoint for embeddings/generation, and MCP/SDK for structured
integration. *(Caveat: the four-surface/SDK details rest on Google's blog; verify exact
SDK import paths and whether model routing is redirectable against the live build before
coding against them.)*

### 11.7 Aspirational items — now with real references *(updates §9)*

- **CoPe is a real, confirmed method** — *Personalized LLM Decoding via Contrasting
  Personal Preference*, Bu et al., **EMNLP 2025 Main**, [arXiv 2506.12109](https://arxiv.org/abs/2506.12109).
  Per-user LoRA `π_user` + base `π_base`; decode-time reward
  `r = log[ π_user / π_base^α ]` over a plausibility-filtered candidate set (paper uses
  α≈0.1–0.3, τ=0.1). Reports **+10.6% ROUGE-L vs a task-adapted baseline, +5.7% vs
  per-user LoRA alone**. Costs **~2× forward passes/token**; **no public code** (small to
  reimplement). Treat as *research-with-easy-reimplementation*.
- **Cheaper local steering (do this before CoPe): activation/control vectors.** One
  forward pass, near-zero overhead — train a control vector from contrastive example
  pairs and add it to the residual stream. Practical tooling: **`repeng`** (trains a
  vector in <60s, exports GGUF for `llama.cpp --control-vector`). Background: RepE
  ([2310.01405](https://arxiv.org/abs/2310.01405)), CAA
  ([2312.06681](https://arxiv.org/abs/2312.06681)),
  [repeng](https://github.com/vgel/repeng).
- **Personalization approach for one local user:** start with **RAG over the user's own
  history** (no training, instant updates; LaMP line of work,
  [2304.11406](https://arxiv.org/abs/2304.11406)); add a **per-user LoRA** (OPPU,
  [2402.04401](https://arxiv.org/abs/2402.04401)) when you want fidelity + robustness to
  drift; stack CoPe-style contrastive decode on top only if needed. Consensus is hybrid
  (survey: [2411.00027](https://arxiv.org/abs/2411.00027)).
- **Voice-preservation (the §9 "stylistic autonomy" guardrail) is buildable locally.**
  Coh-Metrix itself is web-only; use downloadable local tools instead — **TAACO**
  (cohesion), **TAALES** (lexical sophistication), **TAASSC** (syntactic complexity)
  from [linguisticanalysistools.org](https://www.linguisticanalysistools.org/tools.html)
  — plus **Wegmann Style-Embeddings** ([HF: AnnaWegmann/Style-Embedding](https://huggingface.co/AnnaWegmann/Style-Embedding),
  [arXiv 2204.04907](https://arxiv.org/abs/2204.04907)) to cosine-score generated text
  against the user's own baseline and flag drift toward a generic centroid. The
  homogenization risk is empirically real (Padmakumar & He, **ICLR 2024**,
  [2309.05196](https://arxiv.org/abs/2309.05196)).

### 11.8 Glassmorphism — `backdrop-filter` is safe now *(confirms §4.4)*

Baseline "newly available" since Sept 2024, ~94% global support. Needs a translucent
background to show; include the `-webkit-` prefix for Safari and a solid fallback:

```css
.glass { background: rgba(20,20,30,0.85); }            /* fallback */
@supports ((backdrop-filter: blur(10px)) or (-webkit-backdrop-filter: blur(10px))) {
  .glass {
    background: rgba(255,255,255,0.12);                /* needs transparency */
    -webkit-backdrop-filter: blur(10px);
    backdrop-filter: blur(10px);
  }
}
```
([MDN backdrop-filter](https://developer.mozilla.org/en-US/docs/Web/CSS/backdrop-filter),
[caniuse](https://caniuse.com/css-backdrop-filter))

### 11.9 Recommended concrete default stack (2026)

- **Lexical lens:** `bm25s` (keep Jaccard as the conceptual/MVP fallback; `datasketch`
  MinHash/LSH only for dedup at scale).
- **Dense lens:** EmbeddingGemma (768→256 via Matryoshka), served locally via LM Studio/
  Ollama/llama.cpp at an OpenAI-compatible `/v1/embeddings`; **symmetric prompting**.
- **Index:** brute-force exact (`sqlite-vec`/NumPy) → `hnswlib`/`usearch` at scale → or
  LanceDB for vector+FTS+metadata in one store.
- **Fusion:** RRF (k=60) default; relative-score blend if you'll calibrate.
- **UX:** Monaco inline-completions API (web, with its accept/reject telemetry hooks) or
  a `node-pty` shim (terminal); local lexical fallback for <100ms first paint.
- **Substrate seam:** MCP server / Antigravity SDK for structure; PTY (or owned editor)
  for the inline ghost UI; OpenAI-proxy only if model routing is redirectable.
- **Personalization (later):** RAG over history → per-user LoRA → optional CoPe /
  control-vector steering; Wegmann-embedding + Kyle-tools voice-drift monitor.

---

*End of blueprint. Build Milestone 1, validate the matcher against a real corpus,
then let the UX make it feel like magic.*
