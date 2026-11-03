"""Trusted launch configuration; tool clients cannot choose filesystem roots."""
from dataclasses import dataclass
import os
from pathlib import Path


def state_root() -> Path:
    return Path(os.environ.get("RAILSIM_STATE_ROOT", str(Path.home() / ".local/share/railsim-mcp"))).expanduser().resolve()


def project_root() -> Path:
    value = os.environ.get("RAILSIM_PROJECT_ROOT")
    if not value:
        raise ValueError("Set RAILSIM_PROJECT_ROOT to a Railsim checkout with the run-once Java runner")
    return Path(value).expanduser().resolve()


@dataclass(frozen=True)
class Settings:
    project: Path
    state: Path

    @classmethod
    def load(cls, project: str | None = None, state: str | None = None):
        root = Path(project).expanduser().resolve() if project else project_root()
        data = Path(state).expanduser().resolve() if state else state_root()
        for relative in ("mvnw", "pom.xml", "src/main/resources/scenarios",
                         "src/main/java/org/matsim/project/RunSingleSimulation.java"):
            if not (root / relative).exists():
                # Find the runner in its package (older compatible checkouts may place it elsewhere).
                if relative.endswith("RunSingleSimulation.java") and any((root / "src/main/java").rglob("RunSingleSimulation.java")):
                    continue
                raise ValueError(f"Incompatible Railsim checkout: missing {relative} in {root}")
        if data == root or data.is_relative_to(root):
            raise ValueError("RAILSIM_STATE_ROOT must be outside the simulator checkout")
        return cls(root, data)
