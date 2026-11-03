"""Bounded child processes shared by the capacity workflow."""

import os, signal, subprocess

SIM_JVM_OPTIONS = os.environ.get(
    "RAILSIM_SIM_JVM_OPTIONS", "-XX:TieredStopAtLevel=1 -XX:+UseSerialGC"
)


def bounded_process(command, timeout, **kwargs):
    """Kill the whole process group on timeout/cancellation, including Maven/Java."""
    input_text = kwargs.pop("input", None)
    p = subprocess.Popen(command, start_new_session=True, **kwargs)
    try:
        out, err = p.communicate(input=input_text, timeout=max(0.01, timeout))
        return subprocess.CompletedProcess(command, p.returncode, out, err)
    except BaseException:
        os.killpg(p.pid, signal.SIGTERM)
        try:
            p.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
        raise
