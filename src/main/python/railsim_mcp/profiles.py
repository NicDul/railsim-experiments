import copy, json, math, shutil, uuid
from pathlib import Path
import xml.etree.ElementTree as ET
from .uc1 import events, trace, stamp, PERIOD
from .search_evidence import reference_context, layout, holds
from .timetable import replace_timetable
from .validation import evaluate


def reference_windows(runs_root, baselines):
    """product -> resource -> (claim, granted, released) seconds after departure, whole seconds.

    granted..released is the exclusive hold (railsim EXHAUSTED, as in the gap template).
    claim is the earliest deadlock-avoidance (DLA) look-ahead claim on the resource, or
    granted when there is none. In railsim a DLA claim cannot be placed on a resource
    another train holds exclusively, but two claims can coexist.
    """
    for baseline in baselines.values():
        run = json.loads((runs_root / baseline["run_id"] / "run.json").read_text())
        rows = holds(
            run["events_file"], layout(Path(run["snapshot_directory"]) / "network.xml")
        )
        keys = [(row["resource"], row["vehicle"]) for row in rows]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "multiple disjoint visits to one resource need a multi-interval model; unsupported"
            )
    offsets = reference_context(runs_root, baselines)["exclusive_reference_offsets"]
    windows = {}
    for product, baseline in baselines.items():
        run = json.loads((runs_root / baseline["run_id"] / "run.json").read_text())
        links = layout(Path(run["snapshot_directory"]) / "network.xml")
        claims = {}
        for event in events(run["events_file"]):
            if (
                event["type"] == "railsimLinkStateChangeEvent"
                and event.get("dla") == "true"
                and event.get("link") in links
                and event["state"] != "EMPTY"
            ):
                resource = links[event["link"]]
                claims[resource] = min(
                    claims.get(resource, math.inf), float(event["time"])
                )
        windows[product] = {
            resource: (
                math.floor(min(claims.get(resource, w["granted"]), w["granted"])),
                math.floor(w["granted"]),
                math.ceil(w["released"]),
            )
            for resource, w in offsets[product].items()
        }
    return windows
