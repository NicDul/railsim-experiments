# Resource orders and bounded frontier sweeps

The capacity workflow supports local cyclic orders on individual shared resources. It does not require all trains to cross one common entry. This is useful for UC2's bidirectional branches, which do not have a global entry order.

Each rate builds a deterministic train catalogue (`t000`, `t001`, …), with the train's group and available route variants. A resource is eligible for guidance only if every variant of each visitor uses that resource and no variant of a nonvisitor uses it. The model receives the exact eligible visitor list. A proposal supplies one cyclic order per selected resource:

```json
{"resource_orders":[{"resource":"switch_1","trains":["t000","t002","t001"]}]}
```

Each selected order must contain every visitor once. Independent invalid resource rows are reported and dropped; at least one valid row must remain. No train is invented, removed or silently reassigned. Cyclic rotations and duplicate accepted restrictions are deduplicated. Every accepted restriction and every dropped row is retained. `guidance.py` hides catalogue construction, validation and solver constraints behind these operations; legacy UC1 entry-order proposals remain supported separately.

For resource `r`, CP-SAT chooses each train's platform variant and its isolated grant offset `g(i,r)`. Relative to that resource's first proposed visitor `a`, it constrains the phases `(departure_i + g(i,r) - departure_a - g(a,r)) mod period` to the proposed cyclic order. Different resources may have different orders. Nonconflicting trains retain timing/order freedom. All ordinary periodic resource-overlap constraints remain active. Restricting an order never repairs full-model infeasibility or certifies unrestricted simulator capacity; a guided INFEASIBLE result concerns that proposed subset only.

A direct trial still runs first. `direct_gaps` may request a positive safety margin followed by zero if needed; only zero-gap unrestricted INFEASIBLE can support the zero-gap surrogate exclusion. A successful simulator gate advances immediately. Earlier witnesses discovered in the same sweep can provide solver hints, never constraints. Imported calibration contains only measured profiles, not historical orders, capacity answers or witness hints.

`volume_range: [first,last,step]` in JSON or `--volume-range FIRST LAST STEP` on the CLI specifies a finite sweep domain. `solver_seconds` bounds each direct solve; optional `guided_solver_seconds` separately bounds guided solves. Distinct budgets must be disclosed in any comparison. `limits` bounds active wall seconds, measured local CPU seconds, simulation runs (including calibration) and model calls. The CPU accounting uses the Python process plus completed children; limits are checked between operations and the remaining allowance clips solver/model/simulator timeouts. This is an execution guard, not an OS scheduler CPU quota: accounting/model-construction overhead can cross the threshold slightly. Model-provider compute is not measurable local CPU. Reserved calls/runs and consumed time persist through resume; stopped work stays unresolved. Proof packaging is outside the search budget.

The representative frontier examples use 8–24 trains per 1,800 seconds, single-worker 45-second direct solves, positive-gap then zero-gap trials, a 1,000-second active wall guard, 900 local CPU seconds and 22 simulations per case. UC2 volume is per direction. Actual executed configs, pre-test revisions, code/input hashes, limits and usage are in the supplied `frontier/` evidence directory. They are bounded representative-mode experiments, not exhaustive coverage of all operating modes, flows or topologies.

```sh
uv run --extra solver railsim-capacity --config examples/capacity/uc1-bb3-frontier.json --project "$PWD" --state-root /private/tmp/frontier-state --output /private/tmp/frontier-results
python3 scripts/summarize_capacity_sweeps.py /private/tmp/sweep-cases --output /private/tmp/frontier-summary.json
```

The generic example configs calibrate when starting fresh. The recorded experiments explicitly reuse hash-checked isolated profiles to avoid repeating accepted simulations. Unchanged Java checks and retained certificates are reused; relevant Python tests are rerun. A single authenticated CLI model call is a mechanism check, not model-performance evidence. A witness-derived positive control tests representation/solver/simulator compatibility and is labelled separately from both discovery and model guidance.
