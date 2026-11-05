# Capacity mode results

## UC1 — Station

Each row is a mode declared in the UC1 operational plan; columns are its three station building blocks. Values are the highest simulator-validated **lower bound**, in total trains/hour. A UC1 volume is the total forward trains per 1,800-second period; its hourly rate is twice that count.

| Mode / pattern | BB1: fixed blocks | BB2: two platforms | BB3: three platforms |
| --- | ---: | ---: | ---: |
| MAINLINE / FV_PASS | 26 | 30* | 24* |
| MAINLINE / FV_STOP | 14 | 26* | **32*** |
| TRANSIT / FV_STOP | 16 | 32 | 28* |
| EXPRESS / FV_STOP | 10 | 20 | 30 |
| METRO / RV_STOP | 16 | 32 | 36* |

## UC2 — Crossing

Rates are total trains/hour **including both directions**. The search volume is per direction per 1,800-second period; the displayed rate is twice that per-direction count divided by the half-hour period.

| Mode / pattern | BB1: flat junction | BB2: grade separation |
| --- | ---: | ---: |
| TRANSIT / BALANCED | 52* | 52* |
| TRANSIT / TRUNK | 60* | 60* |
| METRO / BALANCED | 68* | 68* |
| METRO / TRUNK | 68* | 68* |

## UC3 — Reversing

The scenario catalogue declares these modes, but this revision's capacity CLI rejects UC3 (`use_case must be UC1 or UC2`). No UC3 solver formulation or capacity result is available.

| Mode / pattern | BB1: side reversing track | BB2: middle reversing track |
| --- | --- | --- |
| METRO / LOW_REVERSAL | Unsupported | Unsupported |
| METRO / HIGH_REVERSAL | Unsupported | Unsupported |

## Reading the table

All numbers are finite, seeded simulator-feasible lower bounds for the stated rounded mode, not proven maxima. `*` means one or more larger tested volumes remain unresolved by the bounded solver run. An unstarred value means every larger count in the tested 4–100-per-period grid was classified by the fixed-profile surrogate; it does **not** make surrogate exclusions an unrestricted simulator proof. CP-SAT `UNKNOWN`, budget exhaustion, and resource-occupation bounds are not equivalent to infeasibility or a simulator failure. No case in this table proves a global or unrestricted capacity maximum.

The common matrix run was performed on 2026-11-04, revision `7e958283faa2ee99ca2dcc16c09f4b10e0972f99`, using one solver worker, seed 4711, a 1,800-second period, all integer volumes 4–100, and up to 5 seconds per direct solve. Per case it allowed 120 active wall seconds, 100 measured local CPU seconds, and 18 simulator calls, with no model calls. UC1/UC2 modes were tested only in declared building-block combinations. All 23 supported cases produced at least one passing simulator witness; all 237 candidate simulations passed. Four UC3 combinations were confirmed unsupported.

One result was then extended on the same revision and scenario with a separate, higher budget: **UC1 BB3 MAINLINE/FV_STOP increased from 24 trains/hour (volume 12) to 32 trains/hour (volume 16: 7 FV, 6 RV, 3 GV)**. The extended rerun used 45-second per-solve limits, a 900-second measured-CPU cap, a 1,800-second active-wall cap, up to 200 simulator calls, zero model calls, and a 2,400-second outer process timeout. It completed in 901.957 seconds with 901.344 CPU seconds and 22 simulations (9 isolated calibrations plus 13 candidate validations); all 13 candidates passed with zero terminal delay and no detours. The CPU guard is checked between bounded operations, so its reported accounting slightly exceeded the configured cap.

In that extended case, volumes 17–25 returned `UNKNOWN` at both 45-second gap settings; volume 26 ran out of CPU during its trials. The outward-rounded fixed-profile occupation calculation exceeds the period starting at volume 20. Volumes 27–100 were not solved after the CPU cap, although the same fixed-profile occupation calculation excludes each rounded composition. Thus the extended result remains **32/hour\***, not a proven real frontier: volumes 17–19 are unresolved even in the surrogate, and 20–100 exclusions depend on the fixed-profile/no-holding assumptions. There were no simulator rejections and no CP-SAT `INFEASIBLE` proofs in the extended run.

The two `MAINLINE/FV_STOP` BB3 measurements used different configurations and budgets. The table shows the later 32/hour result, with its unresolved marker; it does not imply the original 5-second run found that witness.

## Reproduction

The exact extended-run configuration is [`examples/capacity/uc1-bb3-mainline-fv-stop-45s.json`](../examples/capacity/uc1-bb3-mainline-fv-stop-45s.json). The initial matrix used the same scenario range and seed with 5-second solves, 100 CPU seconds and 18 simulations per case.

From the repository root, select the tested Java major version for this process and run the saved case configuration. Keep outputs and state outside the checkout; use fresh paths for a fresh run because the CLI resumes matching output directories.

```sh
export JAVA_HOME=/path/to/jdk-25
export PATH="$JAVA_HOME/bin:$PATH"
uv sync --extra solver
uv run --extra solver railsim-capacity \
  --config examples/capacity/uc1-bb3-mainline-fv-stop-45s.json \
  --project "$PWD" \
  --state-root /tmp/railsim-uc1-bb3-state \
  --output /tmp/railsim-uc1-bb3-fv-stop-45s
python3 scripts/audit_capacity_export.py \
  /tmp/railsim-uc1-bb3-fv-stop-45s/proof-handoff
```

The completed run's detailed checkpoint, solver statuses, exact witness, raw simulator evidence, and 157-file portable hash manifest are retained with the experiment artifacts outside the checkout; they are not checked into the repository. The table values, source revision, effective budgets, and frontier boundary are recorded here so the repository results do not depend on private absolute paths.
