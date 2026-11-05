import copy, json, math, shutil, uuid
from pathlib import Path
import xml.etree.ElementTree as ET
from .uc1 import events, trace, stamp, PERIOD
from .search_evidence import reference_context, layout
from .timetable import replace_timetable
from .validation import evaluate


def link_graph(network):
    links = {}
    for link in ET.parse(network).getroot().iter("link"):
        attrs = {a.get("name"): a.text for a in link.iter("attribute")}
        links[link.get("id")] = dict(
            start=link.get("from"),
            end=link.get("to"),
            resource=attrs.get("railsimResourceId"),
        )
    return links


def paths(links, first, last):
    """All simple link paths from link `first` to link `last` (the station area is acyclic)."""
    found = []

    def walk(path):
        if path[-1] == last:
            found.append(path)
            return
        for nxt, link in links.items():
            if (
                link["start"] == links[path[-1]]["end"]
                and nxt not in path
                and not nxt.startswith("loop")
            ):
                walk(path + [nxt])

    walk([first])
    return found


def add_platform_variants(schedule_path, network_path, base_routes):
    """Add <route>_P<platform> variants for every base route; return variant -> (product, platform)."""
    text = schedule_path.read_text()
    prolog = text[: text.index("<transitSchedule")]
    root = ET.fromstring(text)
    links = link_graph(network_path)
    platforms = sorted(
        l for l, v in links.items() if (v["resource"] or "").startswith("platform")
    )
    stops = {f.get("linkRefId"): f.get("id") for f in root.iter("stopFacility")}
    routes = {r.get("id"): r for r in root.iter("transitRoute")}
    line_of = {
        r.get("id"): line
        for line in root.iter("transitLine")
        for r in line.iter("transitRoute")
    }
    variants = {}
    for product, base_id in base_routes.items():
        base = routes[base_id]
        sequence = [l.get("refId") for l in base.find("route")]
        # entry/exit outside the station: the first and last link shared by every path
        inner = [l for l in sequence if not l.startswith("loop")]
        candidates = paths(links, inner[0], inner[-1])
        for platform in platforms:
            options = [p for p in candidates if platform in p]
            if not options:
                continue
            path = min(options, key=len)
            variant = copy.deepcopy(base)
            variant_id = f"{base_id}_P{platforms.index(platform) + 1}"
            variant.set("id", variant_id)
            route = variant.find("route")
            for child in list(route):
                route.remove(child)
            for link in ["loop01"] + path + ["loop02"]:
                ET.SubElement(route, "link", refId=link)
            for stop in variant.find("routeProfile"):
                if stop.get("refId") not in ("L1", "R1"):
                    stop.set(
                        "refId", stops[platform]
                    )  # the M stop moves to the chosen platform
            departures = variant.find("departures")
            if departures is not None:
                for child in list(departures):
                    departures.remove(child)
            line_of[base_id].append(variant)
            variants[variant_id] = dict(
                product=product, platform=platform, base=base_id, links=path
            )
    schedule_path.write_text(prolog + ET.tostring(root, encoding="unicode") + "\n")
    return variants


def calibrate(ws, workspace, variant, product, seed, period=1800):
    """Isolated run of one variant; freeze its route profile to the measured stop times."""
    replace_timetable(
        ws,
        workspace,
        [
            dict(
                route_id=variant,
                departure_id=f"{product}_00",
                vehicle_id=f"{product}_00",
                vehicle_type=product,
                departure_time="00:00:00",
            )
        ],
        1,
        period,
    )
    run = ws.run_once(workspace, seed, 7 * period)
    row = run["trains"][0]
    if row["status"] != "terminal_arrived":
        raise RuntimeError(f"isolated {variant} did not complete")
    trajectory = trace(run["events_file"])[product + "_00"]
    route = ws.get_entity(workspace, "transitRoute", variant)
    profile = copy.deepcopy(
        next(c for c in route["children"] if c["type"] == "routeProfile")
    )
    for stop in profile["children"]:
        for e in trajectory["facilities"]:
            if e["facility"] == stop["attributes"]["refId"]:
                key = (
                    "arrivalOffset"
                    if e["type"] == "VehicleArrivesAtFacility"
                    else "departureOffset"
                )
                stop["attributes"][key] = stamp(float(e["time"]))

    def clean(record):
        return {
            k: ([clean(c) for c in v] if k == "children" else v)
            for k, v in record.items()
            if v is not None
        }

    ws.edit(
        workspace,
        [
            dict(
                action="update",
                entity_type="transitRoute",
                entity_id=variant,
                replace_children={"routeProfile": [clean(profile)]},
            )
        ],
    )
    detours = sum(
        1 for e in events(run["events_file"]) if e["type"] == "railsimDetourEvent"
    )
    return dict(run_id=run["run_id"], terminal=row["actual_arrival"], detours=detours)


def simulate(ws, source, pattern, terminals, seed, period=1800, vehicle_types=None):
    workspace = "plat-" + uuid.uuid4().hex[:12]
    shutil.copytree(ws.workspace_root / source, ws.workspace_root / workspace)
    departures = [
        dict(
            route_id=v,
            departure_id=f"{p}_{i:02}",
            vehicle_id=f"{p}_{i:02}",
            vehicle_type=(vehicle_types or {}).get(p, p),
            departure_time=stamp(t),
        )
        for i, (p, t, v) in enumerate(pattern)
    ]
    replace_timetable(ws, workspace, departures, 6, period)
    run = ws.run_once(workspace, seed, 7 * period)
    expected = {
        f"{p}_{i:02}_p{k}": (t + k * period, v)
        for k in (2, 3)
        for i, (p, t, v) in enumerate(pattern)
    }
    score = evaluate(run, expected, terminals, period, check_routes=True)
    detours = sum(
        1 for e in events(run["events_file"]) if e["type"] == "railsimDetourEvent"
    )
    return dict(
        run_id=run["run_id"],
        passed=score["passed"] and detours == 0,
        max_delay=score["max_delay"],
        total_delay=score["total_delay"],
        errors=score["errors"][:3],
        missing=len(score["missing"]),
        detours=detours,
        platform_use={
            v: sum(r[2] == v for r in pattern) for v in {r[2] for r in pattern}
        },
    ), run
