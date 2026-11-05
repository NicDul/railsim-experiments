"""Thin local CLI; model calls are opt-in, outputs and state external."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
from .capacity import Workflow, digest, save, export_existing
from .configuration import Settings
from .runtime import SIM_JVM_OPTIONS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--replay-witness",
        type=Path,
        help="validate a supplied witness instead of searching",
    )
    parser.add_argument(
        "--export-only",
        action="store_true",
        help="repackage an existing checkpoint without rerunning/resuming computation",
    )
    parser.add_argument(
        "--volume-range",
        type=int,
        nargs=3,
        metavar=("FIRST", "LAST", "STEP"),
        help="explicit inclusive sweep domain; overrides config volumes",
    )
    args = parser.parse_args()
    try:
        settings = Settings.load(args.project, args.state_root)
        config = json.loads(args.config.read_text())
        if args.volume_range:
            config.pop("volumes", None)
            config["volume_range"] = args.volume_range
        args.output.mkdir(parents=True, exist_ok=True)
        with (args.output / ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if config.get("runner") == "fixture":
                fixture = args.config.parent / config.pop("fixture")
                data = json.loads(fixture.read_text())
                config["fixture_sha256"] = digest(fixture)
                save(args.output / "model-fixture.json", data)
            os.environ["MAVEN_OPTS"] = (
                os.environ.get("MAVEN_OPTS", "") + " " + SIM_JVM_OPTIONS
            ).strip()
            if args.export_only:
                result = export_existing(settings, config, args.output)["bounds"]
                print(json.dumps(result, indent=2))
                return
            workflow = Workflow(
                settings,
                config,
                args.output,
                [sys.executable, "-m", "railsim_mcp.cli", *sys.argv[1:]],
            )
            if args.replay_witness:
                from .replay import replay

                result = replay(workflow, args.replay_witness)
            else:
                result = workflow.run()["bounds"]
        print(json.dumps(result, indent=2))
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(2, f"capacity: {exc}\n")


if __name__ == "__main__":
    main()
