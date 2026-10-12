"""
shadow_link/aca/neurons.py

Decision neurons: small, typed, bounded judgments fired in parallel against
one shared state — the shape Jev (TypeSafe System One) evaluates.

    Noul   — P(yes) for one literal yes/no question
    Choice — probability across named options
    Score  — probability-weighted position on an ordered scale (0..1)

Backends
--------
HeuristicBackend  — local, deterministic judgments computed from the ACA state.
                    Always available; used when no Jev client is configured.
JevBackend        — forwards the same state + questions to Jev through a
                    caller-supplied client function. The wire format is not
                    hard-coded here: pass `client(state, questions) -> results`
                    built on TypeSafe's SDK/API (see docs.typesafe.ai).

Application logic (FiringPolicy) — not the backend — decides what a
probability means: each neuron has its own thresholds, and destructive
actions need much stronger evidence.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Protocol


# ------------------------------------------------------------ question types
@dataclass
class Noul:
    name: str
    instructions: str
    when_true: str = ""
    when_false: str = ""
    kind: str = "noul"

    def to_dict(self) -> dict:
        return {"name": self.name, "type": "noul", "instructions": self.instructions,
                "when_true": self.when_true, "when_false": self.when_false}


@dataclass
class Choice:
    name: str
    instructions: str
    options: dict[str, str]                   # option name → description
    kind: str = "choice"

    def to_dict(self) -> dict:
        return {"name": self.name, "type": "choice", "instructions": self.instructions,
                "options": [{"name": k, "description": v} for k, v in self.options.items()]}


@dataclass
class Score:
    name: str
    instructions: str
    levels: list[str]                         # ordered low → high (≤10)
    kind: str = "score"

    def to_dict(self) -> dict:
        return {"name": self.name, "type": "score", "instructions": self.instructions,
                "levels": list(self.levels)}


Question = Noul | Choice | Score


@dataclass
class Signal:
    """One evaluated neuron."""
    name: str
    kind: str
    value: float                               # noul: P(yes); score: 0..1; choice: P(top)
    top: str = ""                              # choice/score: winning label
    probs: dict[str, float] = field(default_factory=dict)
    backend: str = ""
    level: str = "silent"                      # silent | latent | activate | strong | fire
    targets: list[str] = field(default_factory=list)  # node ids this signal is about

    def to_dict(self) -> dict:
        d = {"name": self.name, "kind": self.kind, "value": round(self.value, 4),
             "level": self.level, "backend": self.backend}
        if self.top:
            d["top"] = self.top
        if self.probs:
            d["probs"] = {k: round(v, 4) for k, v in self.probs.items()}
        if self.targets:
            d["targets"] = self.targets
        return d


# ------------------------------------------------------------- state builder
def build_state(
    *,
    event: dict,
    goal: str,
    context: list[dict],
    hypotheses: list[dict],
    funnel: dict,
    metacog: dict,
    agents: list[dict],
    constraints: list[str] | None = None,
) -> dict:
    """
    Converge ACA output into a structured state object — the single shared
    input every neuron is evaluated against.
    """
    def slim(i: dict) -> dict:
        return {"id": i["id"], "type": i["type"], "title": i["title"],
                "content": i["content"][:600], "score": i["score"], "trust": i["trust"],
                "conflicts": i["conflicts"]}

    by_type: dict[str, list[dict]] = {}
    for i in context:
        by_type.setdefault(i["type"], []).append(slim(i))
    return {
        "current_task": goal,
        "event": {"kind": event.get("kind", "prompt"), "text": (event.get("text") or "")[:2000]},
        "active_context": [slim(i) for i in context],
        "memories": [slim(i) for i in context if i["type"] in
                     ("memory", "reaction", "episode", "semantic", "procedure", "decision")],
        "hypotheses": [{"id": h["id"], "text": h["title"], "confidence": h["confidence"]}
                       for h in hypotheses],
        "tool_results": by_type.get("tool_result", []) + by_type.get("test_result", []),
        "previous_failures": by_type.get("error", []) + [
            slim(i) for i in context if i["type"] == "episode"
            and "fail" in i["content"].lower()],
        "project_constraints": (constraints or []) + [
            i["content"][:300] for i in context if i["type"] in ("requirement", "architecture")],
        "conflicts": sorted({tuple(sorted((i["id"], c))) for i in context for c in i["conflicts"]}),
        "funnel": funnel,
        "metacognition": metacog,
        "agents": [{"name": a["name"], "description": a["description"]} for a in agents],
    }


# --------------------------------------------------------- built-in neurons
def standard_neurons(state: dict) -> list[Question]:
    """The decision neurons ACA fires every cycle."""
    qs: list[Question] = [
        Noul("should_expand",
             "Is the active context insufficient to understand the current situation, "
             "so that more associated context should be activated?",
             "More context is needed.", "The active context is sufficient."),
        Noul("context_conflict",
             "Does the active context contain information that contradicts other "
             "active information?"),
        Noul("needs_verification",
             "Before committing to a conclusion, is further verification required "
             "given the evidence and its trustworthiness?"),
        Noul("context_too_large",
             "Is the active context larger than needed, with redundant or weakly "
             "relevant items?"),
        Noul("stuck",
             "Is the work repeatedly circling the same area or failing without progress?"),
        Choice("next_action", "What should happen next?", {
            "inspect_top": "Inspect the strongest active item or leading hypothesis.",
            "explore_alternative": "Investigate a weaker but plausible alternative.",
            "verify": "Verify the leading conclusion before acting on it.",
            "research_external": "Get information that is not in memory.",
            "done": "The task is complete; nothing further is needed.",
        }),
        Score("remember", "How valuable is this event as a long-term memory?",
              ["discard", "temporary", "store"]),
    ]
    if state.get("agents"):
        opts = {a["name"]: a["description"] for a in state["agents"]}
        opts["none"] = "No specialised agent is needed."
        qs.append(Choice("wake_agent", "Which specialised agent should wake up for this?", opts))
    for item in state["active_context"][:8]:
        qs.append(Noul(f"relevant:{item['id']}",
                       f"Is the context item '{item['title'][:80]}' relevant to the current task?"))
    return qs


# ----------------------------------------------------------------- backends
class DecisionBackend(Protocol):
    name: str

    def evaluate(self, state: dict, questions: list[Question]) -> list[Signal]: ...


def _sig(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _softmax(scores: dict[str, float], temp: float = 0.35) -> dict[str, float]:
    m = max(scores.values())
    ex = {k: math.exp((v - m) / temp) for k, v in scores.items()}
    z = sum(ex.values())
    return {k: v / z for k, v in ex.items()}


class HeuristicBackend:
    """
    Local judgments derived from measurable ACA features (scores, trust,
    conflict count, funnel sizes, hypothesis confidences, cycle history).
    Not a language model: it answers only the built-in neurons it knows, and
    leaves unknown questions unanswered rather than guessing.
    """
    name = "heuristic"

    def evaluate(self, state: dict, questions: list[Question]) -> list[Signal]:
        f = self._features(state)
        out: list[Signal] = []
        for q in questions:
            s = self._one(q, f, state)
            if s is not None:
                s.backend = self.name
                out.append(s)
        return out

    @staticmethod
    def _features(state: dict) -> dict:
        ctx = state["active_context"]
        scores = [i["score"] for i in ctx]
        hyps = sorted((h["confidence"] for h in state["hypotheses"]), reverse=True)
        meta = state["metacognition"]
        lead = hyps[0] if hyps else None
        margin = (hyps[0] - hyps[1]) if len(hyps) > 1 else (hyps[0] if hyps else 0.0)
        mean_trust = sum(i["trust"] for i in ctx) / len(ctx) if ctx else 0.0
        top = max(scores) if scores else 0.0
        return {
            "n": len(ctx), "top": top, "mean": sum(scores) / len(scores) if scores else 0.0,
            "lead": lead, "margin": margin, "n_hyp": len(hyps),
            "conflicts": len(state["conflicts"]), "trust": mean_trust,
            "funnel": state["funnel"], "repeat": meta.get("repeat_overlap", 0.0),
            "fail_streak": meta.get("fail_streak", 0), "cycle": meta.get("cycle", 0),
            "event_len": len(state["event"]["text"]),
            "novelty": meta.get("event_novelty", 0.5),
        }

    def _one(self, q: Question, f: dict, state: dict) -> Signal | None:
        n = q.name
        if n == "should_expand":
            weak = 1.0 - f["top"]
            sparse = 1.0 if f["n"] < 3 else 0.0
            unsure = 0.0 if f["lead"] is None else (1.0 - f["margin"])
            x = 2.2 * weak + 1.2 * sparse + 1.0 * unsure + 0.8 * min(1, f["conflicts"]) \
                - 0.9 * f["cycle"] - 2.0
            return Signal(n, "noul", _sig(x))
        if n == "context_conflict":
            p = 0.04 if not f["conflicts"] else min(0.97, 0.75 + 0.1 * f["conflicts"])
            return Signal(n, "noul", p)
        if n == "needs_verification":
            lead = f["lead"] if f["lead"] is not None else 0.5
            mid = 1.0 - abs(lead - 0.6) / 0.6          # peaks when the leader is uncertain
            x = 2.0 * mid + 1.5 * (1 - f["trust"]) + 1.0 * min(1, f["conflicts"]) - 1.6
            return Signal(n, "noul", _sig(x))
        if n == "context_too_large":
            fun = f["funnel"]
            ratio = fun.get("chars", 0) / 4000.0
            x = 3.0 * (ratio - 0.9) + 2.0 * max(0.0, (f["n"] - 8) / 8)
            return Signal(n, "noul", _sig(x))
        if n == "stuck":
            x = 3.0 * f["repeat"] + 0.9 * f["fail_streak"] - 3.0
            return Signal(n, "noul", _sig(x))
        if n == "next_action":
            lead = f["lead"] if f["lead"] is not None else f["top"]
            sc = {
                "inspect_top": 1.4 * lead + 0.3,
                "explore_alternative": 0.6 * (1 - f["margin"]) + 0.5 * f["repeat"]
                + 0.3 * min(3, f["fail_streak"]),
                "verify": 1.2 * (1 - f["trust"]) + 0.8 * min(1, f["conflicts"])
                + (0.5 if f["lead"] and f["lead"] > 0.85 else 0.0),
                "research_external": 1.2 * (1 - f["top"]) + (0.6 if f["n"] == 0 else 0.0),
                "done": 1.6 if (f["lead"] and f["lead"] > 0.95 and not f["conflicts"]) else 0.0,
            }
            probs = _softmax({k: v for k, v in sc.items() if k in q.options})
            top = max(probs, key=probs.get)
            return Signal(n, "choice", probs[top], top=top, probs=probs)
        if n == "remember":
            importance = min(1.0, f["event_len"] / 400.0)
            x = 2.0 * f["novelty"] + 1.0 * importance + 0.6 * min(1, f["conflicts"]) - 1.4
            v = _sig(x)
            levels = q.levels
            # triangular distribution over the ordered levels centred on v
            pos = v * (len(levels) - 1)
            raw = {lv: max(0.0, 1.0 - abs(i - pos)) for i, lv in enumerate(levels)}
            z = sum(raw.values()) or 1.0
            probs = {k: r / z for k, r in raw.items()}
            return Signal(n, "score", v, top=max(probs, key=probs.get), probs=probs)
        if n == "wake_agent":
            text = " ".join([state["event"]["text"]] + [i["title"] + " " + i["type"]
                                                         for i in state["active_context"]]).lower()
            sc = {}
            for a in state["agents"]:
                words = [w for w in a["description"].lower().replace(",", " ").split() if len(w) > 3]
                hits = sum(1 for w in set(words) if w in text)
                sc[a["name"]] = hits / max(4, len(set(words)) / 2)
            sc["none"] = 0.35
            probs = _softmax({k: v for k, v in sc.items() if k in q.options}, temp=0.15)
            top = max(probs, key=probs.get)
            return Signal(n, "choice", probs[top], top=top, probs=probs)
        if n.startswith("relevant:"):
            nid = n.split(":", 1)[1]
            item = next((i for i in state["active_context"] if i["id"] == nid), None)
            if item is None:
                return None
            rel = item["score"] / f["top"] if f["top"] else 0.0
            return Signal(n, "noul", _sig(4.0 * rel - 2.0), targets=[nid])
        return None


class JevBackend:
    """
    Sends ACA state + typed questions to Jev via a caller-provided client.

    `client(state: dict, questions: list[dict]) -> dict[name, result]` where
    each result is {"noul": p} | {"choice": {option: p}} | {"score": v,
    "levels": {level: p}}. Questions arrive as `Question.to_dict()`.
    Unknown or failed questions fall back to `fallback` (heuristic by default).
    """
    name = "jev"

    def __init__(self, client: Callable[[dict, list[dict]], dict],
                 fallback: DecisionBackend | None = None, max_parallel: int = 4,
                 batch_size: int = 16) -> None:
        self.client = client
        self.fallback = fallback or HeuristicBackend()
        self.max_parallel = max_parallel
        self.batch_size = batch_size

    def evaluate(self, state: dict, questions: list[Question]) -> list[Signal]:
        batches = [questions[i:i + self.batch_size]
                   for i in range(0, len(questions), self.batch_size)]
        results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
            futs = [pool.submit(self.client, state, [q.to_dict() for q in b]) for b in batches]
            for fut in futs:
                try:
                    results.update(fut.result() or {})
                except Exception:
                    pass
        out: list[Signal] = []
        missing: list[Question] = []
        for q in questions:
            r = results.get(q.name)
            if r is None:
                missing.append(q)
                continue
            if q.kind == "noul":
                out.append(Signal(q.name, "noul", float(r["noul"]), backend=self.name,
                                  targets=[q.name.split(":", 1)[1]] if ":" in q.name else []))
            elif q.kind == "choice":
                probs = {k: float(v) for k, v in r["choice"].items()}
                top = max(probs, key=probs.get)
                out.append(Signal(q.name, "choice", probs[top], top=top, probs=probs,
                                  backend=self.name))
            else:
                lv = {k: float(v) for k, v in (r.get("levels") or {}).items()}
                out.append(Signal(q.name, "score", float(r["score"]),
                                  top=max(lv, key=lv.get) if lv else "", probs=lv,
                                  backend=self.name))
        if missing:
            out.extend(self.fallback.evaluate(state, missing))
        return out


# ----------------------------------------------------------- firing policy
@dataclass
class Thresholds:
    latent: float = 0.30
    activate: float = 0.50
    strong: float = 0.70
    fire: float = 0.80


DEFAULT_THRESHOLDS: dict[str, Thresholds] = {
    "should_expand": Thresholds(0.3, 0.45, 0.6, 0.6),
    "context_conflict": Thresholds(0.3, 0.5, 0.7, 0.7),
    "needs_verification": Thresholds(0.3, 0.5, 0.7, 0.7),
    "context_too_large": Thresholds(0.3, 0.5, 0.7, 0.75),
    "stuck": Thresholds(0.3, 0.5, 0.7, 0.7),
    "next_action": Thresholds(0.25, 0.4, 0.55, 0.5),
    "wake_agent": Thresholds(0.25, 0.4, 0.55, 0.5),
    "remember": Thresholds(0.3, 0.45, 0.6, 0.6),
    "relevant": Thresholds(0.3, 0.5, 0.75, 0.85),
    # Destructive actions require overwhelming evidence (and verification upstream).
    "destructive": Thresholds(0.5, 0.8, 0.9, 0.97),
}


class FiringPolicy:
    def __init__(self, thresholds: dict[str, Thresholds] | None = None) -> None:
        self.thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    def classify(self, sig: Signal) -> str:
        t = self.thresholds.get(sig.name.split(":", 1)[0], Thresholds())
        v = sig.value
        if v >= t.fire:
            return "fire"
        if v >= t.strong:
            return "strong"
        if v >= t.activate:
            return "activate"
        if v >= t.latent:
            return "latent"
        return "silent"

    def apply(self, signals: list[Signal]) -> list[Signal]:
        for s in signals:
            s.level = self.classify(s)
        return signals
