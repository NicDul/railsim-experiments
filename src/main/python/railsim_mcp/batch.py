"""Persistent, sequential Java execution over immutable ScenarioWorkspace snapshots."""

import json
import os
from pathlib import Path
import selectors
import shlex
import subprocess
import time

from .workspace import ScenarioWorkspace, WorkspaceError


class PersistentScenarioWorkspace(ScenarioWorkspace):
    def __init__(self, *args, classpath, **kwargs):
        super().__init__(*args, **kwargs)
        self.classpath = classpath
        self.process = None
        self.log = None
        self.buffer = b""

    def _start(self):
        if self.process is not None:
            if self.process.poll() is not None:
                raise WorkspaceError(f"persistent simulator exited: see {self.log.name}")
            return
        self.runs_root.parent.mkdir(parents=True, exist_ok=True)
        self.log = (self.runs_root.parent / "persistent-jvm.log").open("ab")
        java_home = os.environ.get("JAVA_HOME")
        java = str(Path(java_home) / "bin/java") if java_home else "java"
        environment = dict(os.environ, XDG_CONFIG_HOME=str(self.runs_root.parent / "config"))
        self.process = subprocess.Popen(
            [java, *shlex.split(os.environ.get("MAVEN_OPTS", "")),
             "-cp", self.classpath, "org.matsim.project.RunBatchSimulation"],
            cwd=self.project_root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.log, env=environment,
        )

    def _response(self):
        deadline = time.monotonic() + self.timeout_seconds
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b"\n" not in self.buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    self.close()
                    raise WorkspaceError("persistent simulation exceeded its configured execution bound")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise WorkspaceError(f"persistent simulator closed its response pipe: see {self.log.name}")
                self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def _execute_run(self, run_id, run_root, snapshot, output, plan_source, hashes,
                     aggregate_hash, seed, horizon_seconds):
        args = [
            "--workspace", str(snapshot), "--output", str(output),
            "--operational-plan", str(snapshot / plan_source.name),
            "--run-id", run_id, "--seed", str(seed),
            "--horizon-seconds", str(horizon_seconds),
        ]
        started = time.monotonic()
        try:
            self._start()
            request = json.dumps({"run_id": run_id, "args": args}).encode() + b"\n"
            self.process.stdin.write(request)
            self.process.stdin.flush()
            response = self._response()
            if response.get("run_id") != run_id or response.get("exit_code") != 0:
                raise WorkspaceError(f"persistent simulation failed: {response}; see {self.log.name}")
            result_path = output / "simulation.json"
            if not result_path.is_file():
                raise WorkspaceError("persistent simulator returned success without simulation evidence")
            result = json.loads(result_path.read_text())
            result.update({
                "input_sha256": aggregate_hash, "input_file_sha256": hashes,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "snapshot_directory": str(snapshot), "output_directory": str(output),
                "execution_backend": "persistent_jvm", "jvm_pid": self.process.pid,
            })
            (run_root / "run.json").write_text(json.dumps(result, indent=2) + "\n")
            return result
        except (OSError, json.JSONDecodeError) as exc:
            self.close()
            raise WorkspaceError(f"could not complete persistent simulation: {exc}") from exc

    def close(self):
        if self.process is not None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process.stdout.close()
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None
