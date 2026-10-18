# GhostShard

**Plan the packets. Shard never runs the work.**

GhostShard takes one large task on one large project and produces
an ordered set of per-shard context bundles: which shard owns
which files, which shards must run after which, which can run in
parallel, and what each packet is estimated to cost. It is the
multi-agent companion to GhostSlice (one bundle for one task) and
the planning front-end for GhostBus (which owns transport and
dispatch, at a separate gate).

By Ghost Developer Studio. MIT licensed. **Zero dependencies**
(Python standard library only), **local-first**: no network, no
telemetry, and **no execution of any kind**: GhostShard does not
run agents, dispatch jobs, contact any relay, approve actions, or
merge results. Its only outputs are files.

Position in the Ghost stack: GhostSlice governs what one agent
reads for one task. GhostShard plans what many agents would each
read for one large task. Agent Seatbelt governs what agents do.
Ghost Hands is the governed browser body.

## Install

No install needed to try it from a checkout:

```bash
cd ghostshard
PYTHONPATH=src python3 -m ghostshard --help
```

Or install it (still zero dependencies):

```bash
pip install .
ghostshard --help
```

Requires Python 3.10 or newer.

## Quickstart

```bash
# auto-clustered: one freeform task, the graph does the split
ghostshard plan ~/my-project --task "Add an alpha summary report and a beta CSV export" -o /tmp/shards-out

# user-split: you pre-split the work in a task file (see below)
ghostshard plan ~/my-project --task-file tasks.md -o /tmp/shards-out

# validate a plan folder (bundles, ownership, dependency graph)
ghostshard check /tmp/shards-out

# re-write the GhostBus-shaped manifest (a file; sends nothing)
ghostshard export-bus /tmp/shards-out
```

Exit codes: `plan` exits 1 when the plan is **partial** (a cap was
hit or a shard is unfittable), 0 for a complete plan, 2 for usage
errors. `check` exits 0 when valid, 1 with every failure printed.

## The two split modes, always labelled

Every artifact (`shards.json`, `PLAN.md`) states which mode made
the split. No plan presents a heuristic split as a designed one.

- **user-split**: you write the subtasks in a task file, one
  `## Heading` per subtask with free text under it. Text before
  the first heading (other than `#` comment lines) is an error.
  GhostShard assigns project files to your subtasks by relevance
  scoring; the assignment is heuristic and `PLAN.md` says so.
- **auto-clustered**: one `--task` string. GhostShard selects the
  relevant files, clusters the import/call graph (weakly connected
  components, strongly connected components contracted into one
  unit), and generates shard descriptions from the graph. The
  shard task text in this mode is a generated graph description,
  labelled generated, never an invented prompt.

No LLM is used in either mode. Subtask text is never invented.

## Ownership, sequencing, parallelism

- A file or symbol is **owned by exactly one shard**. Other
  shards that need it get it read-only, at signatures level, in
  their bundle.
- A shard that imports a file owned by another shard
  **depends_on** that owner. Dependencies form the parallel
  groups: group 0 has no dependencies, each later group waits on
  the ones before it. The plan states the max useful parallelism
  (the largest group) rather than implying more agents always
  help.
- Conservative rule: ambiguous evidence sequences, never
  parallelises. Ambiguity in v0.1 means duplicate symbol names
  across shards, two user subtasks strongly claiming the same
  unit (a near-tie), or auto-cluster shards sharing a top-level
  package. When the rule fires it is recorded per shard in
  `rules_fired`, and collisions are named in `conflicts`.
- Cycles: an import cycle inside a project is contracted into a
  single shard, so it can never become a shard-dependency cycle.
  A dependency cycle inside a `shards.json` is a `check` failure
  with the cycle printed, never a scheduling problem.

## Fit and caps

Per-shard budget (default 60000 estimated tokens) and caps
(default max 12 shards, max shared fan-out 8) come from the
profile `<project>/.ghostshard.json`:

```json
{"shard": {"budget_tokens": 20000, "max_shards": 6, "max_shared_fanout": 4}}
```

(Flat top-level keys work too; CLI flags override the profile.)
A shard over budget is split once by owned-file halves. A single
file whose full content alone exceeds the budget cannot be split
below file granularity: it is marked `unfittable` with the reason,
the plan is marked `partial`, and `plan` exits 1. Hitting any cap
also marks the plan partial. Nothing is ever silently dropped to
make the numbers fit. Token counts are **estimates** from the
GhostSlice-style raw estimator, labelled as such in every
artifact, not exact tokenizer counts.

## Output layout

```
<out>/
  shards.json       the plan (schema below)
  PLAN.md           human view: shard table, Mermaid dependency
                    diagram, conflicts and rules fired, totals
  bus-manifest.json GhostBus-shaped jobs file (dispatch: false)
  <shard-id>/
    manifest.json   per-file path/mode/original_tokens/
                    sliced_tokens/sha256 + totals (Slice-style)
    PROMPT.md       the shard task + its bundle contents
    files/<rel>     owned files full, shared files at signatures
```

`shards.json` per shard: `id`, `title`, `task`, `owns_files`,
`owns_symbols`, `shared_files_readonly`, `shared_symbols`,
`depends_on`, `parallel_group`, `rules_fired`, `seam_basis`
(`python-ast` for Python, import-graph/parser-lite otherwise, so
the basis of every seam is stated), `bundle`, `estimated_tokens`,
`original_tokens`, `unfittable`. Plan totals equal the sum of the
per-shard bundle manifests; shared read-only files are counted
once per shard that carries them, and the totals block says so.

`ghostshard check` validates: every bundle manifest against its
files (sha256 and token totals), unique shard ids, ownership
uniqueness, every `depends_on` target exists, the dependency
graph is acyclic, and `shards.json` estimates match the bundle
manifests.

## The GhostBus boundary

`bus-manifest.json` is a **file only**: one job per shard with
`bundle_path`, `task`, `depends_on`, an
`approval_class: placeholder-seatbelt-review`, and
`"dispatch": false`. GhostShard never sends it, never contacts
the GhostBus relay (hosted or local), and never will in this
line: execution belongs to GhostBus under Agent Seatbelt, and is
a separate decision after the plan format is proven by hand.

## Honest limits

- Graph quality bounds plan quality. Python gets full `ast`
  edges; other languages are import-level (parser-lite) seams,
  stated per shard via `seam_basis`.
- Relevance scoring is lexical (TF-IDF-flavoured). A subtask that
  names nothing in the project may match no files; that is
  reported as an empty-shard conflict, not hidden.
- Lone files that share a top-level package are merged into one
  miscellaneous shard per package in auto mode. Two unrelated
  modules in one folder will be planned as one shard.
- A plan being well-formed does not mean the split is good.
  `check` proves structure (ownership, acyclicity, manifest
  integrity), not judgement. Read `PLAN.md` before handing
  bundles to anyone.

## Tests

```bash
PYTHONPATH=. python3 -m pytest tests -q
# or
python3 -m unittest discover -s tests -t .
```

The suite drives the real CLI as a subprocess against fixtures in
`tests/fixtures/`: disjoint subgraphs that must parallelise, a
shared symbol that must sequence or name a conflict, a
hand-built dependency cycle that must fail `check`, an
import-cycle project that must be contracted into one shard, and
a single-file project that must report itself unfittable at a
tiny budget. See `examples/` for runnable walkthroughs and a
captured sample plan.

## Layout

```
src/ghostshard/
  cli.py       plan / check / export-bus commands + profile load
  planner.py   graph clustering, ownership, ambiguity sequencing,
               parallel groups, caps
  project.py   project scan, Python ast + parser-lite imports,
               SCC + cycle detection
  taskfile.py  user-split task file parser (## headings)
  bundles.py   per-shard bundles, shards.json / PLAN.md /
               bus-manifest writers, plan check
  slicing.py   signatures slicing for read-only shared files
  tokens.py    token estimator (labelled estimates)
tests/         Agent B suite (CLI-level, fixture-driven)
examples/      runnable walkthroughs + a captured sample plan
```
