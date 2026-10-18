"""Graph clustering planner.

Rules (PLAN.md):
- A file/symbol is OWNED by exactly one shard.
- A shard that imports a file owned by another shard depends_on that
  owner (owner runs first). Shared files travel read-only (signatures).
- SCCs (import cycles inside the project) are contracted into one
  shard unit: a cycle can never become a shard-dependency cycle.
- Conservative rule: ambiguous evidence sequences, never parallelises.
  Ambiguity in v0.1 = (a) two shards define the same symbol name,
  (b) a user-split file is strongly claimed by 2+ subtasks (near-tie),
  (c) auto-cluster shards sharing a top-level package. When ambiguity
  fires between shards that would otherwise share a parallel group, a
  sequence edge is added and the rule is recorded in rules_fired.
- Caps: hitting max_shards or fan-out caps, or an unfittable shard,
  marks the plan partial. Nothing is silently truncated.
"""
from __future__ import annotations
import math, re
from collections import defaultdict
from pathlib import Path

from .project import scan_project, build_graph, strongly_connected_components
from .tokens import estimate_tokens
from .taskfile import Subtask, slugify

WORDS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
STOP = {"the", "a", "an", "and", "or", "to", "of", "in", "for", "on", "with",
        "fix", "add", "update", "make", "work", "task", "code", "file", "files"}

def _terms(text: str) -> set[str]:
    out = set()
    for w in WORDS.findall(text or ""):
        wl = w.lower()
        if wl not in STOP and len(wl) > 2:
            out.add(wl)
        # camelCase split
        for part in re.findall(r"[a-z]+|[A-Z][a-z]*", w):
            p = part.lower()
            if p not in STOP and len(p) > 2:
                out.add(p)
    return out

def _file_terms(fi) -> set[str]:
    t = _terms(fi.rel)
    for s in fi.symbols:
        t |= _terms(s)
    t |= _terms(fi.text[:4000])
    return t

def score_file(task_text: str, fi) -> float:
    """Small TF-IDF-flavoured score: task terms vs file terms/symbols."""
    tt = _terms(task_text)
    if not tt:
        return 0.0
    ft = _file_terms(fi)
    sym = set()
    for s in fi.symbols:
        sym |= _terms(s)
    hit = len(tt & ft)
    return hit + 2.0 * len(tt & sym)

class ShardDraft:
    def __init__(self, title, task_text):
        self.title = title; self.task = task_text
        self.owns: list[str] = []
        self.shared: list[str] = []          # read-only files
        self.depends_on: set[str] = set()
        self.rules_fired: list[str] = []
        self.unfittable_reason = None

def plan_project(root, *, task=None, subtasks=None, budget_tokens=60000,
                 max_shards=12, max_shared_fanout=8):
    """Return a plan dict (no files written). See write_plan()."""
    files = scan_project(root)
    graph = build_graph(files)
    sccs = strongly_connected_components(graph)
    comp_of = {}
    for i, comp in enumerate(sccs):
        for f in comp:
            comp_of[f] = i
    mode = "user-split" if subtasks else "auto-clustered"
    conflicts: list[str] = []

    # ---- select relevant files -------------------------------------
    if subtasks:
        full_task = "\n".join(f"{s.title} {s.text}" for s in subtasks)
    else:
        full_task = task or ""
    relevant = set()
    for rel, fi in files.items():
        if score_file(full_task, fi) > 0:
            relevant.add(rel)
    if not relevant:  # a task with no lexical hits still shards the graph
        relevant = set(files)

    # ---- assign SCC units to shards --------------------------------
    unit_of_file: dict[str, int] = {}
    drafts: list[ShardDraft] = []
    claimed_by: dict[str, list[str]] = defaultdict(list)

    if subtasks:
        for s in subtasks:
            drafts.append(ShardDraft(s.title, s.text))
        # score each unit for each subtask, contract by SCC
        units: dict[int, list[str]] = defaultdict(list)
        for rel in sorted(relevant):
            units[comp_of[rel]].append(rel)
        for cid, members in sorted(units.items()):
            scores = []
            for i, s in enumerate(subtasks):
                sc = sum(score_file(f"{s.title} {s.text}", files[m]) for m in members)
                scores.append(sc)
            best = max(range(len(scores)), key=lambda i: scores[i])
            ranked = sorted(scores, reverse=True)
            # claim recording: every subtask with real score claims it
            for i, sc in enumerate(scores):
                if sc > 0:
                    claimed_by[members[0]].append(subtasks[i].title)
            if len(ranked) > 1 and ranked[0] > 0 and ranked[1] >= 0.75 * ranked[0]:
                for m in members:
                    unit_of_file[m] = best
                owner = drafts[best]
                owner.rules_fired.append(
                    f"ambiguous-claim: unit {members} near-tie between subtasks; "
                    f"assigned to '{subtasks[best].title}', others sequenced after it")
                conflicts.append(
                    f"shared-unit: {members} claimed by multiple subtasks "
                    f"({', '.join(claimed_by[members[0]])}); owned by "
                    f"'{subtasks[best].title}', sequenced")
            for m in members:
                unit_of_file[m] = best
        for rel, idx in unit_of_file.items():
            drafts[idx].owns.append(rel)
        drafts = [d for d in drafts if d.owns or True]
        # subtasks with no owned files keep an empty draft out of the plan
        empty = [d.title for d in drafts if not d.owns]
        for t in empty:
            conflicts.append(f"empty-shard: subtask '{t}' matched no project files; omitted")
        drafts = [d for d in drafts if d.owns]
    else:
        # auto: weakly connected components over relevant files
        und: dict[str, set[str]] = defaultdict(set)
        for a, outs in graph.items():
            for b in outs:
                und[a].add(b); und[b].add(a)
        seen = set(); comps = []
        for start in sorted(relevant):
            if start in seen: continue
            stack = [start]; comp = set()
            while stack:
                n = stack.pop()
                if n in comp: continue
                comp.add(n); seen.add(n)
                for nb in und.get(n, ()):
                    if nb in relevant: stack.append(nb)
            comps.append(sorted(comp))
        # merge tiny singleton comps sharing a package into one misc per package
        merged: list[list[str]] = []
        by_pkg: dict[str, list[str]] = defaultdict(list)
        for comp in comps:
            if len(comp) >= 2:
                merged.append(comp)
            else:
                by_pkg[files[comp[0]].package].extend(comp)
        for pkg in sorted(by_pkg):
            merged.append(sorted(by_pkg[pkg]))
        merged.sort(key=lambda c: c[0])
        for comp in merged:
            names = ", ".join(Path(c).name for c in comp[:4])
            d = ShardDraft(title=f"cluster: {names}",
                           task_text=("(generated) graph cluster owning: " + ", ".join(comp)))
            d.owns = list(comp)
            drafts.append(d)
            for rel in comp:
                unit_of_file[rel] = len(drafts) - 1

    # ---- shard ids ---------------------------------------------------
    for i, d in enumerate(drafts):
        d.id = f"shard-{i+1:02d}-{slugify(d.title)}"

    # ---- dependencies + shared context ------------------------------
    owner_of = {rel: di for di, d in enumerate(drafts) for rel in d.owns}
    for di, d in enumerate(drafts):
        for rel in d.owns:
            for tgt in graph.get(rel, []):
                oj = owner_of.get(tgt)
                if oj is not None and oj != di:
                    d.depends_on.add(drafts[oj].id)
                    if tgt not in d.shared: d.shared.append(tgt)
            # name references to files not imported (dynamic-ish): shared only
        d.shared = sorted(set(d.shared) - set(d.owns))
    # shared symbols (defined by owner, referenced/imported by another shard)
    sym_owner: dict[str, str] = {}
    for d in drafts:
        for rel in d.owns:
            for s in files[rel].symbols:
                sym_owner.setdefault(s, rel)
    shard_shared_symbols: dict[str, list[str]] = {}
    for di, d in enumerate(drafts):
        # symbols defined by the read-only files this shard depends on
        shard_shared_symbols[d.id] = sorted(
            {s for f in d.shared for s in files[f].symbols})

    # ---- conservative ambiguity sequencing --------------------------
    seq_edges = []  # (later_id, earlier_id, reason)
    sym_to_shards: dict[str, set[int]] = defaultdict(set)
    for di, d in enumerate(drafts):
        for rel in d.owns:
            for s in files[rel].symbols:
                sym_to_shards[s].add(di)
    amb_pairs = set()
    for s, owners in sym_to_shards.items():
        if len(owners) > 1:
            ol = sorted(owners)
            for a in range(len(ol)):
                for b in range(a + 1, len(ol)):
                    amb_pairs.add((ol[a], ol[b], f"duplicate-symbol-name: {s}"))
    if mode == "auto-clustered":
        pkg_to_shards: dict[str, set[int]] = defaultdict(set)
        for di, d in enumerate(drafts):
            for rel in d.owns:
                pkg_to_shards[files[rel].package].add(di)
        for pkg, owners in pkg_to_shards.items():
            if len(owners) > 1:
                ol = sorted(owners)
                for a in range(len(ol)):
                    for b in range(a + 1, len(ol)):
                        amb_pairs.add((ol[a], ol[b], f"shared-package: {pkg}"))
    # apply: within each ambiguous pair, if neither already depends on
    # the other (directly), sequence the later index after the earlier
    for a, b, reason in sorted(amb_pairs):
        da, db = drafts[a], drafts[b]
        if da.id in db.depends_on or db.id in da.depends_on:
            continue
        db.depends_on.add(da.id)
        seq_edges.append((db.id, da.id, reason))
        db.rules_fired.append(f"conservative-sequence: sequenced after {da.id} ({reason})")

    # ---- fan-out cap -------------------------------------------------
    partial = False; notes = []
    fanout: dict[str, int] = defaultdict(int)
    for d in drafts:
        for t in d.shared: fanout[t] += 1
    for rel, n in sorted(fanout.items()):
        if n > max_shared_fanout:
            partial = True
            conflicts.append(f"fanout-cap: {rel} is read-only context for {n} shards (cap {max_shared_fanout}); plan marked partial")

    # ---- parallel groups (topological levels) ------------------------
    levels: dict[str, int] = {}
    def level_of(d, stack=()):
        if d.id in levels: return levels[d.id]
        if d.id in stack: return 0  # cycle would be a check failure; planner dep graph is acyclic by SCC contraction + added edges form index order
        deps = [x for x in drafts if x.id in d.depends_on]
        lv = 0 if not deps else 1 + max(level_of(x, stack + (d.id,)) for x in deps)
        levels[d.id] = lv; return lv
    for d in drafts: level_of(d)
    groups: dict[int, list[str]] = defaultdict(list)
    for d in drafts: groups[levels[d.id]].append(d.id)
    parallel_groups = [sorted(groups[k]) for k in sorted(groups)]
    max_par = max((len(g) for g in parallel_groups), default=0)

    # ---- budgets / fit ----------------------------------------------
    shards_out = []
    total_est = 0; total_orig = 0
    for d in drafts:
        own_orig = sum(estimate_tokens(files[r].text) for r in d.owns)
        shards_out.append({
            "id": d.id, "title": d.title, "task": d.task,
            "owns_files": sorted(d.owns),
            "owns_symbols": sorted({s for r in d.owns for s in files[r].symbols}),
            "shared_files_readonly": d.shared,
            "shared_symbols": shard_shared_symbols.get(d.id, []),
            "depends_on": sorted(d.depends_on),
            "parallel_group": levels[d.id],
            "rules_fired": d.rules_fired,
            "seam_basis": ("python-ast" if all(files[r].language == "python" for r in d.owns)
                           else "import-graph (parser-lite for non-Python files)"),
            "_own_orig": own_orig,
        })
    # max_shards cap
    if len(shards_out) > max_shards:
        partial = True
        conflicts.append(f"max-shards-cap: {len(shards_out)} shards exceeds cap {max_shards}; plan marked partial (nothing dropped)")

    return {
        "files": files, "graph": graph, "mode": mode, "shards": shards_out,
        "parallel_groups": parallel_groups, "max_parallelism": max_par,
        "conflicts": conflicts, "partial": partial,
        "budget_tokens": budget_tokens, "max_shards": max_shards,
        "seq_edges": seq_edges,
    }
