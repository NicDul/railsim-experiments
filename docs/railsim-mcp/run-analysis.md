# Retained run analysis MCP tools

The original five read-only tools below inspect completed Railsim run records, their hash-checked input snapshots, and their retained event streams. They never create or rerun a simulation. Run IDs are UUIDs; callers cannot provide filesystem paths. Reports are deterministic for a retained run and event order. List bounds are 1–200; defaults are 100.

## Tools

| Tool | Arguments | Purpose |
| --- | --- | --- |
| `inspect_train_timeline` | `run_id: string`, `train_id?: string`, `limit?: integer` | Per train: scheduled and actual departure; each route stop's scheduled/actual arrival and departure, dwell, simulator delay estimate, and event indices; terminal completion and delay. Missing event values are `null`. |
| `locate_first_delay` | `run_id: string`, `reference_run_id: string`, `train_id: string` | Requires a completed, hash-valid, one-train reference run on the same route. Reports actual link entry/exit, reference-relative time, measured baseline deviation, schedule departure delay, simulator-reported estimates, and source event indices. The reference ID and input hash are returned. |
| `inspect_run_conflicts` | `run_id: string`, `train_id?: string`, `resource_id?: string`, `limit?: integer` | Filters recorded headway pairs. Includes resource, pair, reservation/head/tail/release evidence where available, overlap interval (or `null`), measured headways, requirement, deficit, and event indices. Candidate blocker inference is separate from proven dispatcher cause; `dispatcher_cause_proven` remains false. |
| `assess_run_feasibility` | `run_id: string`, `period_seconds?: integer` | Checks every expected train, positive terminal/intermediate delay, and headway admissibility separately; includes counts by scheduled period and warm-up, drain, and wrap-around evidence/limitations. |
| `compare_simulation_runs` | `before_run_id: string`, `after_run_id: string`, `limit?: integer` | Revalidates each frozen input, returns file and aggregate hashes, changed inputs, changed departures/order, metrics and train-level terminal delay regressions. Event stream paths are relative to the configured run store. |

These analysis calls reject incomplete runs and invalid frozen input hashes. Event-reading calls also reject missing/corrupt event files; comparison reads aggregates and snapshots only. Event streams have a 128 MiB compressed-file and 512 MiB decompressed-stream read bound. Analysis only reads files under the configured run root.

## Evidence example

For the retained initial delayed run `5df51b9d-5bf3-4434-8a59-6059b735fcdd`, `inspect_train_timeline` identifies `FV_00_p1` as scheduled at 1,800 s with a measured terminal arrival at 2,431 s and 72 s terminal delay. `locate_first_delay` against the isolated FV reference `14e326d2-2f01-40fa-8bcb-57a5fcace88f` records its first positive link-profile deviation at the exit from `f01` (2,092 s actual; +37 s from the reference-relative profile). `inspect_run_conflicts` also reports a candidate pair on `f03`: `GV_09_p0` then `FV_00_p1`, with 72 s head-to-head and 56 s tail-to-head against 180 s required, a measured maximum deficit of 124 s. These are separate measured observations; the reports do not claim that the `f03` pair was the proven dispatcher cause of the `f01` deviation.

The schedule correction is reflected by the changed `schedule.xml` hash between that run and `18c65cdf-ef6b-4d24-a839-d1123448a4d7`; comparison identifies the timetable differences across runs. The retained corrected run completes all 30 trains, has mean and maximum terminal delay of 0 s, and records zero headway conflicts. `assess_run_feasibility(period_seconds=1800)` reports 10 trains in each of periods 0, 1, and 2, with delay feasibility and headway admissibility both true. The frozen run inputs and event references are returned with each result.

The isolated reference for a link comparison must be supplied explicitly. Baseline deviations are calculated after subtracting each run's scheduled departure, so schedule delay remains its own field. Simulator `delay` event attributes remain labeled as internal estimates. Headway admissibility follows retained headway analysis; it is not used as sole causal proof for any train's delay.

The [complete MCP reference](reference.md) documents all 22 tools, including paginated `list_delayed_trains` and `inspect_train_movements`, file provenance, and limitations. Historical run IDs in the example above refer to the original development workspace; outputs are regenerated locally and excluded from Git. The integration test creates synthetic fixtures rather than requiring those runs.

First-delay comparison additionally requires identical frozen network/config
inputs and matching route profiles and vehicle types, so changes to infrastructure
or train characteristics cannot silently masquerade as congestion delay.
