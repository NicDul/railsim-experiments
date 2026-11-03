"""Persistent run quotas, measured process/child CPU and active wall time."""

import resource
import time


class Exhausted(RuntimeError):
    pass


def cpu_seconds():
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return time.process_time() + children.ru_utime + children.ru_stime


class Budget:
    def __init__(self, limits, state, wall=time.monotonic, cpu=cpu_seconds):
        self.limits, self.state, self.wall, self.cpu = limits, state, wall, cpu
        self.wall_start, self.cpu_start = wall(), cpu()
        for key in ["wall_seconds", "cpu_seconds", "simulations", "model_calls"]:
            state.setdefault(key, 0)

    def refresh(self):
        wall, cpu = self.wall(), self.cpu()
        self.state["wall_seconds"] += max(0, wall - self.wall_start)
        self.state["cpu_seconds"] += max(0, cpu - self.cpu_start)
        self.wall_start, self.cpu_start = wall, cpu

    def allowance(self, kind, requested, workers=1):
        self.refresh()
        for key in ["wall_seconds", "cpu_seconds"]:
            if self.state[key] >= self.limits.get(key, float("inf")):
                raise Exhausted(key)
        counter = {"simulation": "simulations", "model": "model_calls"}.get(kind)
        if counter and self.state[counter] >= self.limits.get(counter, float("inf")):
            raise Exhausted(counter)
        allowance = min(
            requested,
            self.limits.get("wall_seconds", float("inf")) - self.state["wall_seconds"],
            (self.limits.get("cpu_seconds", float("inf")) - self.state["cpu_seconds"])
            / workers,
        )
        if allowance < 0.05:
            raise Exhausted("remaining execution time")
        if counter:
            self.state[counter] += 1
        return allowance
