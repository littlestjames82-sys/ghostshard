"""Bundle generation, shards.json + PLAN.md writers, plan check, and
the GhostBus-shaped manifest export (a FILE only: nothing is sent,
no relay is contacted, dispatch is always recorded as false).
"""
from __future__ import annotations
import hashlib, json
from pathlib import Path

from .project import find_cycle
from .slicing import signatures_of
from .tokens import estimate_tokens, ESTIMATOR_NOTE
from . import __version__

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def split_if_needed(shard: dict, files, budget: int):
    """Fit step: if owned full content exceeds budget, try splitting
    the owned set into two halves by file (graph reason recorded).
    Returns (list_of_bundle_specs, unfittable_reason_or_None).
    A single file whose full content alone exceeds budget cannot be
    split further at file granularity: unfittable.
    Each spec: dict(files=[(rel, mode)], est_tokens, orig_tokens)
    """
    owns = shard["owns_files"]
    def spec_for(rels, shared):
        entries = []
        for r in rels:
            entries.append((r, "full", files[r].text))
        for r in shared:
            entries.append((r, "signatures", signatures_of(files[r].text, files[r].language)))
        est = sum(estimate_tokens(t) for _, _, t in entries)
        orig = sum(estimate_tokens(files[r].text) for r, _, _ in entries)
        return {"entries": entries, "est": est, "orig": orig}
    whole = spec_for(owns, shard["shared_files_readonly"])
    if whole["est"] <= budget:
        return [whole], None
    big = [r for r in owns if estimate_tokens(files[r].text) > budget]
    if big:
        return [whole], (f"single file(s) {big} exceed per-shard budget "
                         f"{budget} estimated tokens at full mode; cannot split below file granularity")
    if len(owns) < 2:
        return [whole], "owned set exceeds budget and cannot be split further"
    half = len(owns) // 2
    subs = [spec_for(sorted(owns)[:half], shard["shared_files_readonly"]),
            spec_for(sorted(owns)[half:], shard["shared_files_readonly"])]
    if all(s["est"] <= budget for s in subs):
        shard.setdefault("rules_fired", []).append(
            "fit-split: shard exceeded budget; split by owned-file halves")
        return subs, None
    return [whole], "owned set exceeds budget even after one fit-split"

def write_bundle(bundle_dir: Path, shard: dict, spec, bundle_task: str):
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "files").mkdir(exist_ok=True)
    rows = []
    prompt_parts = [f"# Shard: {shard['title']}\n\n{bundle_task}\n"]
    for rel, mode, content in spec["entries"]:
        dest = bundle_dir / "files" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        orig = spec  # placeholder replaced below
        rows.append({"path": rel, "mode": mode,
                     "original_tokens": None, "sliced_tokens": estimate_tokens(content),
                     "sha256": _sha(content)})
        prompt_parts.append(f"\n## {rel} ({mode})\n\n```\n{content}\n```\n")
    return rows, "".join(prompt_parts)

def write_plan(plan: dict, project_root, out_dir):
    """Write shards/ output: per-shard bundles, shards.json, PLAN.md,
    bus-manifest.json. Returns (plan_summary_dict, exit_code).
    exit 1 if partial/unfittable (Slice --fit style discipline)."""
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    files = plan["files"]; budget = plan["budget_tokens"]
    final_shards = []
    any_unfit = False
    for shard in plan["shards"]:
        specs, unfit = split_if_needed(shard, files, budget)
        if unfit:
            any_unfit = True
            plan["partial"] = True
            plan["conflicts"].append(f"unfittable: {shard['id']}: {unfit}")
        for k, spec in enumerate(specs):
            sid = shard["id"] if len(specs) == 1 else f"{shard['id']}-part{k+1}"
            bdir = out / sid
            rows, prompt = write_bundle(bdir, shard, spec, shard["task"])
            for row in rows:
                row["original_tokens"] = estimate_tokens(files[row["path"]].text)
            manifest = {
                "tool": "ghostshard-bundle", "ghostshard_version": __version__,
                "shard_id": sid, "files": rows,
                "total_original_tokens": sum(r["original_tokens"] for r in rows),
                "total_sliced_tokens": sum(r["sliced_tokens"] for r in rows),
                "estimator": ESTIMATOR_NOTE,
            }
            (bdir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            (bdir / "PROMPT.md").write_text(prompt, encoding="utf-8")
            fs = dict(shard)
            fs["id"] = sid
            fs["bundle"] = sid
            fs["estimated_tokens"] = manifest["total_sliced_tokens"]
            fs["original_tokens"] = manifest["total_original_tokens"]
            fs["unfittable"] = bool(unfit) if len(specs) == 1 else False
            fs.pop("_own_orig", None)
            if len(specs) > 1:
                fs["owns_files"] = sorted(r["path"] for r in rows if r["mode"] == "full")
                fs["shared_files_readonly"] = sorted(r["path"] for r in rows if r["mode"] != "full")
            final_shards.append(fs)
    plan_out = {
        "tool": "ghostshard", "version": __version__,
        "mode": plan["mode"],
        "mode_label": ("user-split (heuristic assignment of user subtasks to files)"
                       if plan["mode"] == "user-split"
                       else "auto-clustered (heuristic graph clustering, not a designed split)"),
        "project": str(project_root),
        "partial": plan["partial"],
        "budget_tokens_per_shard": budget,
        "estimator": ESTIMATOR_NOTE,
        "parallel_groups": plan["parallel_groups"],
        "max_parallelism": plan["max_parallelism"],
        "conflicts": plan["conflicts"],
        "shards": final_shards,
        "totals": {
            "estimated_tokens": sum(s["estimated_tokens"] for s in final_shards),
            "original_tokens": sum(s["original_tokens"] for s in final_shards),
            "note": "totals are sums over per-shard bundle manifests; shared read-only files are counted once per shard that carries them",
        },
    }
    (out / "shards.json").write_text(json.dumps(plan_out, indent=2), encoding="utf-8")
    (out / "PLAN.md").write_text(render_plan_md(plan_out), encoding="utf-8")
    write_bus_manifest(out, plan_out)
    return plan_out, (1 if plan_out["partial"] else 0)

def render_plan_md(p: dict) -> str:
    lines = ["# GhostShard Plan", "",
             f"Mode: **{p['mode']}** - {p['mode_label']}",
             f"Partial: **{p['partial']}**",
             f"Max useful parallelism: **{p['max_parallelism']}** (largest parallel group)",
             f"Per-shard budget: {p['budget_tokens_per_shard']} estimated tokens",
             f"_{p['estimator']}_", "",
             "Planner only: this plan dispatches nothing and runs no agents.", "",
             "## Shards", "",
             "| Shard | Owns | Depends on | Est. tokens | Parallel group |",
             "|---|---|---|---|---|"]
    for s in p["shards"]:
        lines.append(f"| {s['id']} | {', '.join(s['owns_files']) or '-'} | "
                     f"{', '.join(s['depends_on']) or '-'} | {s['estimated_tokens']} | {s['parallel_group']} |")
    lines += ["", "## Dependency diagram", "", "```mermaid", "graph TD"]
    if not any(s["depends_on"] for s in p["shards"]):
        for s in p["shards"]:
            lines.append(f'    {s["id"].replace("-", "_")}["{s["id"]}"]')
    for s in p["shards"]:
        for d in s["depends_on"]:
            lines.append(f'    {d.replace("-", "_")} --> {s["id"].replace("-", "_")}')
    lines += ["```", "", "## Conflicts and rules fired", ""]
    items = list(p["conflicts"])
    for s in p["shards"]:
        items += [f"{s['id']}: {r}" for r in s.get("rules_fired", [])]
    lines += [f"- {i}" for i in items] or ["- none"]
    lines += ["", f"Total estimated tokens (sum of shard bundles): {p['totals']['estimated_tokens']}",
              f"Total original tokens: {p['totals']['original_tokens']}", ""]
    return "\n".join(lines)

def write_bus_manifest(out_dir, plan_out: dict) -> Path:
    """GhostBus-shaped job manifest. A FILE ONLY. Nothing is sent."""
    jobs = [{
        "job_id": s["id"], "bundle_path": s["bundle"],
        "task": s["task"], "depends_on": s["depends_on"],
        "approval_class": "placeholder-seatbelt-review",
        "dispatch": False,
    } for s in plan_out["shards"]]
    doc = {"kind": "ghostbus-job-manifest", "ghostshard_version": __version__,
           "note": "File only. GhostShard never sends this; hand it to GhostBus at a separate gate.",
           "jobs": jobs}
    path = Path(out_dir) / "bus-manifest.json"
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path

# ---------------------------------------------------------------- check
def check_plan(folder) -> dict:
    folder = Path(folder)
    errors: list[str] = []
    sj = folder / "shards.json"
    if not sj.is_file():
        return {"ok": False, "errors": ["shards.json not found"]}
    try:
        plan = json.loads(sj.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"ok": False, "errors": [f"shards.json is not valid JSON: {e}"]}
    shards = plan.get("shards", [])
    ids = [s["id"] for s in shards]
    if len(ids) != len(set(ids)):
        errors.append("duplicate shard ids")
    owners: dict[str, str] = {}
    for s in shards:
        for f in s.get("owns_files", []):
            if f in owners:
                errors.append(f"ownership violation: {f} owned by both {owners[f]} and {s['id']}")
            owners[f] = s["id"]
        for dep in s.get("depends_on", []):
            if dep not in ids:
                errors.append(f"{s['id']} depends on unknown shard {dep}")
    graph = {s["id"]: sorted(s.get("depends_on", [])) for s in shards}
    cyc = find_cycle(graph)
    if cyc:
        errors.append("dependency cycle detected: " + " -> ".join(cyc))
    for s in shards:
        bdir = folder / s.get("bundle", s["id"])
        mf = bdir / "manifest.json"
        if not mf.is_file():
            errors.append(f"{s['id']}: manifest.json missing"); continue
        man = json.loads(mf.read_text(encoding="utf-8"))
        tot_s = tot_o = 0
        for row in man.get("files", []):
            fpath = bdir / "files" / row["path"]
            if not fpath.is_file():
                errors.append(f"{s['id']}: bundle file missing {row['path']}"); continue
            data = fpath.read_text(encoding="utf-8")
            if _sha(data) != row["sha256"]:
                errors.append(f"{s['id']}: sha256 mismatch {row['path']}")
            if estimate_tokens(data) != row["sliced_tokens"]:
                errors.append(f"{s['id']}: sliced token mismatch {row['path']}")
            tot_s += row["sliced_tokens"]; tot_o += row["original_tokens"]
        if tot_s != man.get("total_sliced_tokens"):
            errors.append(f"{s['id']}: manifest sliced total mismatch")
        if tot_o != man.get("total_original_tokens"):
            errors.append(f"{s['id']}: manifest original total mismatch")
        if s.get("estimated_tokens") not in (None, man.get("total_sliced_tokens")):
            errors.append(f"{s['id']}: shards.json estimated tokens do not match bundle manifest")
    return {"ok": not errors, "errors": errors,
            "shards_checked": len(shards),
            "cycle": cyc}
