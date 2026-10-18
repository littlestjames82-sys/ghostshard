# GhostShard examples

Everything here is runnable locally, no network, nothing is
dispatched or executed by any of it. GhostShard is a planner:
its only outputs are files.

## 1. Auto-clustered plan (start here)

Uses the disjoint test fixture as the demo project (two
independent packages, `alpha/` and `beta/`):

```bash
cd ghostshard
mkdir -p /tmp/shard-demo/project
cp -r tests/fixtures/disjoint/alpha tests/fixtures/disjoint/beta /tmp/shard-demo/project/
PYTHONPATH=src python3 -m ghostshard plan /tmp/shard-demo/project \
  --task "$(cat examples/auto-task.txt)" -o /tmp/shard-demo/out
PYTHONPATH=src python3 -m ghostshard check /tmp/shard-demo/out
```

Expected shape: two shards (`shard-01-cluster-alpha-py`,
`shard-02-cluster-beta-py`), both in parallel group 0,
max useful parallelism 2, exit code 0.

`sample-output/` in this folder is a real captured run of exactly
that command (Oct 14, 2026): `shards.json`, `PLAN.md`,
`bus-manifest.json`, and both per-shard bundles with manifests.
Totals there: 354 + 329 = 683 estimated tokens. Estimates are
from the GhostSlice-style raw estimator, not exact tokenizer
counts.

## 2. User-split plan (shared symbol)

Uses the shared test fixture, where `report.py` and `export.py`
both import `normalize_records` from `shared_core.py`:

```bash
mkdir -p /tmp/shard-shared/project
cp tests/fixtures/shared/*.py /tmp/shard-shared/project/
PYTHONPATH=src python3 -m ghostshard plan /tmp/shard-shared/project \
  --task-file examples/user-split.tasks.md -o /tmp/shard-shared/out
```

Expected shape: `shared_core.py` is owned by exactly one shard,
the other shard depends on the owner and carries the shared file
as read-only signatures context, and the near-tie claim is named
in `conflicts`. Mode is labelled `user-split` in `shards.json`
and `PLAN.md`. The task file format: one `## Heading` per
subtask, free text under it; see `user-split.tasks.md`.

## 3. Profile

Copy `profile.ghostshard.json` into a project root as
`.ghostshard.json` to set the per-shard budget and caps without
flags. Precedence: CLI flags over the profile over the defaults
(60000 tokens, 12 shards, fan-out 8). The `shard` section is
optional; flat top-level keys work too.

## 4. Bus manifest (file only)

Every plan already writes `bus-manifest.json` next to
`shards.json`. `ghostshard export-bus <shards-folder> [-o file]`
re-writes it. Every job in it carries `"dispatch": false` and a
Seatbelt approval-class placeholder. GhostShard never sends it:
handing it to GhostBus is a separate, later gate.
