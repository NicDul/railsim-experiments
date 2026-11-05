# Initial capacity workflow validation, 3 November 2026

Implementation starts at `392da4d`; the original branch remains preserved. UC1 core before UC2 adaptation: `e73258d`. Main example computations began at `415f716`; reports retain their actual code/input hashes and revision. Export fixes and available-replay bounds were tested at `946276d`, including a fresh BB1 run and idempotent resume. Later documentation changes do not change the computational core.

## UC1 results

All examples use 1,800-second periods, seed 4711, one solver worker, a five-second CP-SAT limit per solve, one mock-model round per unresolved rate, and at most two proposals. Tested volumes are 4, 5 and 30 trains/period.

| Block / mode | Search witness lower bound (trains/h) | Available witness lower bound | High-rate result | Mock calls |
| --- | --- | --- | --- | --- |
| BB1 MAINLINE/FV_PASS | 10 | 10 | INFEASIBLE | 0 |
| BB2 MAINLINE/FV_PASS | 10 | 10 | UNKNOWN, UNKNOWN | 1 |
| BB3 MAINLINE/FV_STOP | 10 | 32 | UNKNOWN | 2 |

BB3 volume 4 produced a simulator rejection; volume 5 passed. This candidate-search outcome does not imply non-monotone physical capacity. A separately supplied retained BB3 FV_STOP 32/h witness passed again with raw zero delay and no detours; it is explicitly labelled replay, not search discovery. The main bounds view includes that available lower bound while retaining the search-only lower bound. Historical hybrid trials did not establish maxima.

BB1 volume 30 is excluded only within the specified outward-rounded integer fixed-profile surrogate. BB2/BB3 high-rate UNKNOWN results remain unresolved. One order exclusion never becomes an all-order exclusion. Every `maximum` is null.

## UC2 transfer results

Original UC1 mode loading rejects UC2 split flows. UC2 has three entry markers, no platforms, and no resource shared by every direction. `uc2-original-incompatibility.json` preserves that failure and topology evidence; the pre-adaptation revision/configuration is retained. The generic adaptation allocates product, flow and direction counts explicitly, then reuses the unrestricted periodic solver and raw simulator gate. Original network/configuration files and all repository scenario inputs remain unchanged. Workspace schedules are replaced and their isolated timings calibrated, as required for explicit timetable trials. Shared-entry LLM order guidance is unsupported for UC2 and disabled; an unresolved case exports evidence instead of imposing a false common order.

Each case uses volumes 4 and 6 **per direction**, i.e. 16 and 24 total trains/hour; period 1,800 s, seed 4711, one worker, three solver seconds per rate. All four declared modes were tested on both available blocks. No live model calls or paid API calls were made.

| Block | Mode | Validated total trains/h | Solver results |
| --- | --- | --- | --- |
| BB1 | METRO/BALANCED | 16, 24 | OPTIMAL, OPTIMAL |
| BB1 | METRO/TRUNK | 16, 24 | OPTIMAL, OPTIMAL |
| BB1 | TRANSIT/BALANCED | 16, 24 | OPTIMAL, OPTIMAL |
| BB1 | TRANSIT/TRUNK | 16, 24 | OPTIMAL, OPTIMAL |
| BB2 | METRO/BALANCED | 16, 24 | OPTIMAL, OPTIMAL |
| BB2 | METRO/TRUNK | 16, 24 | OPTIMAL, OPTIMAL |
| BB2 | TRANSIT/BALANCED | 16, 24 | OPTIMAL, OPTIMAL |
| BB2 | TRANSIT/TRUNK | 16, 24 | OPTIMAL, OPTIMAL |

These are bounded transfer checks, not estimates of UC2 maximum capacity. No larger UC2 rates were searched. Split-flow rounding is an explicit deterministic largest-remainder allocation, not a claim about all possible flow allocations or arbitrary railway topologies. Networks with repeated disjoint visits to one resource are rejected by the single-window profile adapter.

## Verification

- Maven `verify`: 22 tests, no failures/skips. Java simulator and scenario resources are unchanged from the starting branch.
- Python suite: 78 tests, no failures/skips. Includes mixed modes, raw arrival validation, cyclic solver positive controls, UNKNOWN/model failures, proposal rejection/deduplication, simulator feedback, budgets/resume, full-model infeasibility suppressing escalation, topology guards and proof export.
- Export integration: two tests, including corruption/path containment checks. Real CLI examples are additionally audited from portable raw evidence with `scripts/audit_capacity_export.py`.
- Final BB1 CLI run followed by identical resume retains exactly the same four run IDs; no extra calibration/simulation. All eleven UC1/UC2 proof exports pass independent occupancy/raw-witness/hash audits. UC1 exports repeated twice with no new simulations.
- No MCP installed in the validation environment; server module, entry point/dependency and old protocol tests were removed. Replaced direct-timetable policy tests with workflow-boundary tests; preserved workspace/authoring/analysis/model-adapter tests.
- BB3 enumeration/arithmetic reproduced; ten exact survivors freshly checked; retained full two-formulation 150+150 results and positive controls pass the delivery audit. Both formulations use OR-Tools, not independent solvers.
- BB2 raw profiles match all six traces. All 9,324 cached local witnesses and retained master conservation/connectivity/arithmetic were checked. A fresh bounded master replay reproduced OPTIMAL 7,240; the second master did not finish within the shared 180-second budget. The 6,916 result remains retained evidence, not a fresh solve in this run. Local minima were cached, not freshly re-proved.
- Two omitted historical BB2 build logs were recovered from their original folder after matching expected SHA-256; the full supplement checksum list now passes.

## Exact commands

```sh
uv sync --extra solver --extra llm
./mvnw --batch-mode --no-transfer-progress verify
RAILSIM_PROJECT_ROOT="$PWD" RAILSIM_STATE_ROOT=/private/tmp/railsim-capacity-tests PYTHONPATH=src/main/python:scripts uv run --extra solver --extra llm python -m unittest discover -s src/test/python
uv run python -m unittest discover -s src/test/integration
uv run --extra solver python scripts/validate_capacity_examples.py --suite uc1 --output /private/tmp/capacity-results/uc1 --state-root /private/tmp/capacity-state
uv run --extra solver python scripts/validate_capacity_examples.py --suite uc2 --output /private/tmp/capacity-results/uc2 --state-root /private/tmp/capacity-state
uv run --extra solver railsim-capacity --config examples/capacity/bb1.json --project "$PWD" --state-root /private/tmp/capacity-state/bb1 --output /private/tmp/capacity-results/uc1/bb1
uv run --extra solver railsim-capacity --config examples/capacity/bb1.json --project "$PWD" --state-root /private/tmp/capacity-state/bb1 --output /private/tmp/capacity-results/uc1/bb1 --export-only
python3 scripts/audit_capacity_export.py /private/tmp/capacity-results/uc1/bb1/proof-handoff
git diff --check 5ac2df4...HEAD
```

The supplied local delivery includes exact executed invocations, configuration/input/code hashes, logs and proof manifests under `uc1-feature/`. `--export-only` can refresh packaging after exporter changes while preserving original computational provenance; it does not resume computation against changed code. Normal resume refuses changed code/configuration/state.

## Limits and preservation

Finite seeded terminal-delay checks do not prove indefinite timetable feasibility, model necessity for every simulator trajectory, headway admissibility, or universal capacity. Headway diagnostics remain distinct from the raw zero-terminal-delay gate. No autonomous proof agent is implemented; exported prompts require a separately checked proof. No blind benchmark is claimed: prior UC1/BB2 results were known and used only as labelled regression evidence. The manuscript remains historical and conditional.

The archive inventory preserves prior branches/checkouts, linked worktrees, nine pre-existing dirty cache files and historical runs. No push, PR, publication, remote deletion, MCP setup or secret/configuration change occurred. Sentinel review remains required before eventual publication.

The subsequent bounded frontier experiments and UC2 per-resource hybrid checks are recorded in [frontier validation](frontier-validation.md). The results above are preserved initial smoke/replay checks.
