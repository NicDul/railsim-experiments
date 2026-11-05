# Bounded operations

Run commands from the repository root; discover flags with `uv run --extra solver railsim-capacity --help`. Sources of truth: [`capacity.py`](../../../../src/main/python/railsim_mcp/capacity.py), [`cli.py`](../../../../src/main/python/railsim_mcp/cli.py), [`replay.py`](../../../../src/main/python/railsim_mcp/replay.py), [resource orders](../../../../docs/resource-order-guidance.md). The recipes below are agent-selected actions, not a hard-coded adaptive loop.

## Setup and accounting wrapper

Set `ART` to a new absolute directory outside the checkout; `STATE="$ART/state"`. Preserve one `decisions.jsonl` for the overall experiment. In each record include case, exact evidence paths/status, rationale, action config, reserved/remaining allowance, actual usage and conclusion. Keep provider-call count explicit (zero for these recipes).

```sh
export ART=/tmp/railsim-capacity-acceptance-unique
export STATE="$ART/state"
mkdir -p "$ART"
uv run --extra solver railsim-capacity --help
python3 .github/skills/capacity-research/scripts/run_bounded.py --help
```

The wrapper saves command, UTC start, timeout, elapsed wall time, completed-child CPU time, exit status and log paths. It terminates descendants on timeout, including Maven children that create new sessions. Use a fresh ledger path per action. Its wall timeout is a safety bound, **not an OS CPU quota**. Workflow budgets are operation-level guards; sum their CPU counters or wrapper child CPU (not both for one action), and charge wrapper measurements for proofs/replay. Reserve allowance before launching; construction/export overhead can slightly exceed a guard. A timeout may leave incomplete evidence; keep it labelled interrupted.

## Baseline and targeted longer solve

Prepare config JSON for each declared case. This is a bounded UC1 baseline shape, not a change to the solver:

```json
{
  "use_case": "UC1", "building_block": "BB1", "mode": "MAINLINE/FV_STOP",
  "period_seconds": 1800, "volume_range": [4, 20, 1],
  "seed": 4711, "workers": 1, "solver_seconds": 3,
  "direct_gaps": [1, 0], "resource_no_overlap": true,
  "rounds": 0, "proposals": 1, "runner": "none",
  "limits": {"wall_seconds": 120, "cpu_seconds": 60,
             "simulations": 30, "model_calls": 0}
}
```

Save as `$ART/bb1-baseline.json`, then repeat with BB2/BB3 configs and distinct output/ledger names:

```sh
python3 .github/skills/capacity-research/scripts/run_bounded.py \
  --seconds 150 --ledger "$ART/bb1-baseline-execution.json" -- \
  uv run --extra solver railsim-capacity \
  --config "$ART/bb1-baseline.json" --project "$PWD" \
  --state-root "$STATE" --output "$ART/bb1-baseline"
python3 scripts/audit_capacity_export.py "$ART/bb1-baseline/proof-handoff"
```

For UNKNOWN, copy that case's config to a **new** file, remove `volume_range`, set `volumes` to the chosen unresolved count, and choose a longer `solver_seconds` (for example 15 or 45) **within remaining aggregate allowance**. Set fresh per-action `limits` and add `calibration_from` pointing at the baseline `checkpoint.json`. Keep project, state, mode, period, seed, runtime environment and inputs unchanged; the cache identity checks must pass. Run the same command with new output/ledger paths and an appropriate outer timeout. The cache transfers no hints, orders or witness. If a standalone longer solve loses useful sweep hints, record that limitation; `--replay-witness` is not a hint-import flag.

Keep the source config unchanged: normal resume will reject a changed identity and does not retry an already recorded UNKNOWN. `--export-only` repackages recorded evidence; it does not solve again.

## Validate an existing solver witness or proposed timetable

Create a witness JSON from the **exact** saved trial's `solver.pattern`, its domain and origin:

```json
{
  "building_block": "BB3", "mode": "MAINLINE/FV_STOP",
  "period_seconds": 1800,
  "source": "solver witness from checkpoint rate/trial, or agent-proposed timetable",
  "pattern": [["FV", 0, "FV_LMR_P1"]]
}
```

That one-row pattern is a schema illustration, **not** a valid MAINLINE timetable. The actual list must contain the full rounded product counts and known variants. Locate unresolved solver witnesses in `rates[].trials[]`, not only `bounds.best_witness`.

Prepare a fresh matching replay config with the intended count, `runner: none`, and hash-checked `calibration_from` from the same case/state. Then:

```sh
python3 .github/skills/capacity-research/scripts/run_bounded.py \
  --seconds 60 --ledger "$ART/candidate-validation-execution.json" -- \
  uv run --extra solver railsim-capacity \
  --config "$ART/candidate-validation.json" --project "$PWD" \
  --state-root "$STATE" --output "$ART/candidate-validation" \
  --replay-witness "$ART/candidate.json"
python3 scripts/audit_capacity_export.py "$ART/candidate-validation/proof-handoff"
```

Replay reserves its simulator call through the workflow budget and clips the process timeout to the remaining allowance. Include its measured CPU/wall time in the aggregate ledger. Inspect exported `replays[].simulation` and require `passed`: a zero CLI exit means validation completed, not that it passed. A changed calibration can add runs; trust verified cache reuse only. Record saved solver discovery and new validation as separate origins; manually proposed timetables are explicitly proposed/replayed, never discovered by the direct search. UC1 replay checks product counts; UC2/UC3 replay checks exact route counts against the configured volumes and mode. Either product names or solver route-group names may label UC2/UC3 rows; the variant selects the vehicle product.

## Agent-proposed orders via existing fixture transport

For a selected UC1 composition the agent may propose an entry order such as `FFRRG` for 2 FV, 2 RV, 1 GV. `platforms: []` leaves platform choice free. For resource proposals, use `guidance.catalogue(products, options, windows)` where `products` expands the target report's composition in its stored order, `options` groups calibrated variants by product (UC1) or variant (UC2), and `windows` is the recorded calibration. It returns exact train tokens and eligible visitors; never invent them.

Save a round response array beside the action config:

```json
[{"proposals":[{"order":"FFRRG","platforms":[]}]}]
```

Set `guidance: entry`, `runner: fixture`, `fixture: proposals.json`, `rounds: 1`, `proposals: 1`; keep the target count consistent. For resource proposals set `guidance: resources` and each proposal to `{"resource_orders":[{"resource":"verified resource","trains":["verified tokens"]}]}`. See [`guidance.py`](../../../../src/main/python/railsim_mcp/guidance.py) for exact validation and catalogue APIs. Choose a short positive direct budget, an appropriate `guided_solver_seconds`, remaining action limits and fresh output. Invoke the bounded search command as above.

The direct trial always runs first. Zero-gap full-model INFEASIBLE suppresses escalation; a direct passing witness also skips the fixture. Verify `rates[].rounds` and trial scope to tell whether the proposal was actually used. This is an **agent-authored proposal transported by the fixture runner**, not a live model-provider call or a model-quality benchmark. A guided rejection/INFEASIBLE applies only to its accepted order subset. A witness-derived order is a control, not a new independent discovery. Built-in `runner: codex` requires an explicitly selected authenticated model and allowance; this skill does not require or authorize provider usage.
