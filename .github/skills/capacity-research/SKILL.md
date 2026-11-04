---
name: capacity-research
description: Search Railsim capacity or investigate an unresolved frontier using bounded solver/simulator evidence, targeted timetable or order proposals, and checked fixed-profile proofs. Use for capacity experiments across declared use cases, modes or building blocks, and for interpreting UNKNOWN or budget-limited results.
---

# Capacity research

The **agent chooses the next experiment**. Existing deterministic tools execute it; there is no adaptive search daemon. A CLI run with `runner: none` is a solver-only action inside this agent-led loop, not evidence that an external model proposed candidates.

## 1. Scope and allowance

Inventory the checked-out revision's declared modes, patterns and building blocks from `src/main/resources/scenarios/`. Check support in `validate_config` and mode loaders, not just the Java catalogue. Read [workflow semantics](../../../docs/uc1-capacity-search.md) and [existing proofs](../../../proofs/README.md) before choosing experiments.

Record a case manifest, source/input hashes, volume convention, period, integer mix rounding, routing/holding assumptions and whether known results have already influenced the experiment. Keep inherited proofs/witnesses distinct from new discoveries.

Use the user's explicit limits. Otherwise recommend this **small acceptance allowance**, not an implicit authorization for open-ended research: one UC1 mode across BB1/BB2/BB3, one worker, at most **600 aggregate measured local CPU seconds, 1,200 wall seconds and 120 simulator calls**, no provider calls. If the mode is unspecified for this acceptance, use `MAINLINE/FV_STOP` and state that assumption. Allocate at most 60 CPU seconds per baseline case; reserve at least 180 CPU seconds for remaining witness validation/checkers before spending on harder solves. Count calibration, unsuccessful actions, replays and proof subprocesses. Existing per-workflow limits do not enforce this cross-action allowance: maintain a ledger and stop scheduling actions when any allowance is spent.

**Done:** explicit supported/unsupported manifest, per-action caps, aggregate allowance, reserved validation/proof budget and stopping conditions exist. Expansions beyond the allowance require a user decision.

## 2. Readiness and ETA

Read `pyproject.toml`, `pom.xml`, the selected scenario and the current CLI help. Probe Python/solver, Java and Maven before installing anything. Restore declared dependencies after a missing-dependency failure. Select a compatible installed JDK process-locally; avoid global Java changes. Run a small real smoke case if readiness is unproven.

Send an early measured wall-time ETA and local completion window based on case count, calibration cost and per-action limits. Revise it if observed solver/proof costs change it. Work sequentially by default. Keep all configs, logs and outputs outside the checkout; preserve existing results. Follow [bounded operations](references/operations.md) for executable recipes and accounting.

**Done:** smoke/readiness evidence, exact environment/revision and first ETA recorded.

## 3. Bounded baseline

Start with short direct solves and enough simulator calls to calibrate every variant and validate candidates. For the small UC1 acceptance, request counts 4–20 per 1,800 seconds initially, 3-second solves, gap 1 then 0, and a 60-CPU-second / 120-wall-second baseline per case. Wider user-requested domains remain explicit; they may need more allowance.

Audit exports and classify **each requested count** as simulator-validated, conditional profile exclusion, UNKNOWN, solver-witness-awaiting-validation, or not attempted/budget-spent. A visited row with no solver status was not solved. Preserve raw statuses beside aggregate conclusions.

**Done:** all scoped cases have a baseline report or an explicit unsupported/setup failure; remaining counts and remaining allowance are enumerated.

## 4. Adaptive decision

Read actual trials, calibration, timings and budget counters. Log `evidence -> conjecture -> chosen action/cap -> measured outcome -> conclusion/next decision` before and after each action. Choose a relevant branch; avoid repeating an identical failed trial:

| Evidence | Agent action |
| --- | --- |
| Solver witness exists but simulation quota was spent | Validate that saved candidate before spending on rediscovery. Label its solver origin and later validation separately. |
| UNKNOWN close above a validated rate | Spend a targeted longer solve at that exact composition, leaving the validation reserve intact. |
| Repeated UNKNOWN with useful route/resource structure | Propose an eligible resource/entry order through the supported fixture interface, or a timetable for explicit validation; record the agent's rationale and exact proposal. |
| Occupation bound or inherited structural argument | Match profiles and assumptions, then seek a counterexample or run an independent checker. Read [proof checking](references/proofs.md) first. |
| Simulator rejection | Inspect raw delay, missing trains, detours and reference consistency. Treat feedback as a candidate failure, not universal infeasibility. |
| Baseline already classifies the requested grid | Report that honestly; perform a scoped boundary/checker control only if it answers a real remaining question. |

Use fresh action output/configuration. Strict resume requires unchanged identity; it skips recorded trials rather than granting them more time. Calibration reuse imports isolated profiles only and must pass the workflow's identity/hash checks. Candidate replay is validation, not new search discovery, and the current replay path needs **external** runtime/accounting guards.

Proof or proposal subagents, when available, are bounded local runtime tasks with complete evidence and a stop condition. Project children request additional app-session delegation through their coordinator; do not require app-session creation tools or any particular paid model. Report unsupported helper needs instead of advertising an unimplemented operation.

**Done:** the acceptance trace contains at least one real `unresolved -> action -> verified result` transition (a checked UNKNOWN is a valid result, not a feasibility claim), with changed evidence or explicit resource exhaustion. If every baseline resolves its grid, report that rather than manufacture an unresolved case; label adaptive acceptance unexercised unless a genuine remaining conjecture is checked.

## 5. Stop, verify, report

Stop on allowance/reserve exhaustion, explicit user limit, unsupported operation, or fully classified requested domain with the stated proof scope. A difficult case must not prevent reporting the others. Audit portable exports and check the final manifest accounts for every scoped case.

**First content in every capacity-results Markdown:** comparison tables grouped by UC, **modes as rows and building blocks as columns**, total trains/hour. Use `*` for unresolved higher requested counts, `Unsupported` for missing solver support, and distinct labels for no witness or execution failure. Explain that unstarred cells classify only the requested fixed-profile grid, not unrestricted maxima; UC2 totals include both directions. Retain prior cells and their provenance when updating one experiment; distinguish budgets and inherited versus fresh evidence. Follow with exact configs/commands, per-count boundary statuses, actual CPU/wall/simulation/provider usage, decisions and uncertainty.

An infeasible proposed order excludes that subset only. A proof must cover its exact composition/domain and state the simulator-to-model necessity assumptions; prose alone is not a certificate. Separate finite witnesses, conditional surrogate exclusions, independently checked certificates and unrestricted claims. Report the cost and outcome of the agent-led orchestration separately from any in-workflow model calls.
