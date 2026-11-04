# Entry-order flexibility analysis

`railsim-flexibility` measures how often cyclic train-entry orders work for an
exact deterministic mode composition. It is an adjacent workflow: it reuses
the capacity workflow's calibration, solver, simulator and persistent budget
framework, but writes separate flexibility checkpoints and reports. Existing
capacity bounds and tables are not modified.

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
