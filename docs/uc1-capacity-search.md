# UC1 capacity search: solver, model loop and proofs

This document explains how the UC1 building blocks (BB1-BB3) are searched for
the highest number of trains per hour that run with zero delay, and how the
limits are proved. For the numeric table and the proof scripts see
[`proofs/README.md`](../proofs/README.md).

## Pieces

| Piece | Where | Role |
| --- | --- | --- |
| Single-run bridge | `RunSingleSimulation.java`, subcommand `run-once` | Runs one explicit workspace timetable and writes terminal-delay evidence |
| Scenario workspace + MCP server | `src/main/python/railsim_mcp/` | Edit, freeze and analyse scenarios; `railsim-mcp` stdio server |
| Search harness | `railsim_mcp/search.py`, `uc1.py`, `uc1_modes.py` | UC1 modes, train-mix rounding, bounded model-driven timetable search |
| Model runners | `railsim_mcp/model_runner.py` | Codex CLI, Copilot CLI, OpenAI-compatible endpoint, Anthropic API; keys only from the environment |
| Periodic CP-SAT solvers | `scripts/solve_uc1_cpsat.py`, `solve_uc1_platforms.py`, `solve_uc1_binding.py` | Exact departure seconds and platforms from measured resource claims; every answer is checked by simulation |
| Hybrid loop | `scripts/order_uc1.py`, `scripts/hybrid_uc1.py` | A model proposes the entry order of train types, CP-SAT times it or proves it impossible and returns the closest miss |
| Bounds and proofs | `scripts/occupation_bound_uc1.py`, `scripts/window_bound_uc1.py`, `proofs/` | Necessary conditions that exclude rates |

## Running

```sh
python3 -m venv .venv && .venv/bin/pip install '.[solver,llm]'
export RAILSIM_PROJECT_ROOT=$PWD            # this checkout (needs ./mvnw and a populated ~/.m2)
export RAILSIM_STATE_ROOT=/path/outside/the/checkout
export PYTHONPATH=src/main/python:scripts
python scripts/solve_uc1_binding.py --building-block BB2 --mode MAINLINE/FV_PASS \
    --project "$RAILSIM_PROJECT_ROOT" --state-root "$RAILSIM_STATE_ROOT" --output-dir out/bb2
```

The state root must be outside the checkout. Outputs (runs, logs, models) are
not committed. Model-driven runs read keys from `RAILSIM_OPENAI_API_KEY` or the
provider CLI's own login; nothing is written to disk.

## Tests

```sh
cd src/test/python
RAILSIM_PROJECT_ROOT=$(cd ../../..; pwd) PYTHONPATH=../../main/python:. \
  python -m unittest discover -s . -t .      # needs mcp, pydantic, ortools, anthropic
```

Integration tests under `src/test/integration` run real simulations and are not
part of this set.

## Assumptions behind every limit

Fixed per-type and per-platform resource profiles measured in isolation, no
holding or rerouting, 0 s extra gap, a repeating 1,800 s period and a
largest-remainder train mix. A "limit" is a statement in this model; working
rates also have zero-delay simulator runs, the exclusions do not.
