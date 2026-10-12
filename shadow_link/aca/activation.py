"""
shadow_link/aca/activation.py

The activation pipeline:

    seed detection → spreading activation → competition + inhibition
                   → context pull → context convergence

Every surviving object carries *why* it is active: the seed that started it,
the synapse path that reached it, and the factor breakdown of its score.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field

from shadow_link.aca.store import (
    HALF_LIFE_HOURS, INHIBITORY_RELS, CognitiveStore, term_overlap, terms,
)


# ---------------------------------------------------------------- budgets
@dataclass
class Budget:
    """Hard limits on how much ACA may activate in one cycle."""
    max_depth: int = 3            # graph hops from a seed
    max_seeds: int = 12
    max_activated: int = 300      # nodes allowed to hold activation
    max_selected: int = 8         # converged context objects
    max_chars: int = 4000         # converged context size
    min_transmit: float = 0.05    # activation too small to keep spreading
    hop_decay: float = 0.82       # activation kept per hop on a perfect synapse
    max_cycles: int = 3           # recurrent firing cycles per event
    max_neurons: int = 32         # decision questions per cycle

    def expanded(self) -> "Budget":
        """Spend more when uncertainty is high."""
        return Budget(
            max_depth=self.max_depth + 1, max_seeds=self.max_seeds + 4,
            max_activated=int(self.max_activated * 1.5),
            max_selected=self.max_selected + 3, max_chars=int(self.max_chars * 1.4),
            min_transmit=self.min_transmit * 0.7, hop_decay=self.hop_decay,
            max_cycles=self.max_cycles, max_neurons=self.max_neurons,
        )

    def to_dict(self) -> dict:
        return dict(self.__dict__)


# Thresholds separating active / latent / suppressed after competition.
ACTIVE_THRESHOLD = 0.30
LATENT_THRESHOLD = 0.12
OUTCOMPETED_RATIO = 0.20      # below this fraction of the winner → outcompeted
DUPLICATE_OVERLAP = 0.80


# ------------------------------------------------------------------ seeds
_PATH_RE = re.compile(r"(?:[\w.-]+/)*[\w.-]+\.[a-zA-Z]{1,5}\b")
_ERR_RE = re.compile(r"\b[A-Z]\w*(?:Error|Exception|Failure|Fault)\b")
_IDENT_RE = re.compile(r"\b(?:[a-z]+_[a-z0-9_]+|[a-z]+[A-Z]\w+|[A-Z][a-z]+[A-Z]\w+)\b")
_QUOTED_RE = re.compile(r"[`'\"]([^`'\"]{3,60})[`'\"]")


def extract_entities(text: str) -> list[str]:
    """Names that should match nodes exactly: files, errors, identifiers, quoted terms."""
    found: list[str] = []
    for rx in (_PATH_RE, _ERR_RE, _IDENT_RE):
        found.extend(m.group(0) for m in rx.finditer(text or ""))
    found.extend(m.group(1) for m in _QUOTED_RE.finditer(text or ""))
    seen, out = set(), []
    for f in found:
        k = f.lower()
        if k not in seen and not k.replace(".", "").isdigit():
            seen.add(k)
            out.append(f)
    return out


@dataclass
class Seed:
    node_id: str
    activation: float
    reason: str


def detect_seeds(
    store: CognitiveStore,
    text: str,
    *,
    refs: list[str] | None = None,
    carried: dict[str, float] | None = None,
    suppressed: set[str] | None = None,
    budget: Budget | None = None,
    floor: float = 0.25,
) -> list[Seed]:
    """
    Decide what becomes initially active for an event.

    Sources, strongest first:
      1. explicit references (node ids attached to the event)
      2. entity matches (file names, error types, identifiers) against node titles
      3. lexical containment: share of the event's content terms present in a node
      4. carried activation from the Working Brain (recurrence)
    """
    budget = budget or Budget()
    suppressed = suppressed or set()
    seeds: dict[str, Seed] = {}

    def put(nid: str, a: float, reason: str) -> None:
        if nid in suppressed:
            return
        cur = seeds.get(nid)
        if cur is None or a > cur.activation:
            seeds[nid] = Seed(nid, min(1.0, a), reason)

    for nid in refs or []:
        if store.get_node(nid):
            put(nid, 1.0, "explicit reference")

    for ent in extract_entities(text):
        for nid in store.find_by_title(ent, limit=3):
            put(nid, 0.95, f"entity match '{ent}'")

    q = terms(text)
    if q:
        q_pre = {t[:4] for t in q if len(t) >= 4}
        scored: list[tuple[float, str]] = []
        for nid, _type, status, nterms in store.all_node_terms():
            if not nterms or status in ("archived",):
                continue
            exact = q & nterms
            # Soft match on 4-char prefixes ("auth" ~ "authentication"), worth 0.6 of exact.
            soft = {t for t in q - exact if len(t) >= 4 and t[:4] in store.prefixes(nid)}
            if not exact and not soft:
                continue
            hits = len(exact) + 0.6 * len(soft)
            containment = hits / len(q)
            jac = hits / len(q | nterms)
            # Polarity mismatch ("use docker" vs "do not use docker") halves the match.
            if ("not" in q) != ("not" in nterms):
                containment *= 0.5
            s = 0.75 * containment + 0.25 * min(1.0, jac * 3)
            if s >= floor:
                scored.append((s, nid))
        scored.sort(reverse=True)
        for s, nid in scored[: budget.max_seeds]:
            put(nid, s, f"lexical match {s:.2f}")

    for nid, a in (carried or {}).items():
        if a >= LATENT_THRESHOLD:
            put(nid, a, "carried from working brain")

    ordered = sorted(seeds.values(), key=lambda s: s.activation, reverse=True)
    return ordered[: budget.max_seeds + len(refs or [])]


# ------------------------------------------------------ spreading activation
@dataclass
class Activation:
    node_id: str
    activation: float = 0.0
    inhibition: float = 0.0
    depth: int = 0
    seed: str = ""
    seed_reason: str = ""
    path: list[tuple[str, str]] = field(default_factory=list)  # [(rel, node_id), ...]
    conflicts: set[str] = field(default_factory=set)


def synapse_efficacy(syn: dict, now: float | None = None) -> float:
    """
    How much activation a synapse carries:
        strength · confidence · recency · reliability
    reliability is the Laplace-smoothed success rate, centred so an unused
    synapse is neutral (×1) and a repeatedly useful one can exceed it.
    """
    now = now or time.time()
    s, c = float(syn["strength"]), float(syn["confidence"])
    last = syn.get("last_used_at") or syn.get("created_at") or now
    age_days = max(0.0, (now - last) / 86400.0)
    recency = 0.6 + 0.4 * math.exp(-age_days / 30.0)
    succ, fail = int(syn.get("success_count", 0)), int(syn.get("fail_count", 0))
    reliability = 0.5 + (succ + 1) / (succ + fail + 2)
    freq = 1.0 + 0.05 * min(10, int(syn.get("frequency", 1)) - 1)
    eff = s * c * recency * reliability * freq
    if syn.get("reverse"):
        eff *= 0.7   # walking a directed relation backwards carries less
    return max(0.0, min(1.0, eff))


def spread(
    store: CognitiveStore,
    seeds: list[Seed],
    budget: Budget,
    attention: dict[str, float] | None = None,
) -> tuple[dict[str, Activation], list[dict]]:
    """
    Layered spreading activation from the seeds.

    Activation into a node combines across paths by noisy-OR
    (a = 1 − Π(1 − a_i)) so many weak paths add up but never exceed 1.
    Fan-out dampens each transmission (ACT-R style): a hub that links to
    everything lends little activation to any single neighbour.
    Inhibitory relations (supersedes, refutes) push negative activation.
    Returns the activation map and the list of traversals (for replay).
    """
    attention = attention or {}
    now = time.time()
    acts: dict[str, Activation] = {}
    traversals: list[dict] = []
    for s in seeds:
        acts[s.node_id] = Activation(s.node_id, s.activation, 0.0, 0, s.node_id, s.reason)

    frontier = [s.node_id for s in seeds]
    expanded: set[str] = set()
    for depth in range(1, budget.max_depth + 1):
        incoming: dict[str, list[tuple[float, str, str, str]]] = {}
        layer = [u for u in dict.fromkeys(frontier)
                 if u not in expanded and acts[u].activation >= budget.min_transmit]
        edges_by_node = store.neighbors_many(layer)
        for u in layer:
            expanded.add(u)
            au = acts[u]
            edges = edges_by_node.get(u, [])
            fan = max(1, len(edges))
            fan_damp = 1.0 / math.sqrt(max(1.0, fan / 4.0))
            for e in edges:
                v = e["src"] if e["reverse"] else e["dst"]
                eff = synapse_efficacy(e, now)
                amount = au.activation * budget.hop_decay * eff * fan_damp
                amount *= 1.0 + 0.3 * attention.get(v, 0.0)
                if e["rel"] in INHIBITORY_RELS and not e["reverse"]:
                    # u supersedes/refutes v: v is inhibited, not excited.
                    av = acts.setdefault(v, Activation(v, 0.0, 0.0, depth, au.seed, au.seed_reason,
                                                       au.path + [(e["rel"], v)]))
                    av.inhibition = min(1.0, av.inhibition + amount)
                    traversals.append({"from": u, "to": v, "rel": e["rel"], "amount": -round(amount, 4)})
                    continue
                if e["rel"] == "contradicts":
                    acts[u].conflicts.add(v)
                    acts.setdefault(v, Activation(v, 0.0, 0.0, depth, au.seed, au.seed_reason,
                                                  au.path + [(e["rel"], v)])).conflicts.add(u)
                if amount < budget.min_transmit * 0.5:
                    continue
                rel = e["rel"] + ("⁻¹" if e["reverse"] else "")
                incoming.setdefault(v, []).append((amount, u, rel, au.seed))
                traversals.append({"from": u, "to": v, "rel": rel, "amount": round(amount, 4)})

        next_frontier: list[str] = []
        for v, contribs in incoming.items():
            if v not in acts and len(acts) >= budget.max_activated:
                continue
            av = acts.get(v)
            if av is None:
                av = Activation(v, depth=depth)
                acts[v] = av
            keep = 1.0 - av.activation
            for amount, *_ in contribs:
                keep *= (1.0 - amount)
            new_a = 1.0 - keep
            best = max(contribs, key=lambda t: t[0])
            is_seed = av.depth == 0 and av.seed == v
            if not is_seed and (not av.path or (av.activation < best[0] and av.depth >= depth)):
                src = acts[best[1]]
                av.path = src.path + [(best[2], v)] if src.path else [(best[2], v)]
                av.seed = src.seed or best[1]
                av.seed_reason = src.seed_reason
                av.depth = depth
            av.activation = new_a
            if av.activation >= budget.min_transmit:
                next_frontier.append(v)
        frontier = next_frontier
        if not frontier:
            break
    return acts, traversals


# ------------------------------------------------ competition + inhibition
@dataclass
class Candidate:
    node: dict
    act: Activation
    score: float
    factors: dict
    state: str = "latent"            # active | latent | suppressed
    suppressors: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.node["id"]


def freshness(node: dict, now: float | None = None) -> float:
    now = now or time.time()
    hl = HALF_LIFE_HOURS.get(node["type"], 24 * 7)
    if math.isinf(hl):
        return 1.0
    ref = node.get("last_used_at") or node["created_at"]
    age_h = max(0.0, (now - ref) / 3600.0)
    return math.exp(-math.log(2) * age_h / hl)


def compete(
    store: CognitiveStore,
    acts: dict[str, Activation],
    *,
    project: str = "",
    inhibited: set[str] | None = None,
) -> list[Candidate]:
    """
    Turn raw activation into competing candidates and apply suppressors:
    status (superseded / incorrect / refuted), inhibitory synapses, staleness,
    low trust, other project, duplicates, explicit inhibition from the Working
    Brain, and being out-competed by the winner.
    """
    inhibited = inhibited or set()
    nodes = store.get_nodes(acts.keys())
    now = time.time()
    cands: list[Candidate] = []
    for nid, a in acts.items():
        n = nodes.get(nid)
        if n is None:
            continue
        fresh = freshness(n, now)
        uf, ul = n["useful_count"], n["useless_count"]
        usefulness = (uf + 1) / (uf + ul + 2)
        factors = {
            "activation": round(a.activation, 4),
            "freshness": round(0.4 + 0.6 * fresh, 4),
            "importance": round(0.55 + 0.45 * n["importance"], 4),
            "trust": round(0.45 + 0.55 * n["trust"], 4),
            "usefulness": round(1.0 + 0.8 * (usefulness - 0.5), 4),   # unused → neutral
            "inhibition": round(1.0 - a.inhibition, 4),
        }
        score = 1.0
        for v in factors.values():
            score *= v
        sup: list[str] = []
        if n["status"] in ("superseded", "incorrect", "refuted"):
            score *= 0.15
            sup.append(f"status:{n['status']}")
        if n["status"] == "archived":
            score *= 0.05
            sup.append("archived")
        if a.inhibition > 0.2:
            sup.append(f"inhibited {a.inhibition:.2f}")
        if fresh < 0.05:
            sup.append("stale")
        if n["trust"] < 0.3:
            sup.append("low trust")
        if project and n["project"] and n["project"] != project:
            score *= 0.25
            sup.append(f"other project '{n['project']}'")
        if nid in inhibited:
            score *= 0.1
            sup.append("inhibited by working brain")
        cands.append(Candidate(n, a, score, factors, suppressors=sup))

    cands.sort(key=lambda c: c.score, reverse=True)
    top = cands[0].score if cands else 0.0
    kept_terms: list[tuple[str, set[str]]] = []
    for c in cands:
        if top > 0 and c.score < top * OUTCOMPETED_RATIO:
            c.suppressors.append("outcompeted")
        nterms = store._terms_cache.get(c.id) or set()
        if nterms:
            for kid, kt in kept_terms:
                if term_overlap(nterms, kt) >= DUPLICATE_OVERLAP:
                    c.score *= 0.2
                    c.suppressors.append(f"duplicate of {kid}")
                    break
            else:
                if c.score >= LATENT_THRESHOLD:
                    kept_terms.append((c.id, nterms))
        if c.score >= ACTIVE_THRESHOLD and not c.suppressors:
            c.state = "active"
        elif c.score >= LATENT_THRESHOLD and not any(
                s.startswith(("status:", "duplicate", "inhibited by")) for s in c.suppressors):
            c.state = "latent"
        else:
            c.state = "suppressed"
    cands.sort(key=lambda c: c.score, reverse=True)
    return cands


# ------------------------------------------------- pull + convergence
def _pull_item(store: CognitiveStore, c: Candidate, nodes_by_id: dict[str, dict],
               reason: str = "") -> dict:
    n = c.node
    path_ids = [c.act.seed] + [nid for _, nid in c.act.path] if c.act.seed else [c.id]
    titles = {k: v["title"] for k, v in nodes_by_id.items()}
    path = []
    if c.act.seed:
        path.append({"node": c.act.seed, "title": titles.get(c.act.seed, c.act.seed), "via": "seed"})
    for rel, nid in c.act.path:
        path.append({"node": nid, "title": titles.get(nid, nid), "via": rel})
    content = n["content"]
    return {
        "id": c.id,
        "type": n["type"],
        "title": n["title"],
        "content": content if len(content) <= 1200 else content[:1200] + "…",
        "score": round(c.score, 4),
        "activation": round(c.act.activation, 4),
        "state": c.state,
        "trust": n["trust"],
        "source": n["source"],
        "why": {
            "seed": c.act.seed,
            "seed_reason": c.act.seed_reason,
            "depth": c.act.depth,
            "path": path,
            "factors": c.factors,
            "selected_because": reason or "top score after competition",
        },
        "conflicts": sorted(c.act.conflicts),
        "provenance": provenance(store, c.id),
        "_path_ids": path_ids,
    }


def provenance(store: CognitiveStore, node_id: str, max_hops: int = 4) -> list[dict]:
    """Follow derived_from / evidence_for / discussed_in back toward the original source."""
    chain, cur, seen = [], node_id, {node_id}
    for _ in range(max_hops):
        nxt = None
        for e in store.neighbors(cur):
            if not e["reverse"] and e["rel"] in ("derived_from", "discussed_in", "observed_in"):
                nxt = (e["rel"], e["dst"])
                break
        if not nxt or nxt[1] in seen:
            break
        n = store.get_node(nxt[1])
        if not n:
            break
        chain.append({"rel": nxt[0], "node": n["id"], "type": n["type"],
                      "title": n["title"], "source": n["source"]})
        seen.add(n["id"])
        cur = n["id"]
    return chain


def converge(
    store: CognitiveStore,
    cands: list[Candidate],
    budget: Budget,
    *,
    explore: bool = False,
    diversity: float = 0.25,
) -> tuple[list[dict], dict]:
    """
    Pick the smallest, strongest, least redundant set of context objects.

    Greedy MMR: each pick maximises score − diversity·(max overlap with picks).
    Contradiction partners of a pick are pulled in together so a conflict is
    never seen from one side only. With `explore`, one slot is reserved for
    the best latent node reached from a *different* seed than the leader.
    """
    pool = [c for c in cands if c.state in ("active", "latent")]
    by_id = {c.id: c for c in cands}
    nodes_by_id = {c.id: c.node for c in cands}
    picked: list[tuple[Candidate, str]] = []
    chars = 0

    def overlap_with_picked(c: Candidate) -> float:
        t = store._terms_cache.get(c.id) or set()
        return max((term_overlap(t, store._terms_cache.get(p.id) or set()) for p, _ in picked),
                   default=0.0)

    slots = budget.max_selected - (1 if explore else 0)
    remaining = list(pool)
    while remaining and len(picked) < slots:
        best = max(remaining, key=lambda c: c.score - diversity * overlap_with_picked(c))
        remaining.remove(best)
        size = len(best.node["content"])
        if picked and chars + size > budget.max_chars:
            continue
        picked.append((best, "top score after competition" if best.state == "active"
                       else "latent but strongest remaining"))
        chars += size
        for partner in sorted(best.act.conflicts):
            pc = by_id.get(partner)
            if pc and pc not in [p for p, _ in picked] and len(picked) < budget.max_selected:
                picked.append((pc, f"contradicts {best.id}"))
                if pc in remaining:
                    remaining.remove(pc)

    if explore and picked:
        lead_seed = picked[0][0].act.seed
        alt = [c for c in pool if c not in [p for p, _ in picked] and c.act.seed != lead_seed]
        if alt:
            picked.append((alt[0], f"exploration: different neighbourhood (seed {alt[0].act.seed})"))

    items = [_pull_item(store, c, nodes_by_id, why) for c, why in picked]
    funnel = {
        "graph_nodes": store.count_nodes(),
        "activated": sum(1 for c in cands if c.act.activation > 0),
        "significant": sum(1 for c in cands if c.state in ("active", "latent")),
        "highly_relevant": sum(1 for c in cands if c.state == "active"),
        "critical": len(items),
        "suppressed": sum(1 for c in cands if c.state == "suppressed"),
        "chars": sum(len(i["content"]) for i in items),
    }
    return items, funnel
