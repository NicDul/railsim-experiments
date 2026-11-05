# Proof branch: prove or refute a scoped conjecture

Read [`proofs/README.md`](../../../../proofs/README.md) and the relevant argument before proposing new proof work. They contain stronger inherited fixed-profile results than a short local search may discover. Inventory which certificates are actually present: some described generated audits/models are absent until reproduced. Treat narrative claims as inherited, not freshly checked. A feasible control or simulator witness can refute an infeasibility conjecture only within matching assumptions; a simulation rejection proves no universal exclusion.

## Matching the existing BB3 FV_STOP certificate

[`PROOF.md`](../../../../proofs/bb3-fv-stop/PROOF.md) is specific to 1,800 seconds, 7 FV / 7 RV / 3 GV, all three binding platforms, half-open exclusive holds, zero additional gap, no holding/rerouting, measured integer profiles, and an entry/exit-order necessity argument. Its claim about all higher counts additionally needs the declared rounding/deletion argument; inspect that argument for the actual requested domain rather than infer monotonicity.

The search export stores `[claim, grant, release]` after isolated departure, whereas this proof's inputs store `[grant, release]` **relative to block_1 grant** for each variant. First compare all variants/resources under exactly this conversion, and retain the result/hashes:

```sh
uv run --extra solver python - "$ART/bb3-baseline/checkpoint.json" \
  proofs/bb3-fv-stop/inputs/fv_stop-profiles.json <<'PY'
import hashlib, json, sys
from pathlib import Path
report_path, proof_path = map(Path, sys.argv[1:])
report = json.loads(report_path.read_text())
config = report["identity"]["config"]
assert (config["use_case"], config["building_block"], config["mode"],
        config["period_seconds"]) == ("UC1", "BB3", "MAINLINE/FV_STOP", 1800)
windows = report["calibration"]["windows"]
normalized = {
    variant: {resource: [grant - holds["block_1"][1],
                          release - holds["block_1"][1]]
              for resource, (_, grant, release) in holds.items()}
    for variant, holds in windows.items()
}
assert normalized == json.loads(proof_path.read_text()), "profile/domain mismatch"
print(json.dumps({"profile_match": True,
                  "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
                  "proof_sha256": hashlib.sha256(proof_path.read_bytes()).hexdigest()}))
PY
```

Save stdout to a match record and also match scenario/runtime hashes, rounding, semantics and necessary-condition reasoning. Equality of profiles alone is not unrestricted simulator/model equivalence. If mismatch, report it and investigate; do not silently substitute historical profiles.

## Bounded independent checking, outside the checkout

Copy the package to `$ART/proof-check` before compiling/running: its scripts write adjacent outputs. Use the current project's locked OR-Tools via `.venv/bin/python`, not an unrequested dependency version or new provider. The general instructions in `proofs/README.md` default to concurrent solvers; for this small acceptance use sequential checking and bounded commands.

```sh
cp -R proofs/bb3-fv-stop "$ART/proof-check"
python3 .github/skills/capacity-research/scripts/run_bounded.py \
  --seconds 20 --ledger "$ART/proof-compile-execution.json" -- \
  c++ -O3 -std=c++17 "$ART/proof-check/enumerate_orders.cpp" \
  -o "$ART/proof-check/enumerate_orders"
python3 .github/skills/capacity-research/scripts/run_bounded.py \
  --seconds 30 --ledger "$ART/proof-enumerate-execution.json" -- \
  "$ART/proof-check/enumerate_orders" \
  "$ART/proof-check/surviving-type-orders.txt" \
  "$ART/proof-check/order-certificates.tsv"
python3 .github/skills/capacity-research/scripts/run_bounded.py \
  --seconds 30 --ledger "$ART/proof-audit-execution.json" -- \
  .venv/bin/python "$ART/proof-check/audit_certificates.py"
```

This independently audits enumeration coverage and positive contradictory cycle sums, but **does not discharge the 150 surviving orders**. To check a targeted survivor or positive control sequentially, import the copied `check_survivors.check(word, seconds=...)` or `independent_full_checks.check(word, period=..., seconds=...)` using a short script with the copied directory on `sys.path`, then execute it with `run_bounded.py`. Read their signatures and retained control inputs first. Account each check, allow an actual feasible control, and retain models, logs and statuses. To finish a complete certificate, every survivor needs a conclusive check and every required control/artifact must exist before `verify_delivery.py` can pass. A timeout or subset check leaves the rest inherited/unverified; report coverage exactly.

Use [`scripts/audit_capacity_export.py`](../../../../scripts/audit_capacity_export.py) for current raw witness/hash/occupation validation. It is independent of CP-SAT construction, but does not check infeasibility proofs. An agent may recompute `capacity.occupation_exclusion` arithmetically from the exported integer intervals and exact rounded counts; record each resource's minimum workload versus period and the variant-choice relaxation used. This bounds the surrogate, not all simulator trajectories.

For a new structural conjecture, write an independently runnable checker, explicit input hashes/assumptions, coverage argument and feasible controls within the allowed scratch artifacts. Use a local proof subagent only if available and useful, with a precise conjecture, allowance and instruction to seek counterexamples as well as a proof. A Project child needing a separate app proof session asks its coordinator to delegate. No autonomous proof daemon or hosted model is supplied by this repository skill. Stop at partial checked coverage if the remaining allowance cannot finish; request expansion rather than quietly launching exhaustive work.
