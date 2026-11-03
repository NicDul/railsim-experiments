# Railsim MCP reference

The local stdio MCP edits isolated scenarios, executes one explicit timetable at a time, and analyses retained simulations without requiring the calling agent to open output files. It exposes **22 tools**. Seven dedicated analysis tools complement the original aggregate inspection tool.

## Setup and storage

Install this package using `.venv/bin/pip install .`, then launch `.venv/bin/railsim-mcp` with `RAILSIM_PROJECT_ROOT` pointing to a compatible simulator checkout and `RAILSIM_STATE_ROOT` pointing to an external data directory. No source-path injection is required. Simulation additionally needs the simulator's Java/Maven toolchain and populated offline Maven cache (`RAILSIM_MAVEN_REPO` optionally selects it). Analysis tools never invoke Maven or rerun a simulation, although server startup currently checks that a simulator checkout is configured.

Paths below are repository-relative:

| Code/data | Role |
| --- | --- |
| `src/main/python/railsim_mcp/server.py` | MCP tool registration and typed arguments. |
| `src/main/python/railsim_mcp/workspace.py` | Workspace confinement, XML/JSON validation, entity edits, run orchestration and aggregate inspection. |
| `src/main/python/railsim_mcp/analysis.py` | Read-only reports and events XML parsing. |
| `$RAILSIM_PROJECT_ROOT/src/main/resources/scenarios/use_case_N/building_block_N/` | External simulator templates; copied before editing. The operational plan comes from the use-case directory. |
| `$RAILSIM_STATE_ROOT/workspaces/<workspace_id>/` | Writable `network.xml`, `schedule.xml`, `vehicles.xml`, `config.xml` and `ucN_operational_plan.json`. |
| `$RAILSIM_STATE_ROOT/runs/<run_id>/inputs/` | Frozen copies of those five inputs. Per-file and aggregate SHA-256 hashes are recorded. |
| `$RAILSIM_STATE_ROOT/runs/<run_id>/run.json` | Persisted `output/simulation.json` results plus snapshot hashes, paths and elapsed time. Contains all expected train records, completion, terminal delays, aggregate metrics and measured headway conflicts. |
| `$RAILSIM_STATE_ROOT/runs/<run_id>/output/ITERS/it.0/<run_id>.0.events.xml.gz` | Raw XML evidence: driver/vehicle mapping, link entry/exit, tail clearance, link reservation/release, facility arrival/departure and simulator train-state estimates. |

The single-run Java entry point (`RunSingleSimulation.java`) builds `simulation.json` from the simulation's schedule and event-based analysis. The MCP uses this aggregate via `run.json`, and separately parses the raw events XML for detailed evidence. The retained run also contains `railsimLinkStates.csv.gz`, `railsimTimeDistance.csv.gz` and `railsimTrainStates.csv.gz`; these are not additional input dependencies of the MCP. There is no separate `trainmovements.xml` in these retained runs: movement evidence is in `events.xml.gz`. Batch `output_*_summary.csv` files belong to the multi-run workflow and are not used by this single-run connector.

## Complete tool catalogue

Arguments ending in `?` are optional. All times and delays are seconds from simulation start, not wall-clock dates. Analysis run IDs are UUIDs, not arbitrary filesystem paths.

| Tool and arguments | Exact purpose and result | Files read/written |
| --- | --- | --- |
| `create_scenario_workspace(workspace_id, use_case, building_block)` | Copies a built-in scenario to an isolated workspace; returns its ID and path. Example selectors: `UC1`, `BB1`. | Reads built-in XML and operational plan; writes workspace copies. |
| `read_scenario_file(workspace_id, filename)` | Returns the named supported file's complete text. | Reads one workspace file from the allowlist above. |
| `write_scenario_file(workspace_id, filename, content)` | Validates syntax and scenario references, then atomically replaces the file. Returns written status. | Reads related workspace inputs for validation; writes the selected XML/JSON file. |
| `list_scenario_entities(workspace_id, entity_type)` | Lists XML entity records or operational-plan entries. | Reads network, schedule, vehicles or operational plan according to entity type. |
| `get_scenario_entity(workspace_id, entity_type, entity_id)` | Retrieves one entity; fails if absent. | Same mapping as entity listing. |
| `upsert_scenario_entity(workspace_id, entity_type, entity_id, value, create?=false)` | Updates XML attributes or replaces a plan entry. `create=true` requires a new ID; default requires an existing ID. Validates before saving; returns the entity. | Reads related workspace files; writes the corresponding XML or operational-plan JSON. |
| `run_simulation_once(workspace_id, seed?=4711, horizon_seconds?=21600)` | Validates and snapshots the current explicit timetable; runs exactly one simulation; returns persisted metrics and train/conflict records. Horizon: 600–172800 seconds; seed: signed 64-bit integer. Execution timeout: 300 seconds. Failed runs raise errors, never a successful zero-delay result. | Reads five workspace inputs; writes frozen inputs, simulator outputs, `simulation.json` and `run.json`. |
| `inspect_simulation_run(run_id)` | Returns the full persisted aggregate result, positive terminal-delay records under `delay_conflict_evidence`, and delay definition. That field is a delayed-train list, not causal conflict proof. | Reads `run.json`; does not revalidate frozen hashes or parse events. |
| `list_delayed_trains(run_id, minimum_delay_seconds?=0, include_unfinished?=true, offset?=0, limit?=100)` | Returns trains with terminal delay strictly greater than the threshold, plus unfinished/not-started outcomes by default. Unknown outcomes first, then decreasing delay and vehicle ID. Includes aggregate metrics, total matches, page count and `next_offset`. Missing arrivals remain unknown, never zero. | Reads `run.json`; verifies frozen inputs. No raw event read needed. |
| `inspect_train_timeline(run_id, train_id?, limit?=100)` | Sorted train records with scheduled/actual stop arrival/departure, dwell, departure delay, terminal completion, aggregate terminal delay and source event indices. Missing event measurements are null. | Reads `run.json`, verified inputs including schedule offsets, and events XML. |
| `inspect_train_movements(run_id, train_id, link_id?, start_seconds?, end_seconds?, offset?=0, limit?=100)` | Pages raw vehicle-associated XML events in stream order, including movement, reservation/release, facility and train-state events. Resolves vehicle ID or departure ID. Link filter matches `link`, `headLink` or `tailLink`; time range is inclusive. Returns original XML attributes plus `_index` (zero-based event position), `_seconds`, total, count and `next_offset`. | Reads `run.json`, verifies inputs, parses events XML. |
| `locate_first_delay(run_id, reference_run_id, train_id)` | Requires an explicit completed isolated one-train reference with the same route ID. Compares link entry/exit times relative to each scheduled departure; returns first positive baseline deviation, link deviations, separate departure delay and simulator delay estimates. Includes reference ID/hash. | Reads both run records, verifies both input snapshots, parses both events XML streams. |
| `inspect_run_conflicts(run_id, train_id?, resource_id?, limit?=100)` | Filters retained headway-conflict pairs; adds reservation/release intervals, head/tail passage, overlap if observed, event indices, requirement and maximum head-/tail-head deficit. Returns count and truncation flag. Train filter uses vehicle IDs; resource filter uses link IDs. | Reads `run.json` headway evidence, verifies inputs, parses events XML for the involved vehicles. |
| `assess_run_feasibility(run_id, period_seconds?)` | Reports expected/completed trains, terminal delay and positive non-terminal facility-event delay checks, separate headway admissibility, period train counts and drain/boundary evidence. `delay_feasible` requires completion and no positive delays. | Reads `run.json`, verifies inputs, parses facility events XML. |
| `compare_simulation_runs(before_run_id, after_run_id, limit?=100)` | Compares input hashes, changed schedule departures/routes, order, aggregate metrics and worsening terminal delays for matched departure IDs. | Reads both `run.json` records and verifies both snapshots. Returns event paths as provenance; does not parse or validate event streams. |

XML entity mapping: `node`, `link` → network; `stopFacility`, `transitLine`, `transitRoute`, `departure` → schedule; `vehicleType`, `vehicle` → vehicles. Plan types: `product`, `flow`, `pattern`, `mix`, `operatingMode`. Operating-mode IDs are `mix:pattern1,pattern2`. Legacy XML upserts set scalar attributes. Nested route/departure creation requires the structured batch API with explicit parent IDs. Use `query_scenario` and structured editing tools for normal agent workflows; raw-file tools remain compatibility escape hatches. Updating a plan alone does not generate a timetable: the single-run tool executes `schedule.xml` as supplied.

## Structured scenario authoring

The preferred interface handles XML internally. Start with a copied template or `create_empty_scenario`, query only the entities you need, then submit semantic edits.

| Tool and arguments | Purpose | Files |
| --- | --- | --- |
| `create_empty_scenario(workspace_id)` | Empty network, stops/lines/routes, vehicles/types and operational plan. Starts from the tested UC1/BB1 configuration. | Creates all five workspace inputs. |
| `query_scenario(workspace_id, entity_type, entity_id?, parent_id?, offset?=0, limit?=100, include_children?=false)` | Compact pages (max 200), exact ID/parent filtering, total/next offset, current revision. Supports all entity types plus `config` modules and top-level `plan` sections. Child detail is opt-in. | Reads workspace inputs internally, returns JSON only. |
| `describe_scenario_editing()` | Operation fields, supported structured child grammar, constraints. | None. |
| `apply_scenario_edits(workspace_id, operations, dry_run?=false, expected_revision?)` | Create/update/delete entities; edit nested route paths/profiles, rolling-stock properties and rail attributes; config parameters/sets and operational-plan fields. Up to 10,000 operations, validation of the final combined state. | Reads all inputs, writes changed files only. |
| `add_train_departure(workspace_id, route_id, departure_id, departure_time, vehicle_type, vehicle_id?, dry_run?, expected_revision?)` | Creates the departure on the exact route and a dedicated vehicle in one transaction. Default vehicle ID: `<departure_id>_vehicle`. | Schedule + vehicles. |
| `move_train_departure(workspace_id, departure_id, departure_time, dry_run?, expected_revision?)` | Moves one departure, preserving route and vehicle. | Schedule. |
| `replace_timetable(workspace_id, departures, periods?=1, period_seconds?=1800, dry_run?, expected_revision?)` | Replaces departures and their referenced vehicles with structured service records; optionally repeats a pattern (1–100 periods, max 10,000 departures). Leaves routes/profiles and unrelated fleet inventory intact. | Schedule + vehicles. |

`departure_time` and XML timetable offsets use `HH:MM:SS`, allowing hours above 23. A repeated pattern appends `_p0`, `_p1`, ... to departure IDs and explicitly supplied vehicle IDs. Unused routes may have zero departures; the Java runner omits these routes from the live simulation to satisfy SwissRailRaptor, preserving them in the workspace and frozen input snapshot. Dummy trains at 24:00 are unnecessary.

Every batch validates the final state before writing. Invalid cross-file references, disconnected routes, unordered stops/offsets, duplicate IDs, invalid times, reused departure vehicles and invalid physical values are rejected. `dry_run` returns the planned operations and changed filenames without saving. Pass a revision returned by `query_scenario` as `expected_revision` to reject stale edits. All MCP workspace reads, edits and simulation snapshots share a process/thread lock. Multi-file writes use an undo journal: a write failure rolls back; an interrupted process is recovered before the next MCP access. Direct external file readers/writers do not participate in this lock. This is not a filesystem power-loss guarantee.

The serializer preserves unedited elements, comments, namespaced data and doctypes semantically; whitespace and namespace prefixes may change. It never fetches external DTDs. Core structure and reference checks are application validation, not exhaustive MATSim XSD/module validation. Additional module-specific constraints are checked by the Java reader when running. Simulation remains required for feasibility.

Scenario IDs are unique per entity type across the workspace. A vehicle can serve only one departure in this explicit-timetable runner. Creation of a route needs a `parent_id` identifying its line, a transport mode, ordered stops and connected link path. A departure needs its route as `parent_id`. Deletes never silently cascade: include dependent edits in the same batch. To remove operational-plan entries, replace the relevant top-level collection with `set_plan`; the legacy plan upsert is useful for a single entry.

Configuration accepts named modules, parameters and parameter sets. The single-run runner deliberately overrides input/output paths, run ID, seed, horizon/start time, stuck-vehicle handling, iteration/output settings and certain controller/routing settings through `RailsimConfigHelper`. Use `run_simulation_once` arguments for seed/horizon. Editing those overridden values does not alter the runner's behavior.

Additional file/URL inputs are rejected; only the runner-controlled network,
schedule and fleet file parameters may refer to non-empty inputs. Configuration
changes must preserve that snapshot boundary.

### Examples (tool arguments are JSON)

Add a departure without reading XML:

```json
{"workspace_id":"experiment","route_id":"FV_LR","departure_id":"extra_fv","departure_time":"00:12:00","vehicle_type":"FV"}
```

Pass that object to `add_train_departure`. To change it, call `move_train_departure` with `workspace_id`, `departure_id` and `departure_time`.

A coordinated network edit using `apply_scenario_edits`:

```json
{"workspace_id":"experiment","operations":[
  {"action":"create","entity_type":"node","entity_id":"extension","attributes":{"x":10800,"y":0}},
  {"action":"create","entity_type":"link","entity_id":"extension_link","attributes":{"from":"n11","to":"extension","length":70,"freespeed":30,"capacity":9999,"permlanes":1,"modes":"rail"}}
]}
```

Rolling stock and config changes in one batch:

```json
{"workspace_id":"experiment","operations":[
  {"action":"update","entity_type":"vehicleType","entity_id":"RV","replace_children":{"length":[{"type":"length","attributes":{"meter":210}}]}},
  {"action":"set_config","module":"railsim","parameters":{"pollInterval":2}}
]}
```

A child record is `{type, id?, attributes?, text?, children?}`. `replace_children` maps a direct child tag to its complete replacement list; other child groups survive. An empty list removes that group. `attributes` updates individual attributes; null removes one, and IDs are immutable. To edit an individual route stop, fetch that route with `include_children=true`, change the relevant stop inside its `routeProfile`, and replace only the `routeProfile` group. Link resources and rolling-stock acceleration/deceleration use typed MATSim `attributes/attribute` records; discover the supported grammar using `describe_scenario_editing`.

### Verification

Coverage includes parent selection, cross-file vehicle creation, invalid-batch rejection, dry runs, stale revisions, write-failure rollback, interrupted-commit recovery, nested-data preservation, path confinement, and timetable replacement. `test_mcp_authoring.py` builds a complete scenario from structured MCP entity results in an empty workspace, edits its timetable, and runs the real Java simulator—no XML file payload crosses the client boundary.

## Optimisation-agent workflow

1. Edit the workspace, then call `run_simulation_once`; retain its run ID and input hash.
2. Call `assess_run_feasibility` and `list_delayed_trains`. Follow `next_offset` until null when collecting all problematic trains.
3. Call `inspect_train_timeline` for a delayed train, then `locate_first_delay` with the matching isolated reference. The server requires identical network/config inputs and matching route profile and vehicle type; the caller must choose a suitable reference.
4. Call `inspect_run_conflicts` to identify measured headway conflicts. Use `inspect_train_movements` on relevant trains/links/time windows for raw reservation, passage and release evidence, even where no headway violation was recorded.
5. Make a schedule correction, simulate again, and use `compare_simulation_runs` to track input and outcome changes. A proposal is not validated until simulated.

No analysis tool mutates inputs or launches a simulation. To locate a resource blocker, the caller may compare the suspected trains' movement events; the API does not automatically prove the dispatcher's causal chain. Headway violations and delay are separate tests: the pilot included delayed schedules with zero headway violations.

## Bounds and interpretation

- Report limits are integers 1–200. New delay/movement tools support non-negative offsets and explicit pagination. Existing timeline/conflict/comparison lists are capped; select an individual train/resource to narrow them. First-delay link deviations and internal estimates, and feasibility's intermediate-event list, cap at 200.
- `RunAnalysis` tools require a completed run and hash-valid frozen inputs. Event-reading tools additionally reject missing/corrupt event streams, compressed files over 128 MiB and decompressed streams over 512 MiB. Events are parsed on each call, with no persistent cache.
- Terminal delays come from the aggregate result and are signed actual minus planned terminal arrival, with unfinished trains excluded from delay statistics. Early arrivals can be negative; select or clamp positive delays explicitly rather than allowing early trains to cancel delayed ones. Internal XML `delay` values are simulator estimates and are labelled separately.
- Warm-up and circular resource headway are explicitly unverified by the feasibility report. Scheduled boundary gaps and successful drain are evidence, not an unlimited-period capacity proof.
- Existing timeline/link helpers retain first occurrences for a vehicle/facility or link. Repeated visits, repeated route IDs across lines and vehicle reuse require care; the validated pilot uses unique vehicles and simple non-repeating routes.
- Frozen input hashes detect snapshot edits. Event files and aggregate output records are not cryptographically authenticated against each other.

## Verification

The retained-analysis stdio test generates synthetic delayed, corrected and isolated
reference records at runtime. No archived simulation output or machine-specific run
IDs are needed. The separate authoring and simulation tests execute the actual Java
simulator through the MCP client. See [review and validation](review.md).

```sh
.venv/bin/python -m unittest discover -s src/test/python -p test_analysis.py
.venv/bin/python -m unittest discover -s src/test/integration -p test_mcp_run_analysis.py
```
