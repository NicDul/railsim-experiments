# Bounded capacity frontier follow-up

For the complete declared UC1/UC2 mode-by-building-block comparison, including the later higher-budget BB3 FV_STOP result, see [capacity mode results](capacity-mode-matrix.md).

This supersedes the initial low-rate smoke checks as evidence of capacity finding. All numbers below are **new search discoveries**; historical witness replay is excluded from the five sweep reports. No global simulator maximum is established.

## Coverage and measured results

The five higher-budget frontier cases in this report are UC1 BB1/BB2 MAINLINE/FV_PASS, UC1 BB3 MAINLINE/FV_STOP, and UC2 BB1/BB2 TRANSIT/TRUNK. Other operating modes were not included in this 45-second frontier subset; their separate all-mode bounded coverage is summarized in [capacity mode results](capacity-mode-matrix.md).

Each sweep requests integer volumes 8 through 24 inclusive, period 1,800 s and seed 4711. UC1 rates are total trains/hour (16–48). UC2 volume is **per direction**, so the domain is 32–96 total trains/hour. Exclusions below are tested rounded-composition points in the outward-rounded fixed-profile surrogate; they are not an unrestricted simulator upper bound.

| Case / mode | New validated lower bound (trains/h) | Excluded tested rates, surrogate only | Undecided above discovered lower bound | CPU s / active wall s | Simulator runs |
| --- | --- | --- | --- | --- | --- |
| uc1-bb1 MAINLINE/FV_PASS | 26 | 28–48 by 2 | none in requested domain | 143.65 / 105.76 | 6 |
| uc1-bb2 MAINLINE/FV_PASS | 34 | 44, 46, 48 | 36, 38, 40, 42 | 870.87 / 815.98 | 10 |
| uc1-bb3 MAINLINE/FV_STOP | 32 | 40, 42, 44, 46, 48 | 34, 36, 38 | 900.47 / 856.77 | 9 |
| uc2-bb1 TRANSIT/TRUNK | 76 | 80, 84, 88, 92, 96 | none in requested domain | 339.14 / 261.73 | 12 |
| uc2-bb2 TRANSIT/TRUNK | 76 | 80, 84, 88, 92, 96 | none in requested domain | 401.37 / 321.58 | 12 |

BB1 reaches 26/h and the unrestricted zero-gap model excludes 28/h. BB2 reaches 34/h, with 36/38/40/42 unresolved and an occupation exclusion starting at 44/h. BB3 independently rediscovers 32/h without the historical witness, leaves 34/36/38 unresolved, and has occupation exclusions from 40/h. Both UC2 blocks reach 76/h; 80/h and the rest of the requested higher grid are excluded in the surrogate. No first UNKNOWN/failure is treated as a maximum, and no monotonicity beyond checked compositions is assumed.

The main sweeps use 45 seconds per direct solve, one worker, positive gap 1 then zero gap if needed, redundant resource no-overlap constraints and hints only from earlier witnesses discovered in that same sweep. No historical timetable/order hint was imported. Full-model zero-gap INFEASIBLE suppresses model escalation. All five primary sweeps made **zero model calls**.

## Budgets and provenance

Each primary case declares 1,000 active wall seconds, 900 measured local CPU seconds and 22 simulation runs. The single model-enabled primary case also has a one-call/120-second model cap; it never needed that call. Only hash-checked isolated calibration profiles were reused. Configs, exact invocations, actual code revisions/hashes, original input hashes, calibration source hashes, budgets, trial statuses, passing witnesses and raw portable evidence are retained. `execution.json` preserves the driver declaration and elapsed time; each report records its actual imported code revision. Direct solver code is the same across primary cases; later guided-validation fixes affect the diagnostic path.

CPU guards check between bounded operations, not as an OS scheduler quota. BB3 stopped at the CPU guard with 900.47 local CPU seconds including accounting/construction overhead; the final zero-gap trial was not run. Its resource-occupation exclusions remain independently checked arithmetic. No threshold overrun was hidden. Other cases remained below the CPU guard. Packaging/audits are outside the search guard. Search choices and timeouts can vary with machine load and model construction; these are fixed-config/seed experiments, not a bit-for-bit performance benchmark. Resource iteration was stabilised after the recorded sweeps; their exact original source revisions remain available in the Git bundle.

`provenance-audit.json` confirms that all five cache donors matched the recorded scenario hashes and runtime settings, imported only isolated profiles, and supplied no replayed witness. Future cache reuse now rejects a changed runtime as well as changed inputs/mode/seed/state. Original scenarios, historical runs and accepted work are preserved.

## UC2 hybrid transfer: what was exercised

`guidance.py` supports independent cyclic grant orders on selected shared resources. It needs no common entry or platforms. Each resource order contains precisely its visitors; variant-dependent resource membership is rejected. CP-SAT retains all other resource, timing and route constraints. Invalid independent rows are recorded and dropped; an entirely invalid proposal is rejected. A guided INFEASIBLE result excludes only that accepted proposed order subset.

The one live mechanism check used the existing ChatGPT-authenticated Codex CLI, `gpt-6-luna`, read-only/no-shell settings, no MCP setup and no new API key. It tested UC2 BB2 TRANSIT/TRUNK at 76/h as a fresh single-rate case without sweep hints. Direct trials had four seconds each and returned UNKNOWN. This shorter diagnostic direct budget was deliberately different from the 45-second frontier experiment; it is not a performance comparison. Guided solves were allowed 45 seconds. The model call took 16.13 seconds and reported 20,090 input / 572 output tokens through the existing subscription. Exactly one live call was made.

- The live response contained three resource orders. One included a train that did not visit that resource, so the original atomic validator rejected it. Raw prompt/response/metrics and failure are preserved under `uc2-resource-live/`.
- After validating independent resource rows separately, the **same saved live response** was replayed without another provider call. Two rows were accepted, the malformed row was explicitly dropped, and CP-SAT returned INFEASIBLE for that proposed subset. The case remains unresolved; it does not exclude 76/h, which the primary sweep already found feasible. See `uc2-resource-response-replay/`.
- A separately labelled positive control derives resource orders and hints from the new sweep witness. The resource-order solver returned OPTIMAL and a fresh real simulator run passed at 76/h, zero raw delay and no detours. This proves representation/solver/simulator compatibility on UC2, not model discovery or model quality. See `control-uc2-resource-order/` and `resource_order_control.py`. Its one-run/45-second-solver/120-second-simulator limits were explicit; standalone control CPU was not separately measured.

The previous structural blocker (a false common-entry order) is resolved. **Successful model-assisted discovery or a model benefit is not demonstrated**: the only live proposal failed, even though the representation passes a witness-derived positive control. Testing more proposals would be further model-efficacy evidence, not a missing proof of the implementation. There is no autonomous proof agent.

## Validation and reproduction

- Final code: 85 non-proof Python tests pass, including ten frontier/budget/cache/guidance tests; two portable-export integration tests pass.
- Eight portable exports (five sweeps, two model diagnostics, one positive control) pass independent standard-library checks of hashes, occupation arithmetic and retained raw terminal times.
- The three expensive retained proof tests passed earlier in this follow-up before the isolated guided-validation/cache changes. Their standalone proof inputs/formulations were unchanged and were not rerun again. The prior accepted 22 Java tests/build are reused; Java code and scenario resources have not changed.
- `maximum` is null in every report. Conditional surrogate exclusions, simulation witnesses, UNKNOWN, budget exhaustion, malformed proposals and one-order infeasibility remain distinct.
- No push, PR, publication, secrets change, MCP setup or historical data deletion. The accepted a51f342 ZIP/bundle are saved separately before the updated package. Sentinel review remains before any eventual publication.

```sh
uv run --extra solver railsim-capacity --config examples/capacity/uc1-bb3-frontier.json --project "$PWD" --state-root /private/tmp/frontier-state --output /private/tmp/frontier-results
uv run --extra solver railsim-capacity --config examples/capacity/uc1-bb1-frontier.json --volume-range 8 24 1 --project "$PWD" --state-root /private/tmp/frontier-state-bb1 --output /private/tmp/frontier-results-bb1
python3 scripts/summarize_capacity_sweeps.py /path/to/frontier --output /private/tmp/summary.json
python3 scripts/audit_capacity_export.py /path/to/frontier/uc2-bb2/proof-handoff
```

The included `frontier/configs/` are the exact executed configs (with explicit calibration donors); the generic repo examples calibrate from scratch. `frontier/summary.json` is the machine-readable frontier result. Each case exports `proof-handoff/prompt.md`, profiles, status/budget diagnostics, best witness and portable raw inputs/events with a SHA-256 manifest for external proof discovery. The unchanged conditional/rounding/no-holding and finite simulation assumptions apply.
