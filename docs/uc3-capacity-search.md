# UC3 reversing-track capacity search

The CLI and `capacity.Workflow` support all four declared UC3 cases:
`METRO/LOW_REVERSAL` and `METRO/HIGH_REVERSAL`, on BB1 (side reversing track)
and BB2 (middle reversing track). UC1 and UC2 retain their existing volume
domains and allocation conventions.

## Trips, rounding and units

UC3 uses the operational plan's native domain **1-6 per-direction allocations
per 1,800 seconds**. The search makes deterministic largest-remainder allocations
to products, then flows, with lexical flow ties. This selects one precise integer
composition; the native Java sampling experiment instead uses a stochastic
remainder lottery. Neither convention makes fractional shares exactly realizable
at every small volume. The compositions used by search are:

| Volume | LOW: LMR / RML / LML | HIGH: LMR / RML / LML |
|---|---|---|
| 1 | 1 / 1 / 0 | 1 / 1 / 0 |
| 2 | 2 / 2 / 0 | 1 / 1 / 2 |
| 3 | 2 / 2 / 2 | 2 / 2 / 2 |
| 4 | 3 / 3 / 2 | 2 / 2 / 4 |
| 5 | 4 / 4 / 2 | 3 / 3 / 4 |
| 6 | 5 / 5 / 2 | 4 / 4 / 4 |

`RV_LMR` and `RV_RML` are distinct through routes. Both LML direction mappings
name **the same complete `RV_LML` route**. As in `StatefulScheduleSampler`,
that mapping receives twice its allocated count, labelled `one-way`, not an
invented R-M-R route. Each L-M-L trip uses one vehicle throughout, stops at
`M_REVERSING` with its original **480-second minimum dwell**, and returns to L.
The continuous reversing-platform occupation and both visits to shared switches
are measured from raw events. Through RV stops retain their 102-second minimum
dwell. Original routes, networks, rerouting markers and circulation configuration
are preserved; calibration freezes measured stop offsets without removing dwell.

Distinct route departures use distinct vehicles, including across periods,
matching the native sampler. There is no additional inter-trip fleet-cycle
constraint: the scenario supplies none. LML's outbound and return sections are
never split into independently timed trains.

Reported **total trains/hour** means complete route departures/hour:
`3600 * (2 * volume) / period_seconds`, hence 4-24/hour on the native period.
This is not a count of section crossings, station calls, distinct available
vehicles, or a fleet-constrained operating capacity.

## Run and inspect

Use Python with the solver extra and a compatible Java 25 process environment.
Keep state, run outputs and logs outside the checkout.

```sh
export ART=/absolute/external/uc3-acceptance
mkdir -p "$ART"
uv run --extra solver railsim-capacity \
  --config examples/capacity/uc3-bb1-low-reversal.json \
  --project "$PWD" --state-root "$ART/state" --output "$ART/bb1-low"
python3 scripts/audit_capacity_export.py "$ART/bb1-low/proof-handoff"
```

For the other three cases copy the example config outside the checkout and
change `building_block` and/or `mode`. It is a one-worker, no-provider,
3-second-per-solve native-domain sweep with explicit wall/CPU/simulator quotas.
Calibration costs three isolated runs per case; a passing candidate needs one
additional simulator call. Allow sufficient wall time for JVM/Maven launches;
the included limits can leave unresolved counts on a slower machine.

For a targeted follow-up set `volumes: [N]` instead of `volume_range`, use a fresh
output directory, and optionally set `calibration_from` to the matching baseline
`checkpoint.json`. Input, mode, period, seed, runtime, state and retained-evidence
hash checks protect profile reuse. A saved solver witness can be validated with
`--replay-witness`: include `use_case`, `building_block`, `mode`,
`period_seconds`, `source`, and full `pattern` rows `[product, second, variant]`.
Solver route-group labels are also accepted in place of product labels.
Replay checks the exact mode/route composition at a configured volume, charges
its simulator budget, and remains separate from search discovery.

## Model, validation and evidence

Resource profiles retain the existing `[claim, grant, release]` format for
single visits and use a list of these triples for disjoint visits. The periodic
solver constrains every conflicting pair, including a train's return visit
against its own next-period copy. It does not merge visits into an overrestrictive
envelope. Occupation arithmetic sums the actual visit durations.

Resource-order guidance supports single-visit resources. Revisited resources
are excluded from the proposal catalogue because a train-token order cannot
describe multiple visits; they remain fully constrained in every solve.
Common-entry/platform-choice guidance is not appropriate for UC3.

Only periods 2 and 3 of six repeats are scored. A candidate must complete every
expected route with exactly zero raw positive terminal delay and no detours.
The portable export contains frozen scenario inputs, raw events, calibration,
profiles, route profiles, compositions, statuses, budget counters and hashes.
The standard-library audit independently checks UC3 route counts, reported
units, occupation arithmetic, raw terminal arrivals, route identity, no detours,
and at least 480 seconds of dwell on the same reversing vehicle.

`conditional_profile_excluded` concerns the outward-rounded fixed-profile
surrogate only. UNKNOWN, simulation rejection, process failure and spent budgets
remain distinct unresolved evidence. A simulator-passing finite witness is a
lower bound, not an unrestricted maximum or indefinite-feasibility proof.
The existing UC1 certificates do not establish UC3 limits.
