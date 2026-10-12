"""
shadow_link/aca/brain.py

The ACA loop and the Working Brain.

    event ─► seeds ─► spread ─► compete/inhibit ─► converge ─► Working Brain
       ▲                                                          │
       │                                                  state builder
       │                                                          │
       └── propagate ◄── firing policy ◄── decision neurons ◄─────┘

`ACA.process(event)` runs recurrent cycles: fired neurons change what is
seeded, inhibited, or budgeted in the next cycle, until nothing new fires
or the cycle budget is spent. Everything is written to the replay log.

Also here: hypothesis network, contradiction detection, the learning loop
(reinforce/weaken the synapse paths that pulled useful/useless context),
memory formation, episodes and their consolidation into semantic memory,
and the inspector ("why is this active?").
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field

from shadow_link.aca.activation import (
    Budget, compete, converge, detect_seeds, spread,
)
from shadow_link.aca.neurons import (
    DecisionBackend, FiringPolicy, HeuristicBackend, Signal, build_state, standard_neurons,
)
from shadow_link.aca.store import CognitiveStore, term_overlap, terms

DEFAULT_AGENTS = [
    {"name": "code_agent", "description": "implement change edit code function class refactor feature"},
    {"name": "debugger", "description": "debug error exception failure crash broken traceback bug"},
    {"name": "researcher", "description": "research documentation external library unknown api version"},
    {"name": "tester", "description": "test tests failing coverage verify regression assertion"},
    {"name": "critic", "description": "review risk contradiction conflict security design tradeoff"},
]

EVENT_TYPES = {
    "prompt": "event", "observation": "observation", "error": "error",
    "tool_result": "tool_result", "file_change": "file", "test_failure": "test_result",
    "test_result": "test_result", "memory_recall": "memory", "decision": "decision",
    "unexpected_output": "observation", "agent_finding": "finding",
}


@dataclass
class WorkingBrain:
    """What the system is currently thinking about, for one task."""
    task_id: str
    goal: str = ""
    cycle: int = 0
    active: dict[str, float] = field(default_factory=dict)      # node → activation
    inhibited: list[str] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)          # hypothesis node ids
    last_pull: list[dict] = field(default_factory=list)
    last_selected: list[list[str]] = field(default_factory=list)  # history of selections
    signals: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)            # outcomes reported back
    pulled_ever: dict[str, list[str]] = field(default_factory=dict)  # node → path ids
    awakened: list[str] = field(default_factory=list)

    CARRY = 0.6       # activation retained between cycles/events

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "WorkingBrain":
        wb = cls(task_id=d["task_id"])
        for k, v in d.items():
            if hasattr(wb, k):
                setattr(wb, k, v)
        return wb

    def carried(self) -> dict[str, float]:
        return {k: v * self.CARRY for k, v in self.active.items()}

    def fail_streak(self) -> int:
        n = 0
        for a in reversed(self.actions):
            if a.get("success"):
                break
            n += 1
        return n

    def repeat_overlap(self) -> float:
        """How much the last selections keep returning the same nodes (0..1)."""
        h = self.last_selected[-3:]
        if len(h) < 2:
            return 0.0
        sims = []
        for a, b in zip(h, h[1:]):
            sa, sb = set(a), set(b)
            sims.append(len(sa & sb) / len(sa | sb) if sa | sb else 0.0)
        return sum(sims) / len(sims)


class ACA:
    """Associative Context Activation over a CognitiveStore."""

    def __init__(
        self,
        store: CognitiveStore,
        backend: DecisionBackend | None = None,
        policy: FiringPolicy | None = None,
        budget: Budget | None = None,
    ) -> None:
        self.store = store
        self.backend = backend or HeuristicBackend()
        self.policy = policy or FiringPolicy()
        self.budget = budget or Budget()

    # ------------------------------------------------------------ tasks
    def brain(self, task_id: str, goal: str = "") -> WorkingBrain:
        row = self.store.load_task(task_id)
        if row:
            wb = WorkingBrain.from_dict(row["state"])
            if goal and not wb.goal:
                wb.goal = goal
            return wb
        return WorkingBrain(task_id=task_id, goal=goal)

    def _open(self, task_id: str, goal: str = "") -> WorkingBrain:
        """Load or create a task for writing; a new task is persisted (and logged) at once."""
        wb = self.brain(task_id, goal)
        if self.store.load_task(task_id) is None:
            self._save(wb)
        return wb

    def _save(self, wb: WorkingBrain, closed: bool = False) -> None:
        # A task is "opened" when first written, so reads never create log entries.
        if self.store.load_task(wb.task_id) is None:
            self.store.log("task_opened", task_id=wb.task_id, goal=wb.goal)
        self.store.save_task(wb.task_id, wb.goal, wb.to_dict(), closed)

    def agents(self) -> list[dict]:
        return self.store.get_meta("agents", DEFAULT_AGENTS)

    def attention(self) -> dict[str, float]:
        return self.store.get_meta("attention", {})

    def _update_attention(self, acts: dict[str, float], rate: float = 0.3, keep: int = 64) -> None:
        att = self.attention()
        for k in list(att):
            att[k] *= (1 - rate)
        for k, v in acts.items():
            att[k] = att.get(k, 0.0) + rate * v
        att = dict(sorted(((k, round(v, 4)) for k, v in att.items() if v > 0.02),
                          key=lambda kv: kv[1], reverse=True)[:keep])
        self.store.set_meta("attention", att)

    # ------------------------------------------------------------ main loop
    def process(
        self,
        text: str,
        *,
        kind: str = "prompt",
        task_id: str | None = None,
        goal: str = "",
        refs: list[str] | None = None,
        project: str = "",
        budget: Budget | None = None,
        remember: bool | None = None,
    ) -> dict:
        """
        Handle one event: activate, converge, judge, propagate — recurrently.

        `remember`: True stores the event, False never does, None lets the
        `remember` neuron decide — except plain prompts, which are only stored
        when explicitly asked, so the graph does not fill with your own questions.
        Returns the converged context, the Jev state, the signals, the funnel
        and the actions the caller should take (agents to wake, verification).
        """
        with self.store.buffered_log():
            return self._process(text, kind=kind, task_id=task_id, goal=goal, refs=refs,
                                 project=project, budget=budget, remember=remember)

    def _process(self, text: str, *, kind: str, task_id: str | None, goal: str,
                 refs: list[str] | None, project: str, budget: Budget | None,
                 remember: bool | None) -> dict:
        t0 = time.time()
        self.store.sync_from_prompts()
        self.store.prune_temporary()
        if t0 - float(self.store.get_meta("events_pruned_at", 0)) > 3600:
            self.store.prune_events()
            self.store.set_meta("events_pruned_at", t0)
        task_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        wb = self._open(task_id, goal or text[:120])
        budget = budget or self.budget
        event = {"kind": kind, "text": text, "refs": refs or [], "project": project}
        self.store.log("event", task_id=task_id, cycle=wb.cycle, kind_=kind, text=text[:500])

        q_terms = terms(text)
        novelty = 1.0 - max((term_overlap(q_terms, t) for _, _, _, t in self.store.all_node_terms()),
                            default=0.0)

        extra_seeds: list[str] = list(refs or [])
        explore = False
        result: dict = {}
        cycles_run = 0
        prev_selected: list[str] = []
        selected_now: list[str] = []
        for c in range(budget.max_cycles):
            cycles_run += 1
            wb.cycle += 1
            cyc = wb.cycle
            seeds = detect_seeds(self.store, text, refs=extra_seeds, carried=wb.carried(),
                                 suppressed=set(wb.inhibited), budget=budget)
            for s in seeds:
                self.store.log("seed", task_id=task_id, cycle=cyc, node_id=s.node_id,
                               value=s.activation, reason=s.reason)
            acts, traversals = spread(self.store, seeds, budget, self.attention())
            for tr in sorted(traversals, key=lambda t: -abs(t["amount"]))[:40]:
                self.store.log("traverse", task_id=task_id, cycle=cyc, node_id=tr["to"],
                               value=tr["amount"], frm=tr["from"], rel=tr["rel"])
            cands = compete(self.store, acts, project=project, inhibited=set(wb.inhibited))
            for cand in cands:
                if cand.state == "suppressed" and cand.act.activation > 0.1:
                    self.store.log("suppressed", task_id=task_id, cycle=cyc, node_id=cand.id,
                                   value=cand.score, reasons=cand.suppressors)
            items, funnel = converge(self.store, cands, budget, explore=explore)
            self.store.log("converged", task_id=task_id, cycle=cyc, value=len(items),
                           funnel=funnel, selected=[i["id"] for i in items])
            for i in items:
                if i["conflicts"]:
                    self.store.log("contradiction_active", task_id=task_id, cycle=cyc,
                                   node_id=i["id"], with_=i["conflicts"])

            # Working Brain update (recurrent activation carry).
            wb.active = {cand.id: round(cand.act.activation, 4)
                         for cand in cands if cand.state != "suppressed"}
            wb.active = dict(sorted(wb.active.items(), key=lambda kv: -kv[1])[:budget.max_activated // 4])
            wb.last_pull = [{k: v for k, v in i.items() if k != "_path_ids"} for i in items]
            selected_now = [i["id"] for i in items]
            for i in items:
                wb.pulled_ever[i["id"]] = i["_path_ids"]
            self.store.touch(i["id"] for i in items)

            hyps = self._hypotheses(wb)
            metacog = {
                "cycle": c, "fail_streak": wb.fail_streak(),
                "repeat_overlap": round(wb.repeat_overlap(), 3),
                "event_novelty": round(novelty, 3),
                "budget": budget.to_dict(),
                "elapsed_ms": int((time.time() - t0) * 1000),
            }
            state = build_state(event=event, goal=wb.goal, context=wb.last_pull,
                                hypotheses=hyps, funnel=funnel, metacog=metacog,
                                agents=self.agents())
            questions = standard_neurons(state)[: budget.max_neurons]
            signals = self.policy.apply(self.backend.evaluate(state, questions))
            for s in signals:
                self.store.log("judgment", task_id=task_id, cycle=cyc, node_id=",".join(s.targets),
                               value=s.value, name=s.name, level=s.level, top=s.top,
                               backend=s.backend)
            wb.signals = [s.to_dict() for s in signals]
            result = {"items": items, "funnel": funnel, "state": state, "signals": signals,
                      "hypotheses": hyps}

            # ---- propagation: fired signals reshape the next cycle
            sig = {s.name: s for s in signals}
            changed = False
            for s in signals:
                if s.name.startswith("relevant:") and s.level == "silent":
                    nid = s.targets[0]
                    if nid not in wb.inhibited:
                        wb.inhibited.append(nid)
                        self.store.log("inhibit", task_id=task_id, cycle=cyc, node_id=nid,
                                       value=s.value, reason="judged irrelevant")
                        changed = True
            exp = sig.get("should_expand")
            if exp and exp.level in ("strong", "fire") and c + 1 < budget.max_cycles:
                budget = budget.expanded()
                latent = [cand.id for cand in cands if cand.state == "latent"][:4]
                extra_seeds = list(dict.fromkeys(extra_seeds + latent))
                self.store.log("expand", task_id=task_id, cycle=cyc, value=exp.value,
                               new_budget=budget.to_dict(), seeded=latent)
                changed = True
            nxt = sig.get("next_action")
            if nxt and nxt.top == "explore_alternative" and nxt.level in ("strong", "fire") \
                    and not explore:
                explore = True
                self.store.log("explore", task_id=task_id, cycle=cyc, value=nxt.value)
                changed = True
            if not changed or (c > 0 and selected_now == prev_selected):
                break   # nothing fired, or the context field has converged
            prev_selected = selected_now

        # Selection history is per event (not per internal cycle) so the
        # metacognitive "stuck" signal measures progress across the task.
        wb.last_selected.append(selected_now)
        wb.last_selected = wb.last_selected[-10:]

        # ---- outputs that leave the loop
        signals: list[Signal] = result["signals"]
        sig = {s.name: s for s in signals}
        actions: list[dict] = []
        wake = sig.get("wake_agent")
        if wake and wake.top != "none" and wake.level in ("activate", "strong", "fire"):
            if wake.top not in wb.awakened:
                wb.awakened.append(wake.top)
            actions.append({"type": "wake_agent", "agent": wake.top, "p": round(wake.value, 3)})
            self.store.log("agent_awakened", task_id=task_id, cycle=wb.cycle, value=wake.value,
                           agent=wake.top)
        ver = sig.get("needs_verification")
        if ver and ver.level in ("strong", "fire"):
            actions.append({"type": "verify", "p": round(ver.value, 3)})
        nxt = sig.get("next_action")
        if nxt:
            actions.append({"type": "next_action", "action": nxt.top, "p": round(nxt.value, 3)})

        memory_id = None
        rem = sig.get("remember")
        if rem and remember is not False and (kind != "prompt" or remember is True):
            memory_id = self._form_memory(text, kind, rem, result["items"], project, task_id, wb,
                                          forced=remember is True)

        self._update_attention({i["id"]: i["activation"] for i in result["items"]})
        self._save(wb)
        return {
            "task_id": task_id,
            "cycles": cycles_run,
            "funnel": result["funnel"],
            "context": [{k: v for k, v in i.items() if k != "_path_ids"} for i in result["items"]],
            "hypotheses": result["hypotheses"],
            "signals": [s.to_dict() for s in signals],
            "actions": actions,
            "memory": memory_id,
            "jev_state": result["state"],
            "backend": self.backend.name,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }

    # ----------------------------------------------------- memory formation
    def _form_memory(self, text: str, kind: str, rem: Signal, items: list[dict],
                     project: str, task_id: str, wb: WorkingBrain,
                     forced: bool = False) -> str | None:
        decision = "store" if forced else rem.top or (
            "store" if rem.value >= 0.66 else "temporary" if rem.value >= 0.33 else "discard")
        self.store.log("memory_decision", task_id=task_id, cycle=wb.cycle, value=rem.value,
                       decision=decision)
        if decision == "discard" or not text.strip():
            return None
        ntype = EVENT_TYPES.get(kind, "observation")
        nid = self.store.add_node(
            text, type=ntype, title=text[:80], importance=rem.value, project=project,
            status="active" if decision == "store" else "temporary",
            source=f"aca:{kind}", metadata={"task_id": task_id},
        )
        # Hebbian formation: the new memory is wired to what was co-active with it.
        for i in items[:5]:
            self.store.link(nid, i["id"], "co_active", strength=0.25 + 0.5 * i["score"],
                            confidence=0.6, source="hebbian")
        self.detect_contradictions(nid, task_id=task_id)
        self.store.log("memory_formed", task_id=task_id, cycle=wb.cycle, node_id=nid,
                       value=rem.value, decision=decision)
        return nid

    def detect_contradictions(self, node_id: str, *, task_id: str = "",
                              min_overlap: float = 0.45) -> list[str]:
        """
        Flag existing nodes that talk about the same thing with opposite
        polarity ("rotation is required" vs "rotation is not required").
        Contradictions are linked, never silently overwritten.
        """
        n_terms = self.store._terms_cache.get(node_id)
        if not n_terms:
            return []
        found = []
        for nid, _t, status, t in self.store.all_node_terms():
            if nid == node_id or status in ("archived",):
                continue
            if ("not" in n_terms) == ("not" in t):
                continue
            ov = term_overlap(n_terms, t)
            if ov >= min_overlap:
                self.store.link(node_id, nid, "contradicts", strength=0.6 + 0.4 * ov,
                                confidence=round(ov, 3), source="polarity")
                self.store.log("contradiction_found", task_id=task_id, node_id=node_id,
                               value=ov, other=nid)
                found.append(nid)
        return found

    # ---------------------------------------------------------- hypotheses
    def add_hypothesis(self, task_id: str, text: str, prior: float = 0.5,
                       about: list[str] | None = None) -> str:
        wb = self._open(task_id)
        hid = self.store.add_node(text, type="hypothesis", title=text[:80], trust=prior,
                                  importance=prior, metadata={"task_id": task_id,
                                                              "confidence": prior})
        for a in about or []:
            self.store.link(hid, a, "about", strength=0.7, source="hypothesis")
        wb.hypotheses.append(hid)
        self.store.log("hypothesis_created", task_id=task_id, node_id=hid, value=prior, text=text)
        self._save(wb)
        return hid

    def add_evidence(self, task_id: str, hypothesis_id: str, evidence: str | None = None, *,
                     evidence_id: str | None = None, supports: bool = True,
                     weight: float = 1.0, kind: str = "observation") -> dict:
        """
        Bayesian-style update in log-odds space, scaled by the evidence's trust:
            logit(h) += ±weight · 1.5 · trust(e)
        Below 0.05 the hypothesis is eliminated (status=refuted).
        """
        h = self.store.get_node(hypothesis_id)
        if not h or h["type"] != "hypothesis":
            raise ValueError(f"{hypothesis_id} is not a hypothesis")
        if evidence_id is None:
            if not evidence:
                raise ValueError("Provide evidence text or evidence_id")
            evidence_id = self.store.add_node(evidence, type=EVENT_TYPES.get(kind, "observation"),
                                              source="evidence", metadata={"task_id": task_id})
        e = self.store.get_node(evidence_id)
        conf = float(h["metadata"].get("confidence", h["trust"]))
        conf = min(max(conf, 1e-4), 1 - 1e-4)
        logit = math.log(conf / (1 - conf))
        delta = (1 if supports else -1) * weight * 1.5 * float(e["trust"])
        new = 1 / (1 + math.exp(-(logit + delta)))
        meta = {**h["metadata"], "confidence": round(new, 4)}
        status = "refuted" if new < 0.05 else h["status"]
        self.store.update_node(hypothesis_id, metadata=meta, trust=new, importance=new, status=status)
        self.store.link(evidence_id, hypothesis_id, "supports" if supports else "refutes",
                        strength=min(1.0, 0.4 + 0.4 * weight), confidence=float(e["trust"]),
                        source="evidence")
        self.store.log("hypothesis_" + ("strengthened" if supports else "weakened"),
                       task_id=task_id, node_id=hypothesis_id, value=new, prior=conf,
                       evidence=evidence_id)
        if status == "refuted":
            self.store.log("hypothesis_eliminated", task_id=task_id, node_id=hypothesis_id, value=new)
        return {"hypothesis": hypothesis_id, "prior": round(conf, 4), "posterior": round(new, 4),
                "status": status, "evidence": evidence_id}

    def _hypotheses(self, wb: WorkingBrain) -> list[dict]:
        nodes = self.store.get_nodes(wb.hypotheses)
        out = []
        for hid in wb.hypotheses:
            n = nodes.get(hid)
            if n:
                out.append({"id": hid, "title": n["title"], "status": n["status"],
                            "confidence": float(n["metadata"].get("confidence", n["trust"]))})
        return sorted(out, key=lambda h: -h["confidence"])

    # ------------------------------------------------------- learning loop
    def feedback(self, task_id: str, node_id: str, useful: bool, lr: float = 0.15) -> dict:
        """
        Did pulled context actually help? Reinforce (or weaken) every synapse on
        the path that activated it, record usefulness on the node, and connect
        co-useful nodes so they activate together next time.
        """
        wb = self._open(task_id)
        path = wb.pulled_ever.get(node_id, [node_id])
        self.store.mark_usefulness(node_id, useful)
        changed = []
        for a, b in zip(path, path[1:]):
            for e in self.store.neighbors(a):
                other = e["src"] if e["reverse"] else e["dst"]
                if other != b:
                    continue
                src, dst = (e["src"], e["dst"])
                s = self.store.adjust_synapse(src, dst, e["rel"], useful, lr)
                if s is not None:
                    changed.append({"src": src, "dst": dst, "rel": e["rel"], "strength": round(s, 4)})
                break
        if useful:
            useful_before = [a["useful_node"] for a in wb.actions if a.get("useful_node")]
            for other in useful_before[-5:]:
                if other != node_id:
                    self.store.link(node_id, other, "co_active", strength=0.4,
                                    confidence=0.7, source="co-useful")
            wb.actions.append({"useful_node": node_id, "success": True, "ts": time.time()})
        elif node_id not in wb.inhibited:
            wb.inhibited.append(node_id)
        self.store.log("feedback", task_id=task_id, node_id=node_id, value=1.0 if useful else 0.0,
                       path=path, changed=changed)
        self._save(wb)
        return {"node": node_id, "useful": useful, "path": path, "synapses": changed}

    def report_action(self, task_id: str, action: str, success: bool, detail: str = "") -> None:
        """Outcome of an action taken outside ACA — feeds the metacognitive layer."""
        wb = self._open(task_id)
        wb.actions.append({"action": action, "success": success, "detail": detail[:300],
                           "ts": time.time()})
        self.store.log("action", task_id=task_id, value=1.0 if success else 0.0,
                       action=action, detail=detail[:300])
        self._save(wb)

    # ------------------------------------------------- episodes + consolidation
    def close_task(self, task_id: str, *, result: str, lesson: str = "",
                   procedure: str = "") -> dict:
        """
        Store the task as an episode linked to everything that mattered, then
        consolidate its lesson into semantic memory (and optionally a procedure).
        """
        wb = self._open(task_id)
        hyps = self._hypotheses(wb)
        useful = [a["useful_node"] for a in wb.actions if a.get("useful_node")]
        involved = list(dict.fromkeys(useful + list(wb.pulled_ever)))[:40]
        lines = [f"goal: {wb.goal}", f"result: {result}"]
        if hyps:
            lines.append("hypotheses: " + "; ".join(f"{h['title']} ({h['confidence']:.2f}, {h['status']})"
                                                    for h in hyps))
        if useful:
            lines.append("useful context: " + ", ".join(useful))
        if lesson:
            lines.append(f"lesson: {lesson}")
        ep = self.store.add_node("\n".join(lines), type="episode", title=f"Episode: {wb.goal[:70]}",
                                 importance=0.7, source=f"task:{task_id}",
                                 metadata={"task_id": task_id, "cycles": wb.cycle})
        for nid in involved:
            self.store.link(nid, ep, "involved_in",
                            strength=0.8 if nid in useful else 0.4, source="episode")
        for h in hyps:
            self.store.link(h["id"], ep, "involved_in", strength=0.5, source="episode")
        out = {"episode": ep, "involved": len(involved)}
        if lesson:
            sem = self.store.add_node(lesson, type="semantic", title=lesson[:80], importance=0.8,
                                      source=f"consolidated:{ep}")
            self.store.link(sem, ep, "derived_from", strength=0.9, source="consolidation")
            for nid in useful:
                self.store.link(sem, nid, "related_to", strength=0.6, source="consolidation")
            self.detect_contradictions(sem, task_id=task_id)
            out["semantic"] = sem
        if procedure:
            proc = self.store.add_node(procedure, type="procedure", title=procedure[:80],
                                       importance=0.8, source=f"consolidated:{ep}")
            self.store.link(proc, ep, "derived_from", strength=0.9, source="consolidation")
            out["procedure"] = proc
        self.store.log("episode_stored", task_id=task_id, node_id=ep, **out)
        self._save(wb, closed=True)
        return out

    # ---------------------------------------------------------- inspector
    def inspect(self, task_id: str, node_id: str) -> dict:
        """WHY IS THIS ACTIVE? — activation, path, evidence, and signals for one node."""
        wb = self.brain(task_id)
        n = self.store.get_node(node_id)
        if not n:
            raise ValueError(f"No node {node_id}")
        pulled = next((i for i in wb.last_pull if i["id"] == node_id), None)
        evidence: dict[str, int] = {}
        for e in self.store.neighbors(node_id):
            if e["reverse"]:
                other = self.store.get_node(e["src"])
                if other:
                    evidence[other["type"]] = evidence.get(other["type"], 0) + 1
        sigs = [s for s in wb.signals if node_id in s.get("targets", [])]
        return {
            "node": {k: n[k] for k in ("id", "type", "title", "content", "trust", "importance",
                                       "status", "source", "use_count", "useful_count",
                                       "useless_count")},
            "activation": wb.active.get(node_id, 0.0),
            "in_context": pulled is not None,
            "why": pulled["why"] if pulled else None,
            "conflicts": pulled["conflicts"] if pulled else [],
            "provenance": pulled["provenance"] if pulled else [],
            "incoming_evidence": evidence,
            "signals": sigs,
            "inhibited": node_id in wb.inhibited,
        }

    def snapshot(self, task_id: str) -> dict:
        """The Working Brain as a panel: goal, active nodes, hypotheses, signals, attention."""
        wb = self.brain(task_id)
        nodes = self.store.get_nodes(list(wb.active)[:30])
        return {
            "task_id": task_id, "goal": wb.goal, "cycle": wb.cycle,
            "active": [{"id": k, "type": nodes[k]["type"], "title": nodes[k]["title"],
                        "activation": v} for k, v in list(wb.active.items())[:30] if k in nodes],
            "hypotheses": self._hypotheses(wb),
            "signals": wb.signals,
            "awakened_agents": wb.awakened,
            "inhibited": wb.inhibited,
            "attention": dict(list(self.attention().items())[:15]),
            "metacognition": {"fail_streak": wb.fail_streak(),
                              "repeat_overlap": round(wb.repeat_overlap(), 3)},
        }

    def stats(self) -> dict:
        return {"nodes": self.store.count_nodes(), "synapses": self.store.count_synapses(),
                "backend": self.backend.name}
