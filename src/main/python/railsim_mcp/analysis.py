"""Read-only analysis of completed, retained Railsim runs."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from railsim_mcp.workspace import WorkspaceError


class _BoundedEventStream:
    def __init__(self, stream, maximum):
        self.stream = stream
        self.remaining = maximum

    def read(self, size=-1):
        bound = self.remaining + 1 if size < 0 else min(size, self.remaining + 1)
        data = self.stream.read(bound)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise WorkspaceError('retained event stream exceeds the decompressed read bound')
        return data


class RunAnalysis:
    """Provides bounded reports using only completed run snapshots and event logs."""

    MAX_EVENT_FILE_BYTES = 128 * 1024 * 1024
    MAX_EVENT_STREAM_BYTES = 512 * 1024 * 1024
    MAX_LIMIT = 200

    def __init__(self, runs_root: Path):
        self.runs_root = runs_root.resolve()

    def _run(self, run_id: str) -> tuple[Path, dict]:
        try:
            canonical = str(uuid.UUID(run_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise WorkspaceError("invalid run_id") from exc
        root = self.runs_root / canonical
        path = root / "run.json"
        if not path.is_file() or not path.resolve().is_relative_to(self.runs_root):
            raise WorkspaceError(f"completed simulation run not found: {canonical}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceError(f"run metadata is unreadable: {canonical}") from exc
        if data.get("run_id") != canonical or data.get("status") != "completed":
            raise WorkspaceError(f"run is incomplete or inconsistent: {canonical}")
        hashes = data.get("input_file_sha256")
        if not isinstance(hashes, dict) or not hashes:
            raise WorkspaceError(f"run has no frozen input hashes: {canonical}")
        snapshot = root / "inputs"
        for filename, expected in hashes.items():
            if not isinstance(filename, str) or Path(filename).name != filename:
                raise WorkspaceError("run metadata contains an invalid frozen input name")
            source = snapshot / filename
            if not source.is_file() or not source.resolve().is_relative_to(root.resolve()):
                raise WorkspaceError(f"frozen run input is missing: {filename}")
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
            if actual != expected:
                raise WorkspaceError(f"frozen run input hash mismatch: {filename}")
        aggregate = hashlib.sha256("\n".join(f"{name}:{hashes[name]}" for name in sorted(hashes)).encode()).hexdigest()
        if data.get("input_sha256") != aggregate:
            raise WorkspaceError(f"aggregate frozen input hash mismatch: {canonical}")
        return root, data

    def _events(self, run_id: str, root: Path, selected: set[str] | None = None,
                include_state: bool = False) -> dict[str, list[dict]]:
        event_path = root / "output" / "ITERS" / "it.0" / f"{run_id}.0.events.xml.gz"
        if (not event_path.is_file() or not event_path.resolve().is_relative_to(root.resolve())
                or event_path.stat().st_size > self.MAX_EVENT_FILE_BYTES):
            raise WorkspaceError(f"retained event stream is missing or exceeds the read bound: {run_id}")
        by_person: dict[str, str] = {}
        result: dict[str, list[dict]] = defaultdict(list)
        index = 0
        try:
            with gzip.open(event_path, "rb") as stream:
                bounded = _BoundedEventStream(stream, self.MAX_EVENT_STREAM_BYTES)
                for _, elem in ET.iterparse(bounded, events=("end",)):
                    if elem.tag != "event":
                        elem.clear()
                        continue
                    event = dict(elem.attrib)
                    event_type = event.get("type")
                    if event_type == "TransitDriverStarts":
                        by_person[event.get("driverId", "")] = event.get("vehicleId", "")
                    vehicle = event.get("vehicle") or event.get("vehicleId")
                    if not vehicle and event.get("person"):
                        vehicle = by_person.get(event["person"])
                    if vehicle and (selected is None or vehicle in selected):
                        if include_state or event_type != "railsimTrainStateEvent":
                            event["_index"] = index
                            try:
                                event["_seconds"] = float(event.get("time", "nan"))
                            except ValueError:
                                event["_seconds"] = math.nan
                            result[vehicle].append(event)
                    index += 1
                    elem.clear()
        except (OSError, EOFError, ET.ParseError) as exc:
            raise WorkspaceError(f"retained event stream is corrupt: {run_id}") from exc
        return result

    def _stops(self, root: Path, route_id: str) -> list[dict]:
        schedule = root / "inputs" / "schedule.xml"
        if not schedule.is_file():
            raise WorkspaceError("frozen schedule is missing")
        try:
            tree = ET.parse(schedule)
        except (OSError, ET.ParseError) as exc:
            raise WorkspaceError("frozen schedule is unreadable") from exc
        for route in tree.iter():
            if route.tag.rsplit("}", 1)[-1] == "transitRoute" and route.get("id") == route_id:
                return [{"stop_id": stop.get("refId"),
                         "arrival_offset": self._number(stop.get("arrivalOffset")),
                         "departure_offset": self._number(stop.get("departureOffset"))}
                        for stop in route.iter() if stop.tag.rsplit("}", 1)[-1] == "stop"]
        return []

    @staticmethod
    def _number(value):
        try:
            return float(value)
        except (ValueError, TypeError):
            if not isinstance(value, str) or not re.fullmatch(r"\d{1,3}:\d{2}:\d{2}(?:\.\d+)?", value):
                return None
            hours, minutes, seconds = value.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    @staticmethod
    def _check_offset(offset):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise WorkspaceError("offset must be a non-negative integer")

    def delayed_trains(self, run_id: str, minimum_delay_seconds: float = 0,
                       include_unfinished: bool = True, offset: int = 0, limit: int = 100) -> dict:
        """Rank positive terminal delays; retain unknown outcomes as explicit problems."""
        self._check_limit(limit)
        self._check_offset(offset)
        if (isinstance(minimum_delay_seconds, bool) or not isinstance(minimum_delay_seconds, (int, float))
                or not math.isfinite(minimum_delay_seconds) or minimum_delay_seconds < 0):
            raise WorkspaceError("minimum_delay_seconds must be finite and non-negative")
        _, run = self._run(run_id)
        rows = [t for t in run.get("trains", [])
                if (t.get("terminal_delay_seconds") is not None
                    and t["terminal_delay_seconds"] > minimum_delay_seconds)
                or (include_unfinished and not t.get("arrived"))]
        rows.sort(key=lambda t: (t.get("terminal_delay_seconds") is not None,
                                 -(t.get("terminal_delay_seconds") or 0), t.get("vehicle_id", "")))
        page = rows[offset:offset + limit]
        return {"run_id": run_id, "input_sha256": run["input_sha256"],
                "source": "run.json (persisted simulation.json aggregates)",
                "delay_definition": "terminal delay strictly greater than threshold; unfinished delay is unknown",
                "metrics": self._metrics(run), "total": len(rows), "count": len(page),
                "next_offset": offset + len(page) if offset + len(page) < len(rows) else None,
                "trains": page}

    def movements(self, run_id: str, train_id: str, link_id: str | None = None,
                  start_seconds: float | None = None, end_seconds: float | None = None,
                  offset: int = 0, limit: int = 100) -> dict:
        """Page link passage, reservation/release, facility and train-state XML evidence."""
        self._check_limit(limit)
        self._check_offset(offset)
        for value in (start_seconds, end_seconds):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value < 0):
                raise WorkspaceError("time bounds must be finite and non-negative")
        if start_seconds is not None and end_seconds is not None and start_seconds > end_seconds:
            raise WorkspaceError("start_seconds must not exceed end_seconds")
        root, run = self._run(run_id)
        train = next((t for t in run.get("trains", [])
                      if train_id in (t.get("vehicle_id"), t.get("departure_id"))), None)
        if train is None:
            raise WorkspaceError(f"train not found in run: {train_id}")
        vehicle = train["vehicle_id"]
        events = self._events(run_id, root, {vehicle}, include_state=True).get(vehicle, [])
        rows = [e for e in events
                if (link_id is None or link_id in (e.get("link"), e.get("headLink"), e.get("tailLink")))
                and (start_seconds is None or e["_seconds"] >= start_seconds)
                and (end_seconds is None or e["_seconds"] <= end_seconds)]
        page = rows[offset:offset + limit]
        return {"run_id": run_id, "train_id": vehicle, "input_sha256": run["input_sha256"],
                "source": f"output/ITERS/it.0/{run_id}.0.events.xml.gz",
                "evidence_label": "raw_simulator_events_not_proven_delay_causes",
                "total": len(rows), "count": len(page),
                "next_offset": offset + len(page) if offset + len(page) < len(rows) else None,
                "events": page}

    def timeline(self, run_id: str, train_id: str | None = None, limit: int = 100) -> dict:
        self._check_limit(limit)
        root, run = self._run(run_id)
        trains = sorted(run.get("trains", []), key=lambda t: t.get("vehicle_id", ""))
        if train_id:
            trains = [t for t in trains if t.get("vehicle_id") == train_id or t.get("departure_id") == train_id]
            if not trains:
                raise WorkspaceError(f"train not found in run: {train_id}")
        selected = {t["vehicle_id"] for t in trains[:limit] if t.get("vehicle_id")}
        events = self._events(run_id, root, selected)
        rows = []
        for train in trains[:limit]:
            vehicle = train.get("vehicle_id")
            stop_events = events.get(vehicle, [])
            stops = self._stops(root, train.get("route_id", ""))
            by_facility: dict[str, dict] = defaultdict(dict)
            for event in stop_events:
                typ = event.get("type")
                if typ in ("VehicleArrivesAtFacility", "VehicleDepartsAtFacility"):
                    field = "arrival" if typ == "VehicleArrivesAtFacility" else "departure"
                    by_facility[event.get("facility", "")].setdefault(field, event.get("_seconds"))
                    by_facility[event.get("facility", "")].setdefault(field + "_event_index", event.get("_index"))
                    if event.get("delay") is not None:
                        by_facility[event.get("facility", "")][field + "_simulator_delay_seconds"] = self._number(event.get("delay"))
            stop_rows = []
            for pos, stop in enumerate(stops):
                found = by_facility.get(stop["stop_id"], {})
                arrival = found.get("arrival")
                departure = found.get("departure")
                stop_rows.append({"stop_id": stop["stop_id"],
                    "scheduled_arrival": train["scheduled_departure"] + stop["arrival_offset"] if train.get("scheduled_departure") is not None and stop["arrival_offset"] is not None else None,
                    "actual_arrival": arrival,
                    "scheduled_departure": train["scheduled_departure"] + stop["departure_offset"] if train.get("scheduled_departure") is not None and stop["departure_offset"] is not None else None,
                    "actual_departure": departure,
                    "dwell_seconds": departure - arrival if departure is not None and arrival is not None else None,
                    "simulator_delay_seconds": found.get("arrival_simulator_delay_seconds"),
                    "event_indices": {"arrival": found.get("arrival_event_index"), "departure": found.get("departure_event_index")}})
            first = stop_rows[0] if stop_rows else {}
            last = stop_rows[-1] if stop_rows else {}
            rows.append({"train_id": vehicle, "departure_id": train.get("departure_id"),
                "route_id": train.get("route_id"), "scheduled_departure": train.get("scheduled_departure"),
                "actual_departure": first.get("actual_departure"),
                "departure_delay_seconds": first.get("actual_departure") - train["scheduled_departure"] if first.get("actual_departure") is not None and train.get("scheduled_departure") is not None else None,
                "stops": stop_rows, "terminal_completion": last.get("actual_arrival"),
                "terminal_delay_seconds": train.get("terminal_delay_seconds"),
                "status": train.get("status")})
        return {"run_id": run_id, "input_sha256": run["input_sha256"], "count": len(rows), "trains": rows}

    def first_delay(self, run_id: str, reference_run_id: str, train_id: str) -> dict:
        target_root, target = self._run(run_id)
        ref_root, reference = self._run(reference_run_id)
        if reference.get("trains_expected") != 1 or reference.get("trains_arrived") != 1:
            raise WorkspaceError("reference_run_id must identify a completed, isolated one-train run")
        train = next((t for t in target.get("trains", []) if train_id in (t.get("vehicle_id"), t.get("departure_id"))), None)
        ref_train = reference.get("trains", [None])[0]
        if not train or not ref_train or train.get("route_id") != ref_train.get("route_id"):
            raise WorkspaceError("target train and isolated reference must use the same route")
        for name in ('network.xml', 'config.xml'):
            target_hash = target['input_file_sha256'].get(name)
            if target_hash is None or target_hash != reference['input_file_sha256'].get(name):
                raise WorkspaceError(f"isolated reference must use identical {name}")
        if self._train_signature(target_root, train) != self._train_signature(ref_root, ref_train):
            raise WorkspaceError("isolated reference must use the same route profile and vehicle type")
        target_events = self._events(run_id, target_root, {train["vehicle_id"]}, include_state=True).get(train["vehicle_id"], [])
        ref_events = self._events(reference_run_id, ref_root, {ref_train["vehicle_id"]}, include_state=True).get(ref_train["vehicle_id"], [])
        def link_times(items):
            values = defaultdict(dict)
            for e in items:
                if e.get("type") in ("entered link", "left link"):
                    side = "entry" if e["type"] == "entered link" else "exit"
                    values[e.get("link", "")].setdefault(side, e.get("_seconds"))
                    values[e.get("link", "")].setdefault(side + "_index", e.get("_index"))
            return values
        candidate, baseline = link_times(target_events), link_times(ref_events)
        reference_departure = ref_train.get("scheduled_departure")
        target_departure = train.get("scheduled_departure")
        deviations = []
        for link in sorted(candidate, key=lambda k: candidate[k].get("entry_index", math.inf)):
            if link not in baseline: continue
            record = {"link_id": link}
            for side in ("entry", "exit"):
                actual = candidate[link].get(side); ref_time = baseline[link].get(side)
                record[side + "_actual_seconds"] = actual
                record[side + "_reference_relative_seconds"] = ref_time - reference_departure if ref_time is not None and reference_departure is not None else None
                record[side + "_baseline_deviation_seconds"] = (actual - target_departure) - (ref_time - reference_departure) if actual is not None and ref_time is not None and target_departure is not None and reference_departure is not None else None
                record[side + "_event_index"] = candidate[link].get(side + "_index")
            deviations.append(record)
        first = next((d for d in deviations if any(d.get(s + "_baseline_deviation_seconds") is not None and d[s + "_baseline_deviation_seconds"] > 0 for s in ("entry", "exit"))), None)
        estimates = [{"time_seconds": e.get("_seconds"), "delay_seconds": self._number(e.get("delay")), "event_index": e.get("_index")}
                     for e in target_events if e.get("type") == "railsimTrainStateEvent" and self._number(e.get("delay")) not in (None, 0)]
        for estimate, event in zip(estimates, [e for e in target_events if e.get("type") == "railsimTrainStateEvent" and self._number(e.get("delay")) not in (None, 0)]):
            estimate["link_id"] = event.get("headLink")
            estimate["source"] = "railsimTrainStateEvent.delay"
        return {"run_id": run_id, "reference_run_id": reference_run_id, "reference_input_sha256": reference["input_sha256"],
            "train_id": train["vehicle_id"], "scheduled_departure": train.get("scheduled_departure"),
            "schedule_departure_delay_seconds": (next((e.get("_seconds") for e in target_events if e.get("type") == "VehicleDepartsAtFacility"), None) - train.get("scheduled_departure")) if next((e.get("_seconds") for e in target_events if e.get("type") == "VehicleDepartsAtFacility"), None) is not None else None,
            "first_positive_baseline_deviation": first, "link_deviations": deviations[:self.MAX_LIMIT],
            "internal_simulator_estimates": estimates[:self.MAX_LIMIT],
            "evidence_labels": {"link_deviations": "measured_from_retained_events", "internal_simulator_estimates": "simulator_reported_estimate", "causal_attribution": "not_proven"}}

    @staticmethod
    def _train_signature(root: Path, train: dict) -> tuple:
        def canonical(element):
            return (element.tag, tuple(sorted(element.attrib.items())), (element.text or '').strip(),
                    tuple(canonical(child) for child in element
                          if child.tag.rsplit('}', 1)[-1] != 'departures'))

        try:
            schedule = ET.parse(root / 'inputs/schedule.xml')
            fleet = ET.parse(root / 'inputs/vehicles.xml')
            route = next(e for e in schedule.iter() if e.tag.rsplit('}', 1)[-1] == 'transitRoute'
                         and e.get('id') == train['route_id'])
            vehicle = next(e for e in fleet.iter() if e.tag.rsplit('}', 1)[-1] == 'vehicle'
                           and e.get('id') == train['vehicle_id'])
            vehicle_type = next(e for e in fleet.iter() if e.tag.rsplit('}', 1)[-1] == 'vehicleType'
                                and e.get('id') == vehicle.get('type'))
        except (OSError, ET.ParseError, StopIteration, KeyError) as exc:
            raise WorkspaceError('baseline comparison requires frozen route and vehicle definitions') from exc
        return canonical(route), canonical(vehicle_type)

    def conflicts(self, run_id: str, train_id: str | None = None, resource_id: str | None = None, limit: int = 100) -> dict:
        self._check_limit(limit)
        root, run = self._run(run_id)
        conflicts = run.get("headway_conflicts")
        if not isinstance(conflicts, list):
            raise WorkspaceError("run has no retained headway conflict evidence")
        if train_id:
            conflicts = [c for c in conflicts if train_id in (c.get("previous_vehicle_id"), c.get("following_vehicle_id"))]
        if resource_id:
            conflicts = [c for c in conflicts if c.get("link_id") == resource_id]
        selected = {v for c in conflicts for v in (c.get("previous_vehicle_id"), c.get("following_vehicle_id"))}
        events = self._events(run_id, root, selected)
        def time_for(vehicle, link, event_type):
            return next((e.get("_seconds") for e in events.get(vehicle, []) if e.get("type") == event_type and e.get("link") == link), None)
        def state_time(vehicle, link, state):
            return next((e.get("_seconds") for e in events.get(vehicle, []) if e.get("type") == "railsimLinkStateChangeEvent" and e.get("link") == link and e.get("state") == state), None)
        rows = []
        for c in conflicts[:limit]:
            prior, following, link = c.get("previous_vehicle_id"), c.get("following_vehicle_id"), c.get("link_id")
            prior_entry = time_for(prior, link, "entered link"); following_entry = time_for(following, link, "entered link")
            prior_head_exit = time_for(prior, link, "railsimTrainLeavesLinkEvent")
            prior_reservation = state_time(prior, link, "IN_USE"); following_reservation = state_time(following, link, "IN_USE")
            prior_release = state_time(prior, link, "EMPTY")
            following_release = state_time(following, link, "EMPTY")
            overlap_start = max(prior_reservation, following_reservation) if prior_reservation is not None and following_reservation is not None else None
            overlap_end = min(prior_release, following_release) if prior_release is not None and following_release is not None else None
            overlap = [overlap_start, overlap_end] if overlap_start is not None and overlap_end is not None and overlap_start <= overlap_end else None
            rows.append({"resource_id": link, "train_pair": [prior, following],
                "reservation_interval_seconds": {"previous": [prior_reservation, prior_release], "following": [following_reservation, following_release]},
                "head_passage_seconds": {"previous": prior_entry, "following": following_entry},
                "tail_passage_seconds": {"previous": prior_head_exit, "following": time_for(following, link, "railsimTrainLeavesLinkEvent")},
                "release_seconds": {"previous": prior_release, "following": following_release},
                "overlap_interval_seconds": overlap,
                "overlap_observed": overlap is not None,
                "event_indices": {"previous_entry": next((e.get("_index") for e in events.get(prior, []) if e.get("type") == "entered link" and e.get("link") == link), None),
                    "previous_tail_exit": next((e.get("_index") for e in events.get(prior, []) if e.get("type") == "railsimTrainLeavesLinkEvent" and e.get("link") == link), None),
                    "previous_release": next((e.get("_index") for e in events.get(prior, []) if e.get("type") == "railsimLinkStateChangeEvent" and e.get("link") == link and e.get("state") == "EMPTY"), None),
                    "following_entry": next((e.get("_index") for e in events.get(following, []) if e.get("type") == "entered link" and e.get("link") == link), None)},
                "headway_head_to_head_seconds": c.get("headway_head_to_head_seconds"), "headway_tail_to_head_seconds": c.get("headway_tail_to_head_seconds"),
                "required_headway_seconds": c.get("required_headway_seconds"), "headway_deficit_seconds": max(c.get("violation_head_to_head_seconds", 0), c.get("violation_tail_to_head_seconds", 0)),
                "measurement": "retained_headway_analysis", "blocker_assessment": "candidate_inferred_from_resource_and_headway_evidence",
                "dispatcher_cause_proven": False})
        return {"run_id": run_id, "count": len(rows), "truncated": len(conflicts) > len(rows), "conflicts": rows}

    def feasibility(self, run_id: str, period_seconds: int | None = None) -> dict:
        root, run = self._run(run_id)
        trains = run.get("trains", [])
        if not trains:
            raise WorkspaceError("run has no train records")
        terminal_positive = [t for t in trains if (t.get("terminal_delay_seconds") or 0) > 0]
        events = self._events(run_id, root)
        intermediate_positive = []
        terminal_by_vehicle = {t.get("vehicle_id"): t.get("terminal_stop_id") for t in trains}
        for vehicle, items in events.items():
            for e in items:
                if (e.get("type") in ("VehicleArrivesAtFacility", "VehicleDepartsAtFacility")
                        and e.get("facility") != terminal_by_vehicle.get(vehicle)
                        and (self._number(e.get("delay")) or 0) > 0):
                    intermediate_positive.append({"train_id": vehicle, "time_seconds": e.get("_seconds"), "delay_seconds": self._number(e.get("delay")), "event_index": e.get("_index")})
        if period_seconds is not None and (isinstance(period_seconds, bool) or not isinstance(period_seconds, int) or not 1 <= period_seconds <= 172800):
            raise WorkspaceError("period_seconds must be an integer between 1 and 172800")
        groups = defaultdict(int)
        if period_seconds:
            for t in trains:
                groups[int(t["scheduled_departure"] // period_seconds)] += 1
        conflict_count = run.get("headway_conflict_count")
        if conflict_count is None or not isinstance(run.get("headway_conflicts"), list):
            raise WorkspaceError("run is missing headway-admissibility evidence")
        horizon = run.get("horizon_seconds")
        started = [t.get("scheduled_departure") for t in trains if t.get("scheduled_departure") is not None]
        completed = sum(bool(t.get("arrived")) for t in trains)
        boundary_rows = []
        if period_seconds and groups:
            by_period = defaultdict(list)
            for t in trains:
                departure = t.get("scheduled_departure")
                if departure is not None:
                    by_period[int(departure // period_seconds)].append(departure % period_seconds)
            period_ids = sorted(by_period)
            for index, period_id in enumerate(period_ids):
                next_id = period_ids[index + 1] if index + 1 < len(period_ids) else period_ids[0]
                distance = next_id - period_id if index + 1 < len(period_ids) else 1
                gap = distance * period_seconds + min(by_period[next_id]) - max(by_period[period_id])
                boundary_rows.append({"from_period": period_id, "to_period": next_id, "schedule_gap_seconds": gap,
                    "evidence": "scheduled departure times", "resource_headway_verified": False})
        last_arrivals = [t.get("actual_arrival") for t in trains if t.get("actual_arrival") is not None]
        last_completion = max(last_arrivals, default=None)
        return {"run_id": run_id, "period_seconds": period_seconds, "trains_expected": run.get("trains_expected"),
            "trains_completed": completed, "all_expected_trains_completed": completed == run.get("trains_expected") == len(trains),
            "no_positive_terminal_delay": not terminal_positive, "no_positive_intermediate_delay": not intermediate_positive,
            "delay_feasible": completed == run.get("trains_expected") == len(trains) and not terminal_positive and not intermediate_positive,
            "headway_admissible": conflict_count == 0, "headway_conflict_count": conflict_count,
            "positive_terminal_delay_trains": [t.get("vehicle_id") for t in terminal_positive], "positive_intermediate_delay_events": intermediate_positive[:self.MAX_LIMIT],
            "period_train_counts": dict(sorted(groups.items())), "warm_up_drain_evidence": {"horizon_seconds": horizon,
                "first_scheduled_departure_seconds": min(started) if started else None,
                "last_scheduled_departure_seconds": max(started) if started else None,
                "last_terminal_completion_seconds": last_completion,
                "drain_completed_within_horizon": last_completion is not None and horizon is not None and last_completion <= horizon and completed == len(trains),
                "warm_up_verified": False,
                "warm_up_limitation": "the retained run begins at its first scheduled departure; no pre-horizon warm-up is present in the event stream",
                "wraparound_schedule_gaps": boundary_rows,
                "wraparound_headway_verified": False,
                "wraparound_limitation": "schedule gaps are measured; circular resource headway needs an explicit repeated-period resource check"}}

    def compare(self, before_id: str, after_id: str, limit: int = 100) -> dict:
        self._check_limit(limit)
        _, before = self._run(before_id); _, after = self._run(after_id)
        left = {t.get("departure_id"): t for t in before.get("trains", [])}
        right = {t.get("departure_id"): t for t in after.get("trains", [])}
        changes = []
        for dep in sorted(left.keys() | right.keys()):
            a, b = left.get(dep), right.get(dep)
            if not a or not b or a.get("scheduled_departure") != b.get("scheduled_departure") or a.get("route_id") != b.get("route_id"):
                changes.append({"departure_id": dep, "before": {"departure_seconds": a.get("scheduled_departure"), "route_id": a.get("route_id")} if a else None,
                                "after": {"departure_seconds": b.get("scheduled_departure"), "route_id": b.get("route_id")} if b else None})
        regressions = []
        for dep in sorted(left.keys() & right.keys()):
            a, b = left[dep], right[dep]
            old, new = a.get("terminal_delay_seconds"), b.get("terminal_delay_seconds")
            if old is not None and new is not None and new > old:
                regressions.append({"departure_id": dep, "before_delay_seconds": old, "after_delay_seconds": new, "regression_seconds": new-old})
        before_order = [t.get("departure_id") for t in sorted(before.get("trains", []), key=lambda t: (t.get("scheduled_departure", math.inf), t.get("departure_id", "")))]
        after_order = [t.get("departure_id") for t in sorted(after.get("trains", []), key=lambda t: (t.get("scheduled_departure", math.inf), t.get("departure_id", "")))]
        before_rank = {departure: i for i, departure in enumerate(before_order)}
        after_rank = {departure: i for i, departure in enumerate(after_order)}
        reorders = [{"departure_id": dep, "before_position": before_rank[dep], "after_position": after_rank[dep]}
                    for dep in sorted(before_rank.keys() & after_rank.keys()) if before_rank[dep] != after_rank[dep]]
        hashes_before = before["input_file_sha256"]; hashes_after = after["input_file_sha256"]
        return {"before_run_id": before_id, "after_run_id": after_id,
            "source": {"before_input_sha256": before["input_sha256"], "after_input_sha256": after["input_sha256"],
                "before_event_file": f"runs/{before_id}/output/ITERS/it.0/{before_id}.0.events.xml.gz", "after_event_file": f"runs/{after_id}/output/ITERS/it.0/{after_id}.0.events.xml.gz"},
            "frozen_input_checks": {"before_valid": True, "after_valid": True, "unchanged_files": sorted(k for k in hashes_before.keys() & hashes_after.keys() if hashes_before[k] == hashes_after[k]),
                "changed_files": sorted(k for k in hashes_before.keys() | hashes_after.keys() if hashes_before.get(k) != hashes_after.get(k))},
            "changed_departures_and_order": {"changed_departures": changes[:limit], "before_order": before_order[:limit],
                "after_order": after_order[:limit], "reordered_departures": reorders[:limit]},
            "metrics": {"before": self._metrics(before), "after": self._metrics(after)},
            "train_level_regressions": regressions[:limit], "truncated": len(changes) > limit or len(regressions) > limit}

    @staticmethod
    def _metrics(run):
        return {k: run.get(k) for k in ("trains_expected", "trains_arrived", "trains_unfinished", "mean_terminal_delay_seconds", "p95_terminal_delay_seconds", "max_terminal_delay_seconds", "headway_conflict_count")}

    @classmethod
    def _check_limit(cls, limit):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= cls.MAX_LIMIT:
            raise WorkspaceError(f"limit must be an integer between 1 and {cls.MAX_LIMIT}")
