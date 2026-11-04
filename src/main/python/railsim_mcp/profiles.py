import json
import math
from pathlib import Path
from .uc1 import events
from .search_evidence import layout, holds


def intervals(value):
    """Read legacy single-visit triples or multiple disjoint visit triples."""
    return [value] if isinstance(value[0], (int, float)) else value


def reference_windows(runs_root, baselines):
    """Variant -> resource -> visit triple(s), whole seconds after departure.

    granted..released is the exclusive hold (railsim EXHAUSTED, as in the gap template).
    claim is the earliest deadlock-avoidance (DLA) look-ahead claim on the resource, or
    granted when there is none. In railsim a DLA claim cannot be placed on a resource
    another train holds exclusively, but two claims can coexist.
    """
    windows = {}
    resource_map = None
    for product, baseline in baselines.items():
        run = json.loads((runs_root / baseline["run_id"] / "run.json").read_text())
        links = layout(Path(run["snapshot_directory"]) / "network.xml")
        if resource_map is not None and links != resource_map:
            raise ValueError("reference resource maps differ")
        resource_map = links
        rows = holds(run["events_file"], links)
        claims = {}
        for event in events(run["events_file"]):
            if (
                event["type"] == "railsimLinkStateChangeEvent"
                and event.get("dla") == "true"
                and event.get("link") in links
                and event["state"] != "EMPTY"
            ):
                resource = links[event["link"]]
                claims.setdefault(resource, []).append(float(event["time"]))
        grouped = {}
        for row in sorted(rows, key=lambda row: row["start"]):
            resource, grant, release = row["resource"], row["start"], row["end"]
            previous_release = max(
                (r["end"] for r in rows if r["resource"] == resource and r["end"] <= grant),
                default=-math.inf,
            )
            claim = min(
                [grant] + [
                    t for t in claims.get(resource, [])
                    if previous_release < t <= grant
                ]
            )
            grouped.setdefault(resource, []).append(
                (math.floor(claim), math.floor(grant), math.ceil(release))
            )
        windows[product] = {
            resource: visits[0] if len(visits) == 1 else visits
            for resource, visits in grouped.items()
        }
    return windows
