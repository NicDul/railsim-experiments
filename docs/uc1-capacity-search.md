# Solver-first UC1 capacity workflow

Install Python 3.11+, Java/Maven (the wrapper is included), `uv`, and the declared solver extra:

```sh
uv sync --extra solver
./mvnw --batch-mode --no-transfer-progress verify
uv run --extra solver railsim-capacity --config examples/capacity/bb1.json --project "$PWD" --state-root /private/tmp/capacity-state --output /private/tmp/capacity-results
```

The command resumes the same output directory. Use a new directory if configuration, source inputs, Python/Java code, runtime settings or state roots change. State and output directories must be outside the checkout. A lock prevents simultaneous CLI writers. The historical Python import namespace `railsim_mcp` remains for helper compatibility; there is no MCP dependency, server, protocol or server-global workspace in this workflow.

The reusable core is `capacity.Workflow`; `cli.py` handles paths, locking and arguments. `simulation`, `profiles`, `solver`, `validation`, `timetable` and `runtime` contain ordinary library operations. Historical proof scripts retain their distinct formulations/checkers. The prior branch preserves removed protocol adapters and direct-timetable policies.

At each requested volume, direct CP-SAT chooses departure seconds and platform variants with all orders free. A successful raw simulator gate advances without a model call. UNKNOWN or a simulator rejection can trigger bounded order proposals. Full-model INFEASIBLE suppresses order escalation: a subset of that model cannot restore feasibility. Infeasibility of an order excludes only that order. Simulator feedback is recorded in the model context; it is not silently promoted to a universal constraint.

Configuration contains `building_block`, `mode`, `period_seconds`, `volumes` (trains per period), `seed`, `workers`, `solver_seconds`, `rounds`, `proposals`, `model_seconds` and `runner`. The default runner is `none`. `fixture` loads recorded mock proposals; it exercises plumbing and is not a model performance benchmark. `codex` requires an explicit `model` and uses the existing authenticated CLI. No API fallback is selected by this workflow. No live model benchmarks are part of local validation.

Each rate spends at most one unrestricted solve plus `rounds * proposals` ordered solves, with one simulation per solver witness. A simulation has a 300-second process limit; calibration uses one isolated simulation per route/platform variant. Each model round is bounded by `model_seconds`. Checkpoints reserve calls/trials before execution; an interruption spends that budget and is recorded rather than silently retried. Rejected/duplicate proposals consume proposal slots but no solver/simulation. Completed rates are not rerun by resume.

`bounds-report.json` records effective config, hashes, invocation, calibration, raw solver statuses, patterns, simulator results, model attempts and budgets. `bounds` separates finite simulator witnesses from fixed-profile exclusions and lists unresolved volumes. `maximum` remains null: neither a failed rate nor one excluded rounded composition covers all larger admissible timetables. Resource-occupation exclusions have an independent arithmetic calculation; CP-SAT INFEASIBLE applies to the specified integer surrogate. Profiles round measurements outward, so model infeasibility alone does not establish simulator infeasibility. Only periods 2 and 3 of six repeated periods are scored; finite simulation does not prove indefinite feasibility. Raw terminal delay must be zero and no detours are accepted.

`proof-handoff/` contains `prompt.md`, report, integer resource profiles, frozen simulator inputs, raw compressed events and a SHA-256 manifest. Pass the directory to an external LLM for a proposed proof/checker. Prose is not a certificate. Original absolute paths remain provenance; portable adjacent copies are the evidence. Proof checking remains separate. Explicit witness regression uses `--replay-witness FILE` with a JSON object containing `building_block`, `mode`, `period_seconds`, `pattern` and optional `source`; it is labelled replay, not search discovery.

The article remains a historical research draft with conditional claims. Its retained hybrid trials did not establish maxima. See the delivered validation report for exact new demonstration results and limitations.

UC2 transfer testing uses `use_case: UC2` and the declared product/flow/direction allocation. Volume is per direction; total rate is reported separately. Original network/configuration stay unchanged. There is no platform selection or shared-entry order guidance for UC2. Generic per-resource order guidance is available for eligible shared resources. See [measured validation and limits](capacity-validation.md).

Use `--export-only` to repackage a recorded checkpoint after exporter fixes without rerunning simulation; computational provenance is preserved. Normal resume remains strict about code/configuration/state hashes. Bounds report both the search-only lower bound and any larger explicitly replayed witness, with its origin.

For finite frontier sweeps, resource-order guidance, measured budgets and their limits, see [resource-order guidance](resource-order-guidance.md). The earlier smoke-test validation remains historical; frontier results are reported separately.
