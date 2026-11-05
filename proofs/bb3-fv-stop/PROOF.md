# BB3 MAINLINE/FV_STOP: the remaining capacity case is resolved

**The fixed-profile capacity on the tested 30-minute periodic count convention
is 32 trains/hour. A 34/hour timetable is impossible in this model.**

The result uses measured, binding-platform profiles, no rerouting or holding,
and zero additional resource gap. It is conditional on these fixed profiles.
Equivalence to every movement possible in the simulator has not been proved.
All computation was local; no model API or subscription model runner was called.

## 1. What had to be decided

At 34/hour the repeating 1,800-second period contains **7 FV, 7 RV and 3 GV**.
The earlier five-train window bound was inconclusive. An independent group
could choose timings incompatible with those chosen by a neighbouring group.
General searches then had to explore order, platforms and timings together.

The new approach first determines how the exit order must relate to the entry
order. That gives very cheap necessary timing checks for **every train-type
order**, leaving only 150 small complete platform/time problems.

## 2. The measured facts used in the structural argument

Times are offsets in seconds from entry to block_1, with half-open occupancy
intervals: a resource may be reused exactly when its preceding occupation ends.
The ranges below are extrema over the three platform variants of each type.
Using extrema can make a condition more permissive; it cannot remove a valid
actual timetable. `inputs/fv_stop-profiles.json` contains every resource interval.

| Train | Minimum entry-track occupation | Exit-track occupation begins | Exit-track occupation ends |
| --- | ---: | ---: | ---: |
| FV | 95 | 315–316 | 394–397 |
| RV | 85 | 207–208 | 269–271 |
| GV | 95 | 41–43 | 141–145 |

All trains use the same exclusive entry track and exclusive exit track. There
are three platforms, and the exact checks include all platform and switch
occupations as well as both tracks.

## 3. Only an immediately following GV can overtake an FV

Suppose A enters first and B enters delta seconds later. If B clears the exit
track before A starts occupying it, then

`delta <= exit_start(A) - exit_end(B)`.

The most permissive values over all platforms are:

- RV overtaking FV: `delta <= 316 - 269 = 47`. Impossible: entry gaps are at
  least 85 seconds, and after an FV at least 95 seconds.
- GV overtaking RV: `delta <= 208 - 141 = 67`. Also impossible.
- GV overtaking FV: `delta <= 316 - 141 = 175`. This is possible.
- Every other ordered pair has a negative maximum in this inequality, so it
  cannot overtake either. The verifier derives all nine values from the data.

If there is an intervening train between the FV and GV, their entry separation
is at least `95 + 85 = 180`, already greater than 175. Therefore **the only
possible overtaking is an adjacent FV–GV pair swapping order at the exit**.
This argument applies to cyclic adjacency across periods as well.

## 4. At this rate, every adjacent FV–GV pair must swap

The entry track alone must be occupied for at least

`7 × 95 + 7 × 85 + 3 × 95 = 1,545 seconds`.

That leaves at most `1,800 - 1,545 = 255 seconds` of extra entry spacing.

If an adjacent GV does not overtake its preceding FV, it can enter only once
it will reach the exit after the FV clears. Even with the most permissive
platform choices, their entry separation must then be at least

`394 - 43 = 351 seconds`.

The ordinary minimum gap after that FV was 95 seconds. This one gap therefore
adds at least `351 - 95 = 256 seconds` above the 1,545-second baseline:

`1,545 + 256 = 1,801 > 1,800`.

That is impossible. **Every adjacent FV–GV pair must be overtaken.** Together
with Section 3, this fixes the exit-type order: take the entry order and swap
every adjacent FG into GF. Those swaps do not overlap.

## 5. Cover every cyclic type order

Write R for RV, F for FV, and G for GV. Orders differing only by the choice of
where the repeating cycle begins describe the same cyclic order. The number
of distinct cyclic words with 7 R, 7 F and 3 G is exactly

`17! / (7! 7! 3! 17) = 137,280`.

All rotational orbits have length 17: the type counts have no common divisor,
so no word can consist of a shorter word repeated several times. The enumerator
starts the entry word at an R and retains its smallest rotation under R<F<G.
Starting at R avoids an FG swap crossing the chosen written boundary.

The independent audit checks that the certificate file contains exactly
137,280 distinct canonical words with the required counts. This establishes
coverage without trusting the enumerator's reported counter.

## 6. Reject almost all orders by necessary track inequalities

Let t_i be the entry time of train i. Consecutive entry trains require

`t_next >= t_current + minimum_entry_occupation(current)`.

For consecutive trains A and B in the now-known exit order, their exit-track
occupations require another inequality on their entry times:

`t_B >= t_A + minimum_exit_headway(A,B)`.

Subtract 1,800 on an inequality crossing the end of the written cycle. A
negative exit headway is meaningful because entry times and exit times differ:
for example, GV followed by FV at the exit allows the GV to enter after the FV.

The most permissive pair headways, derived over all platform pairs, are:

| First at exit / next at exit | RV | FV | GV |
| --- | ---: | ---: | ---: |
| RV | 61 | −47 | 226 |
| FV | 186 | 78 | 351 |
| GV | −67 | −175 | 98 |

There are only 34 inequalities per word: 17 entry and 17 exit. Some chains of
these inequalities return to their starting train and demand, for example,
`t_i >= t_i + 2`. Such a positive sum is an explicit contradiction.

**137,130 words have such a contradiction.** For each word, the package stores
the precise cycle of inequalities and its positive sum. A separate Python
verifier rebuilds the inequalities from the measured profiles and checks every
cycle by addition, without running the graph-search algorithm. The smallest
positive contradiction among these certificates is 2 seconds.

The remaining **150 words** pass these deliberately permissive track-only
conditions. Passing them establishes no timetable: platform and additional
switch constraints still have to be satisfied.

## 7. Check all 150 survivors exactly, twice

For each surviving entry-type word, the first CP-SAT model allows all three
platforms for every train and chooses the actual cyclic entry gaps. It includes
every measured resource interval and the derived necessary exit order.

**All 150 return INFEASIBLE.** None returns UNKNOWN. That batch took about
13 seconds on this host, with four independent single-worker solves at once.

A second formulation checks all 150 again. It:

- uses actual entry-time variables and Boolean platform alternatives;
- checks **every pair of trains and every common resource**, periodically;
- does not use the four-neighbour gap reduction, gap interval tables, or the
  derived exit-order constraints;
- uses only the obvious entry-track occupation lower bounds to tighten time
  domains and rule out impossible period shifts.

**All 150 again return INFEASIBLE.** This batch, including positive controls,
took about 69 seconds. Both formulations use OR-Tools 9.15.6755; their encodings
are independent, but this is not a claim to have used two independent solver
implementations. Both export the full models, responses and logs.

The second formulation accepts a 32/hour positive control and the diagnostic
17-train arrangement at a 1,810-second period. This checks that the model can
accept known feasible cases. The retained 32/hour simulator record also has
**96 of 96 trains arriving, maximum delay zero, and zero headway conflicts**.

An earlier weaker independent encoding reached time limits and was stopped
after the stronger independently encoded checks completed. Its UNKNOWN
outcomes are not used as exclusions and are not included in the proof package.

## 8. Conclusion and precise scope

Every possible 34/hour timetable induces one of the 137,280 cyclic type words.
For 137,130 words, necessary entry/exit inequalities contradict one another.
For every remaining word, both complete resource formulations establish
infeasibility with all platform choices free. Therefore no fixed-profile
34/hour timetable exists. A 32/hour timetable exists and passes the simulator.

Under the study's largest-remainder train-count convention, higher admitted
even rates have at least the same count of each type. Deleting added trains
from a hypothetical higher-rate timetable would leave a 34/hour timetable in
the same period. Thus the exclusion extends to those higher rates.

**The remaining fixed-profile limit is 32/hour.** This closes the last open
cell of the previously reported table. It does not prove that 1,810 seconds is
the global minimum period for 17 trains, nor an unconditional upper limit for
simulator behaviour outside the fixed-profile assumptions.

The capacity table in [`../README.md`](../README.md) lists all 15 cells and the method used for each.

## Reproduce and inspect

`order-certificates.tsv` is the arithmetic certificate for the track-only
eliminations. `certificate-audit.json` records independent coverage and cycle
verification. `survivor-results.json` and `independent-tight-results.json` contain
all 150 exact decisions and the positive controls. The `orders/` and
`independent-tight-orders/` directories contain full solver artifacts.

```sh
c++ -O3 -std=c++17 enumerate_orders.cpp -o enumerate_orders
./enumerate_orders surviving-type-orders.txt order-certificates.tsv
uv run --with ortools==9.15.6755 python audit_certificates.py
uv run --with ortools==9.15.6755 python check_survivors.py --seconds 5 --workers 4
uv run --with ortools==9.15.6755 python independent_full_checks.py
uv run --with ortools==9.15.6755 python verify_delivery.py
```

Solver scheduling can change runtime. A rerun with UNKNOWN is inconclusive;
the delivered runs have 150/150 INFEASIBLE in each complete formulation.
Generated files are git-ignored; `surviving-type-orders.txt` is kept as the
record of the 150 survivors, and the enumerator reproduces it byte for byte.
