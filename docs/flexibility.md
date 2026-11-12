# Entry-order flexibility analysis

`railsim-flexibility` measures how often cyclic train-entry orders work for an
exact deterministic mode composition. It is an adjacent workflow: it reuses
the capacity workflow's calibration, solver, simulator and persistent budget
framework, but writes separate flexibility checkpoints and reports. Existing
capacity bounds and tables are not modified.

Entry-order flexibility supports UC1 and UC2 only. UC3 capacity is supported
separately; its repeated resource visits do not use this entry-order workflow.

Run a declared example from the repository root:

```sh
uv run --extra solver railsim-flexibility \
  --config examples/flexibility/uc1-mainline-fv-stop-bb1.json \
  --project "$PWD" \
  --state-root /private/tmp/railsim-flexibility-state \
  --output /private/tmp/railsim-flexibility-bb1
```

Outputs include `flexibility-report.json`, a resumable
`flexibility-checkpoint.json`, and `flexibility-results.md`; all state and
simulator outputs must remain outside the checkout. Resume with the same
configuration, source revision, runtime and output directory. A changed
identity is rejected rather than silently mixing evidence.

For a small reproducible diagnostic of completed exhaustive records, see
[local solver and simulator spot checks](spot-checks.md). That tool freezes
random UNKNOWN and saved-feasible samples, retries only the UNKNOWNs under a
larger bounded allowance, and replays original witnesses without changing the
active exhaustive run.

## Ordering semantics

For UC1, the ordering universe is the multiset of operational product classes
at the shared entry. For UC2, each product/flow/direction route is a distinct
class; route classes are grouped by their physical entry marker. A joint
ordering consists of one cyclic multiset sequence per used entry. Rotations at
each entry are canonicalized independently because relative timing between
entries is free. Platform/route alternatives are not separate orderings.
CP-SAT fixes only the configured entry sequence(s); departure times, internal
route/platform choices, overtaking and exit order remain free.

The distinct cyclic multiset denominator is computed with Burnside's lemma.
When it is within `flexibility.exact_ordering_cap`, every ordering is
enumerated and solved. Otherwise, `sample_count` independent uniform
ordering-level draws are taken with replacement. To avoid rotation-size bias,
the sampler chooses a uniform distinct linear multiset arrangement and
accepts it with probability `stabilizer / sequence_length` before canonical
rotation. The output records the full denominator, actual sample size,
distinct-ordering coverage, seed and method.

With exact ordering enumeration, every order is solved first. If the set of
solver-feasible orders is larger than `sample_count`, simulator validation
draws are uniform with replacement from that set. Exact-binomial
Clopper-Pearson intervals are reported for sampled estimates. If any sampled
outcome remains unresolved, a conservative interval widens to include it;
an incomplete planned sample has no percentage or confidence interval.

Every fixed-order candidate must pass the existing simulator gate: zero raw
delay, no detours, and every expected train present in the scored periods.
After a simulator rejection, up to `candidate_retries` distinct alternative
CP-SAT schedules are tried; earlier schedules are explicitly excluded from
later candidate solves. A solver `INFEASIBLE` on the first solve is a
conditional no under the existing fixed-profile model assumptions. `UNKNOWN`,
budget exhaustion, failed/rejected candidates and unvalidated schedules are
unresolved, never no. A finite simulator pass is evidence only for that
ordering, composition, seed and runtime.

For a predeclared multi-point sample with simultaneous confidence bounds,
saved-result replay, and a bounded local solver pilot, see
[adaptive flexibility sampling](adaptive-flexibility.md). That workflow is
additive; it does not change this fixed-sample flexibility workflow.

The capacity workflow's `limits` object is reused for calibration and
flexibility analysis. Defaults are 600 CPU seconds, 1,200 wall seconds,
120 simulator calls and zero model calls. Override the flexibility settings
to control enumeration and bounded validation:

```json
{
  "flexibility": {
    "exact_ordering_cap": 5000,
    "sample_count": 30,
    "candidate_retries": 1,
    "seed": 4711
  }
}
```

The results matrix is written before the detailed count/provenance tables and
uses the configured volume and trains/hour; UC2 volume is per direction and
its total rate includes both directions.

Hourly rates count each configured complete route departure once:
`3600 * sum(composition.values()) / period_seconds`. UC2 compositions already
include both directions.

Flexibility accepts UC1 volumes from 1 through 100 so the deterministic mix
rounding can be analyzed at small configured volumes. This does not lower the
capacity-search workflow's existing minimum of 4 trains per period. UC2 keeps
its existing minimum volume of 4.

## Exhaustive UC1/BB2 curve

The bounded sampled workflow above is not a substitute for an exhaustive curve.
The persistent-JVM upgrade of an existing exhaustive checkpoint covers the
complete MAINLINE/FV_STOP domain at volumes 1 through 13:

```sh
JAVA_HOME=/path/to/jdk-25 \
PATH="/path/to/jdk-25/bin:$PATH" \
MAVEN_OPTS="-Xmx768m -XX:TieredStopAtLevel=1 -XX:+UseSerialGC" \
uv run --extra solver python -m railsim_mcp.exhaustive \
  --project "$PWD" --output /absolute/external/exhaustive-results --workers 8 \
  --batch-classpath target/batch-classpath.txt \
  --equivalence-report /absolute/external/exhaustive-results/batch-equivalence.json
```

Build once with `./mvnw -o -q compile dependency:build-classpath
-Dmdep.outputFile=target/batch-classpath.txt -DincludeScope=runtime`, with the same
JDK/environment. The equivalence report must demonstrate identical raw events,
scores and route/entry audits for at least ten unique retained orderings,
including repaired witnesses. Migration checks that existing physics, solver
and snapshot sources are unchanged, preserves every completed record, and
imports hash-checked calibration into a separate `stages/batch-v2/` checkpoint.
It refuses to silently reuse results after other source/model changes.

Select an installed compatible JDK for your machine. The runner enumerates all
8,131 distinct cyclic orders, uses one CP-SAT thread per process, and creates
separate project copies, calibrated workspaces, simulator states and outputs for
each worker. Atomic per-order records and input/code/calibration identities
support strict resume without re-running completed orderings. The old 100/120
simulation acceptance allowance does not cover this explicitly requested
exhaustive run. Keep an external execution ledger and sufficient disk space for
raw events; the runner stops rather than proceeding below 2 GiB of free space.
Each worker keeps one sequential Java process alive over JSON-lines requests.
Every request still receives a separate immutable input snapshot, run ID and
output directory. No Maven/JVM startup is needed between requests. Creating
`STOP` in the output directory drains at most the eight active orderings and
exits without scheduling more; remove that explicit stop request before resume.

The solver-first revision records a new `stages/solver-first-v3/` identity
without changing or re-running retained completed orderings. It first classifies
every remaining ordering in parallel, saving each feasible schedule under
`solver-results/<volume>/<ordering-id>.json`. `solver-summary.json` and the
matrix-first `solver-curve.md` show the provisional model-only feasible fraction
alongside actual simulator validation counts. This is not a flexibility claim.
Only after that entire pass does it simulate the saved feasible schedules,
without solving them again. `--phase solve` or `--phase simulate` can run either
checkpointed phase separately; the default `--phase all` executes both in order.

## BB3 model-only pilot and complete curve

`python -m railsim_mcp.solver_curve` supports the explicitly solver-only BB3
MAINLINE/FV_STOP domain at volumes 1 through 16 (108,235 distinct cyclic orders).
Pass `--project`, `--output`, and `--calibration-from` pointing to an existing
BB3 capacity/flexibility calibration checkpoint. It verifies scenario/physics
hashes, raw evidence, absence of rerouting markers/events, and reconstructed
resource windows before copying isolated profiles. It runs **no simulations**.

The default 256-order, volume-16 spread pilot measures timing only, not a sampled
flexibility estimate. With eight workers, its measured projection must be below
six hours before automatic exhaustive continuation. Pilot results are retained
and skipped during the full pass. Every feasible schedule is saved for possible
later simulator validation. UNKNOWN remains unresolved; only zero-gap
INFEASIBLE is conditional no. `STOP` drains active solves, and unchanged
identities allow resuming without overwriting completed results. Model-feasible
percentages are provisional fixed-profile results, never simulator-validated
flexibility.

Only solver-feasible witnesses are simulated. Same-tick resource
release/request ordering in the simulator can delay a zero-gap witness by one
second even though the integer half-open profile intervals do not overlap.
The runner now solves with one-second resource separation first and simulates
that witness. Only if it is INFEASIBLE does it solve the original zero-gap
model. Zero-gap feasible witnesses are simulated and their fallbacks explicitly
counted; zero-gap INFEASIBLE is a conditional no. One-second INFEASIBLE alone
never excludes an ordering. UNKNOWN or simulator rejection remains unresolved.
The raw zero-delay gate is unchanged. Earlier completed zero-gap/repair records
are retained with their original per-attempt provenance.

Each simulation audits frozen input hashes, absent UC1 rerouting markers,
actual route links, detour events, expected train IDs/departures and cyclic entry
phases modulo the period. `summary.json` partitions unique order IDs into works,
conditional no, unresolved and pending, separately recording solver-feasible
orders, distinct simulated orders, candidate simulation calls and rejected
candidates. If unresolved orders remain, its bounds are deterministic
known-works/unknown-works bounds, not sampling confidence intervals.
