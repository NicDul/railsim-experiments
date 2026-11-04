"""Local entry-order flexibility analysis; outputs and simulator state stay external."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys

from .configuration import Settings
from .flexibility import FlexibilityWorkflow
from .runtime import SIM_JVM_OPTIONS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        settings = Settings.load(args.project, args.state_root)
        config = json.loads(args.config.read_text())
        args.output.mkdir(parents=True, exist_ok=True)
        with (args.output / ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.environ["MAVEN_OPTS"] = (
                os.environ.get("MAVEN_OPTS", "") + " " + SIM_JVM_OPTIONS
            ).strip()
            workflow = FlexibilityWorkflow(
                settings,
                config,
                args.output,
                [sys.executable, "-m", "railsim_mcp.flexibility_cli", *sys.argv[1:]],
            )
            result = workflow.run()
        print(json.dumps(result, indent=2))
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(2, f"flexibility: {exc}\n")


if __name__ == "__main__":
    main()
