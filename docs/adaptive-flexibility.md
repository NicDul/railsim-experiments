# Adaptive entry-order sampling

`railsim-adaptive-flexibility` adds a checkpointed sampling path beside the
existing exact/fixed-sample flexibility workflow. It estimates the proportion
of distinct cyclic entry orders classified feasible by one frozen fixed-profile
solver model. A solver witness is **not** simulator validation, and a
conditional `no` remains conditional on that model's assumptions.

## Sampling and stopping contract

Each comparison point is sampled independently with replacement from its
cyclic multiset of product classes. It reuses `sample_necklace`: a uniform
linear multiset permutation is accepted with probability
`stabilizer / sequence_length` and then canonicalized by rotation. Thus
periodic sequences do not receive extra weight. Platform and timing choices
remain solver freedoms, not additional entry orders.

The default plan declares four comparison points, checkpoints
`100, 200, 400, 800, 1600, 3200, 6400`, family error `alpha = 0.05`, and an
absolute interval-width target of `0.10`. For each endpoint,
`a = alpha / (2 * M * K)`; with `M = 4` and `K = 7`, `a = 1/1120`.
The reported interval is
`[L(F, n), H(F + U, n)]`, where `F` and `U` count feasible and unresolved
**draw occurrences**, not unique cached orders. `L` and `H` are one-sided exact
Clopper-Pearson limits evaluated with a bracketed regularized-beta inversion.
Boundary values use `log`/`expm1` closed forms. Endpoints are rounded only for
display, never for a stopping decision.

All unique solver jobs for the active points in a checkpoint batch finish
before any interval at that checkpoint is computed. A point stops at its first
declared checkpoint with width at most `0.10`; there is no undeclared peeking or
data-driven error reallocation. If execution ends during a batch, only the
last complete checkpoint is valid. The run is marked
`precision_not_reached`, and incomplete jobs remain pending or visibly
`unknown`; they are not treated as negative results. Cache files and
checkpoints are atomically replaced. A resume must use the same domain,
provider identity, seed, checkpoint schedule, confidence plan and Python
version. Cached ordering results are evaluated once, while every repeated iid
draw still contributes its multiplicity to the statistics.

The simultaneous 95% guarantee is procedure coverage across the four points
and every declared checkpoint, not a posterior probability for a particular
interval. A total width of 10 percentage points is absolute precision; it
does not imply a precise relative estimate in a rare tail. Persistent unknown
outcomes can prevent the target width from being reached.

## Providers and commands

The `AdaptiveSampler` accepts an `evaluate_batch` provider. The saved-result
provider reads solver result directories in the existing
`solver-results/<volume>/<ordering-id>.json` shape. Sources are supplied from
lower to higher precedence. A sound feasible/no classification is retained
over an unresolved retry; conflicting sound classifications are rejected.
Every sampled result records its source, budget label, execution identity and
record hash. The interval widens over all unresolved draw occurrences, while
the report keeps unknown counts separated by source/budget rather than
publishing a pooled unknown rate.

Create a config JSON with the UC1 domain, four sorted volumes, a sampling seed,
and optional `checkpoints`, `alpha`, `target_width`, and `comparison_points`.
For example:

```json
{
  "use_case": "UC1",
  "building_block": "BB3",
  "mode": "MAINLINE/FV_STOP",
  "period_seconds": 1800,
  "volumes": [13, 14, 15, 16],
  "seed": 20261112,
  "checkpoints": [100, 200, 400, 800, 1600, 3200, 6400],
  "alpha": 0.05,
  "target_width": 0.10,
  "comparison_points": 4
}
```

Replay completed saved results without CP-SAT or simulator calls:

```sh
uv run --extra solver railsim-adaptive-flexibility replay \
  --project "$PWD" \
  --config /absolute/path/to/sampling.json \
  --base-results /absolute/path/to/base/solver-results \
  --overlay-results /absolute/path/to/overlay/solver-results \
  --source-audit /absolute/path/to/audit.json \
  --output /absolute/path/to/adaptive-replay
```

The audit's model identity must match every selected saved record. Census
fractions are not inputs to draw generation or stopping; an optional
`--curve-points` file is provenance only.

`fresh-pilot` recalibrates profiles from the supplied local checkout, freezes a
small random draw batch, and measures solver calls. It does not read saved
witnesses, hints, or ordering results, and it does not call the simulator after
calibration. The CLI caps the pilot at four orders per point and 900 seconds
active wall time; it does not expose a full fresh adaptive sweep.

```sh
nice -n 19 uv run --extra solver railsim-adaptive-flexibility fresh-pilot \
  --project "$PWD" \
  --state-root /absolute/path/to/pilot-state \
  --config /absolute/path/to/sampling.json \
  --output /absolute/path/to/fresh-pilot \
  --workers 2 \
  --pilot-orders-per-case 4 \
  --active-wall-limit-seconds 900
```

Each worker sets `num_search_workers = 1`. The portable solver exposes
wall-time limits, not deterministic-time limits; therefore the local pilot's
`0.546s` first attempt and `61.96s` UNKNOWN retry are wall-time alternatives,
not equivalent to a deterministic-time reference. It tries a one-second gap
first, retries UNKNOWN at the same gap, and tries zero gap only after the
one-second solve is `INFEASIBLE`. Only zero-gap `INFEASIBLE` is a conditional
no. Other incomplete or failed solves remain unknown.

Replay wall time and unique ordering counts are reported separately from fresh
per-attempt CPU/wall time and calibration cost. The bounded fresh pilot is
timing evidence only, not a confidence checkpoint. Do not infer the cost of a
complete fresh run from replay speed; any larger solver run requires separate
authorization and a measured per-volume projection.
