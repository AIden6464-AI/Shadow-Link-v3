# Shadow Link Architecture & Integration

This document outlines the architecture, components, and integration modes of **Shadow Link v3**, as well as its relationship with adjacent systems.

---

## 1. The Matcher Engine (Shadow Link Core)

The "shadow of you." It learns your linguistic shape from your past AI corrections and predicts your next one.

*   **Tokenizer (`tokenizer.py`):** Generates word tokens, word bi/trigrams, and character 3-grams, NFKC-normalized. Critically, it **preserves negation** (e.g., `not`, `never`, `no`, `avoid`, `without`) instead of stripping them as stopwords. This prevents a correction like *"do not use Docker"* from matching as *"use Docker"*.
*   **Jaccard Similarity (`tokenizer.py`):** Calculates lexical similarity using the formula:
    $$\text{Sim}_{\text{Jaccard}} = \frac{|A \cap B|}{|A \cup B|}$$
    on the flattened token sets.
*   **Dual-Lens Hybrid Scorer (`retrieval.py`):** Combines Jaccard and Cosine similarity:
    $$S = \alpha \cdot \text{Sim}_{\text{Jaccard}} + (1 - \alpha) \cdot \text{Sim}_{\text{Cosine}}$$
    The balance parameter $\alpha$ shrinks dynamically as the query length $L$ (in words) increases:
    $$\alpha = 0.4 \cdot e^{-0.05 \cdot L}$$
    This design ensures:
    *   **Short queries** lean lexical (Jaccard).
    *   **Long queries** lean semantic (dense embeddings via local provider).
    *   **Two-pass optimization:** A cheap lexical Jaccard pass runs over all rows, and embedding/cosine similarity runs only on the top-$k$ candidates. The hot path stays fast and network-free.
*   **Behavioral Buckets (`retrieval.py`):** Categorizes reactions into non-exclusive buckets with per-bucket thresholds:
    *   `rejection` / `correction` are strict ($0.75$, to avoid inverting your meaning).
    *   `reminder` / `request` are moderate ($0.55$).
    *   `pivot` is lenient ($0.45$).
*   **Temporal Decay (`decay.py`):** Automatically decays the utility of reactions over time:
    $$W(t) = W_0 \cdot e^{-\lambda \Delta t}$$
    *   `reminder` (standing style/architectural rules) never decays ($\lambda \to 0$, infinite half-life).
    *   `request` and `pivot` (tactical/local fixes) have a 7-day half-life.
    *   `rejection` and `correction` have a 3-day half-life.
    *   Decay never drops a reply below 25% of its base weight (`SHADOW_WEIGHT_FLOOR`); rejecting a ghost is what removes it.
    *   Each ghost that surfaces a reply re-arms that reply's decay clock.
*   **Graph Database (`database.py`):** A SQLite database storing:
    *   **Prompts (nodes):** text, primary bucket, weight, metadata (JSON containing token caches, session, source, and secondary buckets).
    *   **Prompt Edges:** Pairwise Jaccard weights between nodes to allow BFS theme clustering at a similarity floor ($\ge 0.30$).
    *   **Prompt Vectors:** Cached dense embeddings, stored in a dedicated `prompt_vectors` table to keep the hot read path lean.

---

## 2. The Claude Code Proxy (`proxy.py`)

An optional mitmproxy addon between Claude Code and the Anthropic Messages API. It learns reaction pairs and can add Shadow Link context, without disturbing the conversation Claude Code sends.

*   **History stays byte-stable:** the proxy never edits earlier turns, the system prompt or the tools. Claude Code already keeps its history stable, and two API behaviours depend on that: prompt caching (a changed earlier turn re-bills everything after it) and the thinking-block history check on Opus 5.5 / Fable 5.1 / Sonnet 5.5 / Haiku 5.5 (a 400 for accounts created on or after 2026-08-31).
*   **Injection (`apply_injections()`), off by default:** when `SHADOW_LINK_INJECT=true`, one text block is appended at the end of the new user message, after any tool results. The `InjectionLedger` (`claude_audit/injections.jsonl`) records it, and every later request re-appends exactly the same text to that message. The preferred channel is the `UserPromptSubmit` hook (`shadow_hook.py`), whose output Claude Code adds as context itself.
*   **Wording:** injected context says it is a local prediction from past replies, not a message or instruction from the user. A synthesized ghost is only included with `SHADOW_LINK_INJECT_SYNTHESIZED=true`.
*   **Learning from the request:** each request already holds Claude's previous reply and what the user typed, so pairs are learned there (in the background) instead of from the response.
*   **Streaming-safe:** SSE responses are streamed through unbuffered; `SSETap` reads usage and Claude's reply text from the events as they pass.
*   **Gated:** only `POST /v1/messages` (not `count_tokens` or batches) with a `claude-cli` user agent.
*   **Audit log:** request size, whether anything was injected, and response usage go to `claude_audit/YYYY-MM-DD.jsonl`.

---

## 3. The OGI Framework (`oneiros-main`)

A local-first, self-improving agent structured around a 9-phase lifecycle:
$$\text{RECALL} \to \text{DISTILL} \to \text{PLAN} \to \text{DECOMPOSE} \to \text{EXECUTE} \to \text{VERIFY} \to \text{REFINE} \to \text{RUMINATE} \to \text{REMEMBER}$$

*   **Body:** Interacts with the outer world. Manages folder/API watchers, tools, and CANs (Canonicalized Atomic Narratives — every action logged as `Actor|Action|Target|Time|Origin` and hierarchically compressed).
*   **Mind:** Handles logical reasoning. Manages distillation, high-level planning, plan debates (advocated by Complexity and Simplicity viewpoints), fractal decomposition, post-order tree solving, and validation.
*   **Soul:** Implements triple-tempo learning:
    *   *Rumination:* Real-time short-term lessons.
    *   *Reflection:* End-of-run lesson extraction.
    *   *Dreaming:* Offline SEAL / optional QLoRA fine-tuning.
    *   Nudges are run as hypotheses that must survive shadow runs before graduating into durable lessons.
*   **Execution:** Routing is targeted at local models (e.g., LM Studio at `localhost:1234`), audited via `.ogi_state/llm_calls.jsonl`.

---

## 4. System Throughline & Connection

These components form a unified personal-AI stack centered around the user's workflow:

```
   YOUR AI USAGE (Claude exports/, daily Claude Code+Desktop)
        │                                   │
        │ corpus of corrections             │ live traffic
        ▼                                   ▼
   SHADOW LINK  ──── learns your shape ──── CLAUDE-PROXY
   (predicts your                           (makes that usage
    next nudge)                              cheaper via cache)
        │
        │ /predict writes .response.txt, /watcher reads .question.txt
        ▼
   OGI / oneiros  ── autonomous agent that asks "known unknowns"
                      and learns from each run
```

### Integration Bridge
*   OGI's **Body** employs a *human-as-a-tool* loop: when blocked by a "known unknown," it writes a `request_id.question.txt` file and pauses.
*   **Shadow Link's Watcher** monitors this directory, intercepts the question, matches it against your historical behavioral graph, and writes a `.response.txt` or `.ghost.txt` file.
*   This allows the autonomous agent to continue running using your preferred style rather than blocking indefinitely.

In summary:
1.  **`claude-proxy`** handles the economic layer (cost optimization).
2.  **`Shadow Link`** models your voice and past reactions.
3.  **`OGI`** performs autonomous tasks, using the Shadow Link model as a proxy for the user when decisions need to be made in your voice.
