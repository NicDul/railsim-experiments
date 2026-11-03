"""Run bounded real CLI examples; preserve logs, invocations, source revision and hashes."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=["uc1", "uc2"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    configs = sorted(
        (root / "examples/capacity").glob(
            "bb?.json" if args.suite == "uc1" else "uc2-*.json"
        )
    )
    args.output.mkdir(parents=True, exist_ok=True)
    record = dict(
        revision=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        suite=args.suite,
        runs=[],
    )
    for config in configs:
        out = args.output / config.stem
        command = [
            sys.executable,
            "-m",
            "railsim_mcp.cli",
            "--config",
            str(config),
            "--project",
            str(root),
            "--state-root",
            str(args.state_root / config.stem),
            "--output",
            str(out),
        ]
        row = dict(
            config=str(config.relative_to(root)),
            config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
            command=command,
        )
        record["runs"].append(row)
        (args.output / "execution.json").write_text(json.dumps(record, indent=2) + "\n")
        with (args.output / (config.stem + ".log")).open("w") as log:
            completed = subprocess.run(
                command, cwd=root, stdout=log, stderr=subprocess.STDOUT, timeout=1800
            )
        row["exit_code"] = completed.returncode
        if completed.returncode == 0:
            result = json.loads((out / "bounds-report.json").read_text())
            row["bounds"] = result["bounds"]
            row["solver_statuses"] = [
                t["solver"]["status"]
                for r in result["rates"]
                for t in r["trials"]
                if "solver" in t
            ]
            row["simulation_count"] = sum(
                "simulation" in t for r in result["rates"] for t in r["trials"]
            )
            row["calibration_count"] = len(result["calibration"]["baselines"])
            row["model_calls"] = result["model_calls"]
        (args.output / "execution.json").write_text(json.dumps(record, indent=2) + "\n")
        print(
            json.dumps(
                dict(
                    config=config.name,
                    **{
                        k: v
                        for k, v in row.items()
                        if k not in ["command", "config", "bounds"]
                    },
                )
            ),
            flush=True,
        )
    if any(r["exit_code"] for r in record["runs"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
