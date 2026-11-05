"""Run one agent-selected action with an external wall bound and measured ledger."""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time


def child_cpu():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def stop_tree(process):
    pairs = [
        tuple(map(int, row.split()))
        for row in subprocess.check_output(
            ["ps", "-axo", "pid=,ppid="], text=True
        ).splitlines()
    ]
    ordered = [process.pid]
    seen = set(ordered)
    for parent in ordered:
        for pid, ppid in pairs:
            if ppid == parent and pid not in seen:
                ordered.append(pid)
                seen.add(pid)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in reversed(ordered):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        if sig == signal.SIGTERM:
            time.sleep(0.5)
    process.wait()


def run(command, seconds, ledger):
    ledger = Path(ledger).resolve()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    record = dict(
        command=command,
        working_directory=str(Path.cwd()),
        started_utc=datetime.now(timezone.utc).isoformat(),
        timeout_seconds=seconds,
        status="starting",
        stdout=str(ledger.with_suffix(".stdout.log")),
        stderr=str(ledger.with_suffix(".stderr.log")),
    )
    with ledger.open("x") as stream:
        json.dump(record, stream, indent=2)
    start, cpu = time.monotonic(), child_cpu()
    process = None
    try:
        with Path(record["stdout"]).open("x") as out, Path(record["stderr"]).open("x") as err:
            process = subprocess.Popen(
                command, stdout=out, stderr=err, start_new_session=True
            )
            try:
                record["returncode"] = process.wait(timeout=seconds)
                record["status"] = "completed" if process.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                stop_tree(process)
                record.update(status="timeout", returncode=124)
    except BaseException as exc:
        if process is not None and process.poll() is None:
            stop_tree(process)
        record.update(
            status="interrupted" if isinstance(exc, KeyboardInterrupt) else "launch_failed",
            returncode=130 if isinstance(exc, KeyboardInterrupt) else 127,
            error=f"{type(exc).__name__}: {exc}",
        )
        if not isinstance(exc, (OSError, KeyboardInterrupt)):
            raise
    finally:
        record.update(
            elapsed_wall_seconds=round(time.monotonic() - start, 6),
            completed_child_cpu_seconds=round(child_cpu() - cpu, 6),
            finished_utc=datetime.now(timezone.utc).isoformat(),
        )
        ledger.write_text(json.dumps(record, indent=2) + "\n")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not math.isfinite(args.seconds) or args.seconds <= 0 or not command:
        parser.error("positive finite --seconds and a command after -- are required")
    try:
        result = run(command, args.seconds, args.ledger)
    except OSError as exc:
        parser.exit(2, f"bounded action: {exc}\n")
    print(json.dumps(result, indent=2))
    return result["returncode"] if result["returncode"] >= 0 else 128 - result["returncode"]


if __name__ == "__main__":
    sys.exit(main())
