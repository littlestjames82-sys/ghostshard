"""GhostShard CLI. Commands: plan, check, export-bus.
Planner only: no command runs an agent, contacts a relay, or merges."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

from .planner import plan_project
from .taskfile import parse_task_file
from .bundles import write_plan, check_plan, write_bus_manifest
from . import __version__

def _load_profile(project, args):
    cfg = {"budget_tokens": 60000, "max_shards": 12, "max_shared_fanout": 8}
    for cand in [Path(project) / ".ghostshard.json"]:
        if cand.is_file():
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise SystemExit(f"invalid profile {cand}: {e}")
            sect = data.get("shard", data)
            for k in cfg:
                if k in sect:
                    cfg[k] = int(sect[k])
    if getattr(args, "budget", None): cfg["budget_tokens"] = args.budget
    if getattr(args, "max_shards", None): cfg["max_shards"] = args.max_shards
    if getattr(args, "max_shared_fanout", None): cfg["max_shared_fanout"] = args.max_shared_fanout
    return cfg

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ghostshard", description="Planner-only task sharder for GhostSlice (files out, no dispatch)")
    ap.add_argument("--version", action="version", version=f"ghostshard {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="plan shards and write bundles + shards.json + PLAN.md")
    p.add_argument("project")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--task"); g.add_argument("--task-file")
    p.add_argument("-o", "--out", default="shards")
    p.add_argument("--budget", type=int); p.add_argument("--max-shards", type=int)
    p.add_argument("--max-shared-fanout", type=int)
    p.add_argument("--json", action="store_true")
    c = sub.add_parser("check", help="validate a shards folder (bundles, ownership, acyclic)")
    c.add_argument("folder"); c.add_argument("--json", action="store_true")
    e = sub.add_parser("export-bus", help="write the GhostBus-shaped manifest FILE (sends nothing)")
    e.add_argument("folder"); e.add_argument("-o", "--out")
    args = ap.parse_args(argv)

    if args.cmd == "plan":
        if not Path(args.project).is_dir():
            print(f"error: project not a directory: {args.project}", file=sys.stderr); return 2
        cfg = _load_profile(args.project, args)
        subtasks = None
        if args.task_file:
            try:
                subtasks = parse_task_file(Path(args.task_file).read_text(encoding="utf-8"))
            except (OSError, ValueError) as ex:
                print(f"error: {ex}", file=sys.stderr); return 2
        plan = plan_project(args.project, task=args.task, subtasks=subtasks, **cfg)
        summary, code = write_plan(plan, args.project, args.out)
        if args.json:
            print(json.dumps({"shards": summary["shards"], "partial": summary["partial"],
                              "totals": summary["totals"], "max_parallelism": summary["max_parallelism"]}, indent=2))
        else:
            print(f"mode: {summary['mode']} (partial: {summary['partial']})")
            for s in summary["shards"]:
                print(f"  {s['id']}: owns {len(s['owns_files'])} file(s), est {s['estimated_tokens']} tokens, "
                      f"depends_on {s['depends_on'] or '-'}, group {s['parallel_group']}")
            print(f"max useful parallelism: {summary['max_parallelism']}")
            print(f"wrote {args.out}/shards.json, PLAN.md, bus-manifest.json + per-shard bundles")
            if summary["partial"]:
                print("plan is PARTIAL (cap or unfittable shard) - see conflicts in shards.json", file=sys.stderr)
        return code
    if args.cmd == "check":
        res = check_plan(args.folder)
        if args.json:
            print(json.dumps(res, indent=2))
        elif res["ok"]:
            print(f"check OK: {res['shards_checked']} shard bundle(s), ownership unique, acyclic")
        else:
            for e_ in res["errors"]: print(f"check FAIL: {e_}")
        return 0 if res["ok"] else 1
    if args.cmd == "export-bus":
        sj = Path(args.folder) / "shards.json"
        if not sj.is_file():
            print(f"error: shards.json not found in {args.folder}", file=sys.stderr); return 2
        plan_out = json.loads(sj.read_text(encoding="utf-8"))
        dest = Path(args.out) if args.out else Path(args.folder) / "bus-manifest.json"
        if args.out:
            import shutil; tmp = Path(args.folder)
            written = write_bus_manifest(tmp, plan_out)
            if dest != written:
                shutil.copyfile(written, dest)
            print(f"wrote bus manifest file only (nothing sent): {dest}")
        else:
            written = write_bus_manifest(Path(args.folder), plan_out)
            print(f"wrote bus manifest file only (nothing sent): {written}")
        return 0
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
