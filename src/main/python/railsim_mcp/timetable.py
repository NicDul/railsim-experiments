"""Ordinary workspace timetable editing; no protocol or global workspace."""


def replace_timetable(
    workspaces,
    workspace_id: str,
    departures: list[dict],
    periods: int = 1,
    period_seconds: int = 1800,
    dry_run: bool = False,
    expected_revision: str | None = None,
) -> dict:
    """Replace timetable using route_id, departure_id, departure_time, vehicle_type, optional vehicle_id.

    Repeats the supplied pattern for periods (1..100), shifting time by period_seconds.
    With periods > 1, appends _p0, _p1, ... to departure/vehicle IDs. Removes vehicles
    referenced by the old timetable, preserves unrelated inventory and route profiles.
    """
    from railsim_mcp.workspace import WorkspaceError

    if (
        not 1 <= periods <= 100
        or period_seconds <= 0
        or len(departures) * periods > 10000
    ):
        raise WorkspaceError(
            "requires 1..100 periods, positive period_seconds, at most 10000 departures"
        )
    expanded = []
    for period in range(periods):
        for departure in departures:
            row = dict(departure)
            seconds = (
                workspaces._seconds(row["departure_time"], "departure_time")
                + period * period_seconds
            )
            hours, rest = divmod(seconds, 3600)
            minutes, seconds = divmod(rest, 60)
            row["departure_time"] = f"{int(hours):02}:{int(minutes):02}:{seconds:06.3f}"
            if periods > 1:
                row["departure_id"] += f"_p{period}"
                if "vehicle_id" in row:
                    row["vehicle_id"] += f"_p{period}"
            expanded.append(row)
    return workspaces.edit(
        workspace_id,
        [dict(action="replace_departures", departures=expanded)],
        dry_run,
        expected_revision,
    )
