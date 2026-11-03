# UC1 capacity limits: bounds and proofs

Everything here is **fixed-profile**: each train type and platform occupies each
resource (entry track, switches, platform, exit track) for a measured, constant
interval taken from an isolated simulation, there is no holding, no automatic
rerouting and a 0 s extra gap. The limits are therefore conditional on those
profiles; they are not proofs about every movement the simulator could produce.
Working rates were validated by zero-delay simulator runs. All checks are local
CP-SAT or plain arithmetic; no model API is involved.

## Result table (trains per hour, repeating 30 minute period)

S = direct CP-SAT/solver exclusion of the next rate (or exhaustive type-order
check) plus a working timetable. W = sliding-window bound (`window-bounds/`,
`../scripts/window_bound_uc1.py`) plus a working timetable. E = the
entry/exit-order argument and exhaustive certificate check (`bb3-fv-stop/`).

| Mode | BB1 | BB2 | BB3 |
| --- | ---: | ---: | ---: |
| MAINLINE / FV_PASS | 26 S | 36 W | 40 W |
| MAINLINE / FV_STOP | 14 S | 26 S | 32 E |
| TRANSIT / FV_STOP | 16 S | 32 S | 32 S |
| EXPRESS / FV_STOP | 10 S | 20 S | 30 S |
| METRO / RV_STOP | 16 S | 32 S | 42 S |

## Reproduce

Requires Python >= 3.11, `uv`, a C++17 compiler.

```sh
# BB3 MAINLINE/FV_STOP: 34/hour impossible (about 80 s in total)
cd proofs/bb3-fv-stop
c++ -O3 -std=c++17 enumerate_orders.cpp -o enumerate_orders
./enumerate_orders surviving-type-orders.txt order-certificates.tsv
uv run --with ortools==9.15.6755 python audit_certificates.py
uv run --with ortools==9.15.6755 python check_survivors.py
uv run --with ortools==9.15.6755 python independent_full_checks.py
uv run --with ortools==9.15.6755 python verify_delivery.py

# BB3 sliding-window bounds
cd ../window-bounds
uv run --with ortools==9.15.6755 python check_remaining.py --mode fv_pass --rates 42
uv run --with ortools==9.15.6755 python check_remaining.py --mode fv_stop --rates 38 36
```

`proofs/bb3-fv-stop/PROOF.md` explains the BB3 FV_STOP argument step by step.
