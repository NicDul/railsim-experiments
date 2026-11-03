"""Local stdio MCP entry point for the hand-built scenario repository."""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from railsim_mcp.analysis import RunAnalysis
from railsim_mcp.workspace import ScenarioWorkspace


from railsim_mcp.configuration import Settings

WORKSPACES: ScenarioWorkspace
ANALYSIS: RunAnalysis
mcp = FastMCP("railsim-mcp")


@mcp.tool()
def create_scenario_workspace(workspace_id: str, use_case: str, building_block: str) -> dict[str, str]:
    """Create an isolated writable copy of a built-in use case/building block."""
    path = WORKSPACES.create(workspace_id, use_case, building_block)
    return {"workspace_id": workspace_id, "path": str(path)}


@mcp.tool()
def read_scenario_file(workspace_id: str, filename: str) -> dict[str, str]:
    """Read one supported XML or operational-plan file from a workspace."""
    return {"filename": filename, "content": WORKSPACES.read(workspace_id, filename)}


@mcp.tool()
def write_scenario_file(workspace_id: str, filename: str, content: str) -> dict[str, str]:
    """Atomically replace a supported file after full scenario/reference validation."""
    WORKSPACES.write(workspace_id, filename, content)
    return {"workspace_id": workspace_id, "filename": filename, "status": "written"}


@mcp.tool()
def list_scenario_entities(workspace_id: str, entity_type: str) -> dict:
    """Discover supported network, schedule, and vehicle entities in a workspace."""
    return {"workspace_id": workspace_id, "entity_type": entity_type,
            "entities": WORKSPACES.entities(workspace_id, entity_type)}


@mcp.tool()
def get_scenario_entity(workspace_id: str, entity_type: str, entity_id: str) -> dict:
    """Read one network node/link, stop, line, route, departure, or vehicle type."""
    return WORKSPACES.get_entity(workspace_id, entity_type, entity_id)


@mcp.tool()
def upsert_scenario_entity(workspace_id: str, entity_type: str, entity_id: str,
                           value: dict, create: bool = False) -> dict:
    """Create an XML entity/update its attributes or mutate a plan product/flow/pattern/mix/mode."""
    return WORKSPACES.upsert_entity(workspace_id, entity_type, entity_id, value, create)


@mcp.tool()
def run_simulation_once(workspace_id: str, seed: int = 4711, horizon_seconds: int = 21600) -> dict:
    """Run an explicit timetable once on a frozen snapshot of the workspace."""
    return WORKSPACES.run_once(workspace_id, seed, horizon_seconds)


@mcp.tool()
def inspect_simulation_run(run_id: str) -> dict:
    """Inspect persisted terminal delays, completion counts, and delayed-train conflict evidence for one run."""
    return WORKSPACES.inspect_run(run_id)


@mcp.tool()
def inspect_train_timeline(run_id: str, train_id: str | None = None, limit: int = 100) -> dict:
    """Read scheduled and actual train/stop times from a retained completed run (max 200 trains)."""
    return ANALYSIS.timeline(run_id, train_id, limit)


@mcp.tool()
def locate_first_delay(run_id: str, reference_run_id: str, train_id: str) -> dict:
    """Compare one train's link events with an explicitly selected isolated one-train reference run."""
    return ANALYSIS.first_delay(run_id, reference_run_id, train_id)


@mcp.tool()
def inspect_run_conflicts(run_id: str, train_id: str | None = None,
                          resource_id: str | None = None, limit: int = 100) -> dict:
    """Read measured headway conflicts and event-derived resource occupancy candidates (max 200)."""
    return ANALYSIS.conflicts(run_id, train_id, resource_id, limit)


@mcp.tool()
def assess_run_feasibility(run_id: str, period_seconds: int | None = None) -> dict:
    """Assess completion, terminal/intermediate delay, headway, period counts, and boundary evidence."""
    return ANALYSIS.feasibility(run_id, period_seconds)


@mcp.tool()
def assess_search_candidate(run_id: str) -> dict:
    """Evaluate the frozen measurement cohort [3600,7200): zero exit delay, headways diagnostic.

    This standalone report uses the retained schedule references. The search controller
    additionally checks these references against isolated calibration and frozen hashes.
    """
    from .search import assess_retained_run
    return assess_retained_run(WORKSPACES, run_id)


@mcp.tool()
def compare_simulation_runs(before_run_id: str, after_run_id: str, limit: int = 100) -> dict:
    """Compare retained frozen inputs, timetable changes, metrics, and train regressions (max 200)."""
    return ANALYSIS.compare(before_run_id, after_run_id, limit)


@mcp.tool()
def list_delayed_trains(run_id: str, minimum_delay_seconds: float = 0,
                        include_unfinished: bool = True, offset: int = 0, limit: int = 100) -> dict:
    """Page terminal delays ranked by severity, including unfinished/unknown outcomes by default."""
    return ANALYSIS.delayed_trains(run_id, minimum_delay_seconds, include_unfinished, offset, limit)


@mcp.tool()
def inspect_train_movements(run_id: str, train_id: str, link_id: str | None = None,
                            start_seconds: float | None = None, end_seconds: float | None = None,
                            offset: int = 0, limit: int = 100) -> dict:
    """Page raw train events, including link passage, reservations/releases and simulator state."""
    return ANALYSIS.movements(run_id, train_id, link_id, start_seconds, end_seconds, offset, limit)


@mcp.tool()
def create_empty_scenario(workspace_id: str) -> dict:
    """Start a new empty network/schedule/fleet/plan with the tested UC1 runner configuration."""
    return WORKSPACES.create_empty(workspace_id)


@mcp.tool()
def query_scenario(workspace_id: str, entity_type: str, entity_id: str | None = None,
                   parent_id: str | None = None, offset: int = 0, limit: int = 100,
                   include_children: bool = False) -> dict:
    """Page compact entities or config/plan sections; filter by ID/parent. Returns revision for guarded edits.

    Types: node, link, stopFacility, transitLine, transitRoute, departure, vehicleType,
    vehicle, product, flow, pattern, mix, operatingMode, config, plan.
    Request include_children for one route/type/module to see its structured details.
    """
    return WORKSPACES.query(workspace_id, entity_type, entity_id, parent_id, offset, limit, include_children)


@mcp.tool()
def describe_scenario_editing() -> dict:
    """Discover structured operation fields, child grammar and transaction semantics; no XML required."""
    from railsim_mcp.authoring import CHILDREN
    return {
        'operations': {
            'create': 'entity_type, entity_id, attributes={}, children=[]; parent_id required for transitRoute/departure',
            'update': 'entity_type, entity_id, attributes={}, replace_children={tag:[records]}; null attribute deletes it',
            'delete': 'entity_type, entity_id; dependent references must also be removed in this batch',
            'add_departure': 'route_id, departure_id, departure_time HH:MM:SS, vehicle_type, optional vehicle_id; creates a dedicated vehicle',
            'replace_departures': 'departures: array of add_departure fields; replaces timetable and its referenced vehicles, preserves unrelated fleet',
            'set_config': 'module, parameters={name:value}; null deletes; optional parameter_sets=[structured records] replaces sets',
            'set_plan': 'fields={volumes,products,flows,patterns,mixes,modes,...}; replaces supplied top-level fields',
        },
        'child_record': {'type':'tag', 'id':'optional', 'attributes':{}, 'text':'optional', 'children':[]},
        'allowed_children': CHILDREN,
        'semantics': 'Ordered operations, final-state validation, dry run, revision guard, serialized multi-file commit with rollback/recovery. Empty routes are allowed.',
        'constraints': 'Entity IDs globally unique within each type; one vehicle per departure. Paths connected with ordered terminal stops. Use simulation to check feasibility. Config reader validates additional module-specific constraints at run time.',
    }


@mcp.tool()
def apply_scenario_edits(workspace_id: str, operations: list[dict], dry_run: bool = False,
                          expected_revision: str | None = None) -> dict:
    """Apply a coordinated structured edit batch across network, schedule, vehicles, config and plan.

    Call describe_scenario_editing for operation shapes. No XML strings. Validates the entire
    final scenario before committing; rejects invalid batches without partial edits. Returns
    operation summaries, changed files and revision. dry_run validates without saving.
    """
    return WORKSPACES.edit(workspace_id, operations, dry_run, expected_revision)


@mcp.tool()
def add_train_departure(workspace_id: str, route_id: str, departure_id: str,
                         departure_time: str, vehicle_type: str, vehicle_id: str | None = None,
                         dry_run: bool = False, expected_revision: str | None = None) -> dict:
    """Add a departure to exactly the named route and create its dedicated vehicle atomically."""
    op = dict(action='add_departure', route_id=route_id, departure_id=departure_id,
              departure_time=departure_time, vehicle_type=vehicle_type)
    if vehicle_id is not None:
        op['vehicle_id'] = vehicle_id
    return WORKSPACES.edit(workspace_id, [op], dry_run, expected_revision)


@mcp.tool()
def move_train_departure(workspace_id: str, departure_id: str, departure_time: str,
                          dry_run: bool = False, expected_revision: str | None = None) -> dict:
    """Change one departure time, preserving its route, vehicle and all other departures."""
    return WORKSPACES.edit(workspace_id, [dict(action='update', entity_type='departure',
                           entity_id=departure_id, attributes={'departureTime':departure_time})],
                           dry_run, expected_revision)


@mcp.tool()
def replace_timetable(workspace_id: str, departures: list[dict], periods: int = 1,
                       period_seconds: int = 1800, dry_run: bool = False,
                       expected_revision: str | None = None) -> dict:
    """Replace timetable using route_id, departure_id, departure_time, vehicle_type, optional vehicle_id.

    Repeats the supplied pattern for periods (1..100), shifting time by period_seconds.
    With periods > 1, appends _p0, _p1, ... to departure/vehicle IDs. Removes vehicles
    referenced by the old timetable, preserves unrelated inventory and route profiles.
    """
    from railsim_mcp.workspace import WorkspaceError
    if not 1 <= periods <= 100 or period_seconds <= 0 or len(departures) * periods > 10000:
        raise WorkspaceError('requires 1..100 periods, positive period_seconds, at most 10000 departures')
    expanded = []
    for period in range(periods):
        for departure in departures:
            row = dict(departure)
            seconds = WORKSPACES._seconds(row['departure_time'], 'departure_time') + period * period_seconds
            hours, rest = divmod(seconds, 3600)
            minutes, seconds = divmod(rest, 60)
            row['departure_time'] = f'{int(hours):02}:{int(minutes):02}:{seconds:06.3f}'
            if periods > 1:
                row['departure_id'] += f'_p{period}'
                if 'vehicle_id' in row:
                    row['vehicle_id'] += f'_p{period}'
            expanded.append(row)
    return WORKSPACES.edit(workspace_id, [dict(action='replace_departures', departures=expanded)],
                           dry_run, expected_revision)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Local Railsim MCP server (stdio)")
    parser.add_argument("--railsim-root", help="Simulator checkout; defaults to RAILSIM_PROJECT_ROOT")
    parser.add_argument("--state-dir", help="External workspaces/runs directory; defaults to RAILSIM_STATE_ROOT")
    args = parser.parse_args()
    try:
        settings = Settings.load(args.railsim_root, args.state_dir)
    except ValueError as exc:
        parser.error(str(exc))
    global WORKSPACES, ANALYSIS
    WORKSPACES = ScenarioWorkspace(settings.project / "src/main/resources/scenarios",
                                  settings.state / "workspaces", project_root=settings.project,
                                  runs_root=settings.state / "runs")
    ANALYSIS = RunAnalysis(WORKSPACES.runs_root)
    mcp.run()


if __name__ == "__main__":
    main()
