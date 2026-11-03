"""Checkpointed UC1 orchestration. Search failures never certify simulator capacity."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from .configuration import Settings
from .workspace import ScenarioWorkspace
from .simulation import add_platform_variants, calibrate, simulate
from .profiles import reference_windows
from .solver import solve
from .timetable import replace_timetable
from .runtime import SIM_JVM_OPTIONS, bounded_process
from .uc1_modes import UC1Mode, rounded_counts


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, data):
    path = Path(path)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
    temp.replace(path)


def validate_config(config):
    if config.get("building_block") not in ("BB1", "BB2", "BB3"):
        raise ValueError("only existing UC1 BB1/BB2/BB3 family supported")
    for name, low, high, default in [
        ("period_seconds", 600, 24000, 1800),
        ("seed", 0, 2147483647, 4711),
        ("workers", 1, 32, 1),
        ("rounds", 0, 100, 2),
        ("proposals", 1, 20, 5),
    ]:
        value = config.setdefault(name, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be an integer in {low}..{high}")
    for name, default in [("solver_seconds", 10), ("model_seconds", 120)]:
        value = config.setdefault(name, default)
        if type(value) not in (int, float) or not 0 < value <= 600:
            raise ValueError(f"{name} must be in (0,600]")
    volumes = config.get("volumes")
    if (
        not isinstance(volumes, list)
        or not volumes
        or any(type(v) is not int or not 4 <= v <= 100 for v in volumes)
    ):
        raise ValueError("volumes must list integer trains per period in 4..100")
    config["volumes"] = sorted(set(volumes))
    if config.setdefault("runner", "none") not in ("none", "fixture", "codex"):
        raise ValueError("runner must be none, fixture or subscription codex")
    if config["runner"] == "codex" and not config.get("model"):
        raise ValueError("codex requires an explicit model")
    return config


def proposal(value, counts, options):
    if not isinstance(value, dict) or not isinstance(value.get("order"), str):
        raise ValueError("proposal requires an order string")
    order = value["order"].strip().upper()
    names = {p[0]: p for p in counts}
    if Counter(order) != Counter({p[0]: n for p, n in counts.items()}):
        raise ValueError("order does not match declared integer composition")
    platforms = value.get("platforms", [])
    if not isinstance(platforms, list):
        raise ValueError("platforms must be a list")
    platforms = platforms or ["any"] * len(order)
    if len(platforms) != len(order):
        raise ValueError("one platform per order position required")
    for letter, platform in zip(order, platforms):
        if not isinstance(platform, str) or (
            platform != "any"
            and not any(v.endswith("_" + platform) for v in options[names[letter]])
        ):
            raise ValueError("platform unavailable for product")
    rotations = [
        (order[i:] + order[:i], tuple(platforms[i:] + platforms[:i]))
        for i in range(len(order))
    ]
    key = json.dumps(min(rotations))
    return [names[c] for c in order], platforms, key


def occupation_exclusion(counts, options, windows, period):
    """Independent arithmetic lower bound on total exclusive occupancy per resource."""
    resources = sorted({r for w in windows.values() for r in w})
    loads = {
        r: sum(
            n
            * min(
                windows[v].get(r, (0, 0, 0))[2] - windows[v].get(r, (0, 0, 0))[1]
                for v in options[p]
            )
            for p, n in counts.items()
        )
        for r in resources
    }
    return dict(
        resource_minimum_seconds=loads,
        excluded_resources={r: n for r, n in loads.items() if n > period},
    )


def assemble(report):
    passing = [r for r in report["rates"] if r.get("witness")]
    best = max(passing, key=lambda r: r["volume"], default=None)
    exclusions = []
    for row in report["rates"]:
        if row.get("occupation", {}).get("excluded_resources"):
            exclusions.append(
                dict(
                    volume=row["volume"],
                    composition=row["composition"],
                    kind="resource_occupation",
                    evidence=row["occupation"],
                )
            )
        for trial in row["trials"]:
            if (
                trial.get("solver", {}).get("status") == "INFEASIBLE"
                and trial["scope"] == "all_orders"
            ):
                exclusions.append(
                    dict(
                        volume=row["volume"],
                        composition=row["composition"],
                        kind="fixed_profile_cp_sat",
                        trial=trial["id"],
                    )
                )
    return dict(
        simulator_lower_bound_trains_per_hour=best["trains_per_hour"] if best else None,
        best_witness=best["witness"] if best else None,
        fixed_profile_exclusions=exclusions,
        unresolved_volumes=[
            r["volume"] for r in report["rates"] if not r.get("witness")
        ],
        maximum=None,
        coverage="No coverage of all larger counts/compositions or unrestricted simulator trajectories. Search failures and order exclusions do not establish a maximum.",
    )


class Workflow:
    def __init__(
        self,
        settings,
        config,
        output,
        invocation,
        *,
        solver=solve,
        simulator=simulate,
        model=None,
    ):
        self.settings = settings
        self.config = validate_config(dict(config))
        self.out = Path(output).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        if self.out.is_relative_to(settings.project):
            raise ValueError("output must be outside simulator checkout")
        self.ws = ScenarioWorkspace(
            settings.project / "src/main/resources/scenarios",
            settings.state / "workspaces",
            project_root=settings.project,
            runs_root=settings.state / "runs",
        )
        self.mode = UC1Mode.load(
            settings.project
            / "src/main/resources/scenarios/use_case_1/uc1_operational_plan.json",
            self.config["mode"],
        )
        self.solver, self.simulator, self.model = solver, simulator, model
        self.invocation = invocation
        source = (
            settings.project
            / "src/main/resources/scenarios/use_case_1"
            / ("building_block_" + config["building_block"][-1])
        )
        self.inputs = {
            str(p.relative_to(settings.project)): digest(p)
            for p in source.iterdir()
            if p.is_file()
        }
        plan = (
            settings.project
            / "src/main/resources/scenarios/use_case_1/uc1_operational_plan.json"
        )
        self.inputs[str(plan.relative_to(settings.project))] = digest(plan)
        self.code = {
            str(p.relative_to(settings.project)): digest(p)
            for folder in ["src/main/java", "src/main/python"]
            for p in (settings.project / folder).rglob("*")
            if p.is_file() and p.suffix in (".py", ".java")
        }
        self.code["pom.xml"] = digest(settings.project / "pom.xml")
        self.identity = dict(
            config=self.config,
            inputs=self.inputs,
            code=self.code,
            mode=self.mode.record(),
            state=str(settings.state),
            runtime={
                key: os.environ.get(key)
                for key in [
                    "RAILSIM_DEADLOCK_AVOIDANCE",
                    "RAILSIM_SIM_JVM_OPTIONS",
                    "RAILSIM_MAVEN_REPO",
                    "MAVEN_OPTS",
                ]
            },
        )
        checkpoint = self.out / "checkpoint.json"
        if checkpoint.exists():
            self.report = json.loads(checkpoint.read_text())
            if self.report["identity"] != self.identity:
                raise ValueError(
                    "resume configuration, source/code hashes or state root changed; use a new output directory"
                )
            for row in self.report["rates"]:
                for trial in row["trials"]:
                    if trial["status"] == "running" or (
                        trial.get("solver", {}).get("pattern")
                        and not trial.get("simulation")
                    ):
                        trial["status"] = "interrupted"
                for record in row["rounds"]:
                    if record["status"] == "running":
                        record["status"] = "interrupted"
        else:
            self.report = dict(
                schema_version=1,
                identity=self.identity,
                invocation=invocation,
                calibration=None,
                rates=[],
                model_calls=0,
                assumptions=[
                    "UC1 fixed forward routes; one declared mix; binding platforms; entry/exit rerouting markers removed only in workspace.",
                    "Integer departures; exclusive isolated windows rounded outward to whole seconds; fixed-profile exclusions concern this surrogate only.",
                    "No holding/recovery in profile model. Simulator gate measures periods 2 and 3 of six repeats; raw positive terminal delay zero; no detours.",
                    "Finite seeded simulation witness, not proof of infinite-horizon feasibility. No simulator-learned constraints used as proof assumptions.",
                ],
            )
        self.persist()

    def persist(self):
        save(self.out / "checkpoint.json", self.report)

    def calibrate(self):
        ref = self.report["calibration"]
        if ref is None:
            workspace = "capacity-" + uuid.uuid4().hex[:12]
            root = self.ws.create(workspace, "UC1", self.config["building_block"])
            network = root / "network.xml"
            text = network.read_text()
            pattern = r'\s*<attribute name="railsim(Entry|Exit)" class="java.lang.Boolean">true</attribute>'
            if len(re.findall(pattern, text)) != 2:
                raise ValueError("expected one entry and exit marker")
            network.write_text(re.sub(pattern, "", text))
            variants = add_platform_variants(
                root / "schedule.xml", network, dict(self.mode.routes)
            )
            if set(v["product"] for v in variants.values()) != set(self.mode.shares):
                raise ValueError("missing product routes")
            ref = dict(workspace=workspace, variants=variants, baselines={})
            self.report["calibration"] = ref
            self.persist()
        root = self.ws.workspace_root / ref["workspace"]
        if ref.get("workspace_hashes"):
            if {
                p.name: digest(p)
                for p in root.iterdir()
                if p.suffix in (".xml", ".json")
            } != ref["workspace_hashes"]:
                raise ValueError("calibrated workspace changed")
        for relative, sha in ref.get("evidence_hashes", {}).items():
            if digest(self.ws.runs_root / relative) != sha:
                raise ValueError("calibration evidence changed")
        for variant, info in ref["variants"].items():
            if variant not in ref["baselines"]:
                ref["baselines"][variant] = calibrate(
                    self.ws,
                    ref["workspace"],
                    variant,
                    info["product"],
                    self.config["seed"],
                    self.config["period_seconds"],
                )
                if ref["baselines"][variant]["detours"]:
                    raise ValueError("isolated run rerouted")
                baseline_root = self.ws.runs_root / ref["baselines"][variant]["run_id"]
                ref.setdefault("evidence_hashes", {}).update(
                    {
                        str(p.relative_to(self.ws.runs_root)): digest(p)
                        for p in baseline_root.rglob("*")
                        if p.is_file()
                        and (
                            p.name == "run.json"
                            or p.suffix == ".gz"
                            or "inputs" in p.parts
                        )
                    }
                )
                self.persist()
        ref["windows"] = reference_windows(self.ws.runs_root, ref["baselines"])
        for windows in ref["windows"].values():
            for claim, grant, release in windows.values():
                if not claim <= grant < release:
                    raise ValueError("invalid isolated profile window")
        ref["workspace_hashes"] = {
            p.name: digest(p) for p in root.iterdir() if p.suffix in (".xml", ".json")
        }
        self.persist()
        return ref

    def ask(self, context, index):
        prompt = (
            'Propose cyclic entry orders for this UC1 fixed-profile timetable. Return JSON {"proposals":[{"order":"FRG...","platforms":[]}]}. '
            "Use exactly the counts, F=FV R=RV G=GV; platforms [] means solver choice. Propose diverse structures using feedback. "
            "No tools; model prose is not a certificate.\n"
            + json.dumps(context, indent=2)
        )
        prefix = self.out / f"model-{index:03}"
        prefix.with_suffix(".prompt.txt").write_text(prompt)
        if self.model:
            return self.model(context)
        if self.config["runner"] == "fixture":
            data = json.loads((self.out / "model-fixture.json").read_text())
            return data[index] if index < len(data) else {"proposals": []}
        if self.config["runner"] == "codex":
            from .model_runner import model_identity, request_action

            identity = model_identity("codex", self.config["model"])
            schema = dict(
                type="object",
                additionalProperties=False,
                required=["proposals"],
                properties=dict(
                    proposals=dict(
                        type="array",
                        items=dict(
                            type="object",
                            additionalProperties=False,
                            required=["order", "platforms"],
                            properties=dict(
                                order=dict(type="string"),
                                platforms=dict(type="array", items=dict(type="string")),
                            ),
                        ),
                    )
                ),
            )
            return request_action(
                identity,
                prompt,
                schema,
                prefix,
                self.config["model_seconds"],
                bounded_process,
                lambda a, s: a,
            )[0]
        return {"proposals": []}

    def trial(self, row, ref, options, products, platforms=None):
        trial = dict(
            id=len(row["trials"]),
            status="running",
            scope="one_order" if platforms is not None else "all_orders",
            products=products,
            platforms=platforms,
        )
        row["trials"].append(trial)
        self.persist()
        try:
            entry = None
            if platforms is not None:
                common = set.intersection(*(set(w) for w in ref["windows"].values()))
                candidates = [
                    r
                    for r in sorted(common)
                    if not r.startswith("platform")
                    and all(
                        ref["windows"][v][r][1]
                        <= min(
                            g for p, (_, g, _) in w.items() if p.startswith("platform")
                        )
                        for v, w in ref["windows"].items()
                    )
                ]
                if not candidates:
                    raise ValueError(
                        "ordered solver requires shared entry before platforms"
                    )
                entry = candidates[0]
            solved = self.solver(
                products,
                options,
                ref["windows"],
                self.config["solver_seconds"],
                frozenset(),
                self.config["workers"],
                0,
                period=self.config["period_seconds"],
                seed=self.config["seed"],
                order=products if platforms is not None else None,
                platforms=platforms,
                entry=entry,
            )
            trial["solver"] = solved
            trial["status"] = (
                "fixed_profile_infeasible"
                if solved["status"] == "INFEASIBLE"
                else "solver_" + solved["status"].lower()
            )
            self.persist()
            if solved["status"] in ("FEASIBLE", "OPTIMAL") and "pattern" in solved:
                result, run = self.simulator(
                    self.ws,
                    ref["workspace"],
                    solved["pattern"],
                    {v: b["terminal"] for v, b in ref["baselines"].items()},
                    self.config["seed"],
                    self.config["period_seconds"],
                )
                trial["simulation"] = result
                trial["status"] = (
                    "simulator_passed" if result["passed"] else "simulator_rejected"
                )
                if result["passed"]:
                    row["witness"] = dict(pattern=solved["pattern"], simulation=result)
        except (ValueError, RuntimeError, OSError) as exc:
            trial["status"] = "execution_failed"
            trial["error"] = str(exc)
        self.persist()

    def run(self):
        ref = self.calibrate()
        options = {}
        for variant, info in ref["variants"].items():
            options.setdefault(info["product"], []).append(variant)
        for volume in self.config["volumes"]:
            row = next((r for r in self.report["rates"] if r["volume"] == volume), None)
            if row is None:
                counts = {
                    p: n
                    for p, n in rounded_counts(volume, self.mode.shares).items()
                    if n
                }
                row = dict(
                    volume=volume,
                    trains_per_hour=3600 * volume / self.config["period_seconds"],
                    composition=counts,
                    trials=[],
                    rounds=[],
                    tried=[],
                    witness=None,
                    occupation=occupation_exclusion(
                        counts, options, ref["windows"], self.config["period_seconds"]
                    ),
                )
                self.report["rates"].append(row)
                self.persist()
            products = [p for p, n in row["composition"].items() for _ in range(n)]
            if not row["trials"]:
                self.trial(row, ref, options, products)
            excluded = any(
                t["scope"] == "all_orders"
                and t.get("solver", {}).get("status") == "INFEASIBLE"
                for t in row["trials"]
            )
            row["escalation_reason"] = (
                "full_model_infeasible_cannot_be_repaired_by_order"
                if excluded
                else "solver_or_simulator_unresolved"
                if not row["witness"]
                else "solver_first_pass_no_model_needed"
            )
            while (
                not excluded
                and not row["witness"]
                and len(row["rounds"]) < self.config["rounds"]
                and self.config["runner"] != "none"
            ):
                index = self.report["model_calls"]
                self.report["model_calls"] += 1
                record = dict(index=index, status="running", results=[])
                row["rounds"].append(record)
                self.persist()
                context = dict(
                    mode=self.mode.record(),
                    period_seconds=self.config["period_seconds"],
                    composition=row["composition"],
                    variants=ref["variants"],
                    profiles_seconds=ref["windows"],
                    feedback=row["trials"],
                    prior_rounds=row["rounds"][:-1],
                )
                try:
                    response = self.ask(context, index)
                    save(self.out / f"model-{index:03}.response.json", response)
                    proposals = response.get("proposals")
                    if not isinstance(proposals, list):
                        raise ValueError("response must contain proposals list")
                    for item in proposals[: self.config["proposals"]]:
                        try:
                            sequence, platforms, key = proposal(
                                item, row["composition"], options
                            )
                            if key in row["tried"]:
                                record["results"].append(
                                    dict(status="duplicate", proposal=item)
                                )
                                continue
                            row["tried"].append(key)
                            self.trial(row, ref, options, sequence, platforms)
                            record["results"].append(
                                dict(status="attempted", trial=row["trials"][-1]["id"])
                            )
                        except (ValueError, TypeError) as exc:
                            record["results"].append(
                                dict(
                                    status="invalid_proposal",
                                    error=str(exc),
                                    proposal=item,
                                )
                            )
                        self.persist()
                        if row["witness"]:
                            break
                    record["status"] = "completed"
                except (
                    ValueError,
                    RuntimeError,
                    OSError,
                    subprocess.TimeoutExpired,
                ) as exc:
                    record["status"] = (
                        "model_timeout"
                        if isinstance(exc, subprocess.TimeoutExpired)
                        else "model_failed"
                    )
                    record["error"] = str(exc)
                self.persist()
            row["status"] = (
                "simulator_feasible"
                if row["witness"]
                else "unresolved_budget_exhausted"
            )
            self.persist()
        self.report["bounds"] = assemble(self.report)
        self.persist()
        save(self.out / "bounds-report.json", self.report)
        self.export()
        return self.report

    def export(self):
        """Portable evidence; no external absolute paths required to read or check hashes."""
        handoff = self.out / "proof-handoff"
        handoff.mkdir(exist_ok=True)
        save(handoff / "report.json", self.report)
        ref = self.report["calibration"]
        save(handoff / "profiles.json", ref["windows"])
        runs = {b["run_id"] for b in ref["baselines"].values()}
        runs.update(
            t["simulation"]["run_id"]
            for r in self.report["rates"]
            for t in r["trials"]
            if t.get("simulation")
        )
        runs.update(r["simulation"]["run_id"] for r in self.report.get("replays", []))
        for run_id in sorted(runs):
            source = self.ws.runs_root / run_id
            dest = handoff / "runs" / run_id
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / "run.json", dest / "run.json")
            if (source / "inputs").exists():
                shutil.copytree(source / "inputs", dest / "inputs", dirs_exist_ok=True)
            for events in source.rglob("*.events.xml.gz"):
                shutil.copy2(events, dest / events.name)
        (handoff / "prompt.md").write_text(
            "# UC1 proof request\n\nResolve the unresolved rates in report.json, or identify precisely what remains unknown.\n"
            "Building block: "
            + self.config["building_block"]
            + "; mode: "
            + self.config["mode"]
            + "; period seconds: "
            + str(self.config["period_seconds"])
            + ".\n"
            "Start from intuition and a small example. profiles.json contains [earliest DLA claim, exclusive grant, exclusive release] in integer seconds after departure. "
            "These are outward-rounded isolated measurements, not an exact universal simulator model. All config, counts, budgets, seeds, hashes, solver statuses, failed trial diagnostics and best passing witnesses are in report.json. "
            "runs/ contains portable frozen inputs, simulator summaries and compressed raw events. manifest.json hashes every included file. Absolute paths in original summaries describe provenance only; use the adjacent files.\n\n"
            "Distinguish simulator witnesses, fixed-profile exclusions, one-order infeasibility, UNKNOWN/timeouts and exhausted search. "
            "Do not infer an upper bound from failure or a maximum from one rounded composition. A universal capacity claim needs a necessity argument connecting simulator trajectories to the model, and coverage of every larger admissible count/composition. "
            "Return conjectures separately from proofs. Supply a separately executable checker, explicit assumptions, units and positive controls. LLM prose alone is not a certificate.\n\n"
            "Bounds and unresolved question:\n```json\n"
            + json.dumps(self.report["bounds"], indent=2)
            + "\n```\n"
        )
        save(
            handoff / "manifest.json",
            {
                str(p.relative_to(handoff)): digest(p)
                for p in sorted(handoff.rglob("*"))
                if p.is_file() and p.name != "manifest.json"
            },
        )
