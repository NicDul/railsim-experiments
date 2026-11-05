# Local solver and simulator spot checks

`python -m railsim_mcp.spot_check` performs diagnostic sampling, not a flexibility
sweep or a proof that UNKNOWN cases are impossible. It never writes to the
source bundle. Use a read-only copy of completed `solver-results/<volume>/*.json`
records, `identity.json`, `frozen/manifest.json`, `calibration/reference.json`,
`calibration/origin.json`, and `calibration/workspace/`. Include
`server-execution-manifest.json` when records came from a server continuation.
Do not copy temporary records. Record the snapshot acquisition interval and
origin externally; the preparation timestamp is not the remote capture time.

```sh
uv run --extra solver python -m railsim_mcp.spot_check prepare \
  --source /absolute/snapshot --output /absolute/experiment \
  --origin 'remote completed v13/v14 snapshot, capture interval ...' \
  --volumes 13 14 --count 5 --seed 20261105 --seconds 600 --simulations 10
```

Preparation freezes sorted population IDs, record hashes, execution origins,
sampling seed, and immutable original samples before any computation. Insufficient
populations are errors, never grounds for handpicking replacements. Sampling is
without replacement within each group. Reusing the output requires identical
settings and intact frozen evidence. Original Mac and server records remain
distinguishable even when copied from one server bundle; volume 16 original Mac
records are labelled pilot records.

Use OR-Tools **9.15.6755**, the original model and compatible Java 25. The solver
and protected simulator source hashes must match the calibrated identity.
Build in your own checkout, never in the source run's checkout:
Set `JAVA_HOME` to your locally installed Java 25 runtime.

```sh
export JAVA_HOME=/path/to/jdk-25
export MAVEN_OPTS='-XX:TieredStopAtLevel=1 -XX:+UseSerialGC -XX:ActiveProcessorCount=2 -Xmx2g'
./mvnw -q -DskipTests compile dependency:build-classpath \
  -Dmdep.outputFile=/absolute/classpath.txt
uv run --extra solver python -m railsim_mcp.spot_check run \
  --output /absolute/experiment --project "$PWD" \
  --classpath-file /absolute/classpath.txt
```

All compute is sequential under the same resource allowance: one CP-SAT worker
or one JVM (cap its available processors as above), never both concurrently.
There is one long gap1 attempt, with the original ordering-derived seed.
Only genuine gap1 INFEASIBLE enables gap0, using the remaining per-order wall
allowance and original gap0 seed. Model construction/checkpoint overhead is
included in actual measured attempt time; CP-SAT limits its own solve phase.
UNKNOWN stops the ordering unresolved. Independent arithmetic checks validate
all witness assignments, exact cyclic entry phases and periodic resource holds.

The original random feasible sample is replayed first, unchanged. Newly resolved
UNKNOWN witnesses form a separate additional set. Every simulation has a
120-second JVM execution bound, consumes a persisted call reservation, and
retains immutable inputs, raw events, scores and hashes. Acceptance requires
zero raw scored terminal delay, no detours, exact cyclic entry order, selected
routes/platforms, no rerouting markers, and all six repeated sets arrived on
their selected routes. Rejected runs and execution errors are retained.

Create `STOP` **inside the experiment output** for a clean stop after the current
bounded action; remove only that experiment's marker and rerun to resume.
Completed cases are skipped. Interrupted solver or simulator reservations remain
interrupted, consuming their allowance: resume never silently spends another
budget or changes the sample. Use a new explicitly documented experiment if an
interrupted case needs another allowance. An advisory lock excludes simultaneous
writers. `report` regenerates `report.json` and `report.md` without computation.
Per-case failures appear in `error.json` and on stderr; `run` returns nonzero.

Interpret results as conditional fixed-profile evidence only. Server/Mac
hardware/runtime differences confound claims that time alone caused a changed
outcome. A longer-budget diagnostic does not change any active sweep timeout.

Keep personal remote-access configuration, hostnames, account names, absolute
machine paths and server launch helpers outside the repository. Public examples
use generic provenance; detailed execution provenance and diagnostic outputs
belong in private local experiment storage, not in git.
