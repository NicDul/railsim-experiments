| UC1 flexibility: mode x configured volume (trains/hour total) | BB1 | BB2 | BB3 |
| --- | ---: | ---: | ---: |
| MAINLINE/FV_STOP @ 4 trains / 1800 s (8.0/hour) | 100.0% estimate [15.8-100.0%, 95% CI] | 100.0% estimate [15.8-100.0%, 95% CI] | 100.0% estimate [15.8-100.0%, 95% CI] |
| MAINLINE/FV_STOP @ 5 trains / 1800 s (10.0/hour) | 100.0% estimate [15.8-100.0%, 95% CI] | 100.0% estimate [15.8-100.0%, 95% CI] | 100.0% estimate [15.8-100.0%, 95% CI] |
| MAINLINE/FV_STOP @ 30 trains / 1800 s (60.0/hour) | 0.0% exact, conditional | 0.0% exact, conditional | 0.0% exact, conditional |
| METRO/RV_STOP @ 4 trains / 1800 s (8.0/hour) | 100.0% exact | - | - |

| UC2 flexibility: mode x volume per direction (trains/hour total) | BB1 |
| --- | ---: |
| METRO/BALANCED @ 4 per direction / 1800 s (32.0/hour total) | 100.0% exact |
| METRO/BALANCED @ 6 per direction / 1800 s (48.0/hour total) | 100.0% estimate [15.8-100.0%, 95% CI] |

## Detailed counts and provenance

The denominator is the exact number of distinct cyclic entry-order combinations. “Works” reports validation draws and distinct validated orderings separately because simulator-validation draws are with replacement. Unresolved counts are `solver UNKNOWN/unresolved / unresolved validation draws / solver-feasible but unvalidated orderings`.

| UC | Mode | Block | Configured volume and rate | Distinct orders | Works (draws / unique) | Conditional no | Unresolved | Solver coverage | Simulator validation coverage | Classification method |
| --- | --- | --- | --- | ---: | ---: | ---: | --- | ---: | --- | --- |
| UC1 | MAINLINE/FV_STOP | BB1 | 4 / period; 8.0 trains/hour | 3 | 2 / 2 | 0 | 0 / 0 / 1 | 100% | 2 draws; 2/3 unique feasible orders (66.7%) | Exact ordering enumeration; uniform simulator-validation sample |
| UC1 | MAINLINE/FV_STOP | BB2 | 4 / period; 8.0 trains/hour | 3 | 2 / 2 | 0 | 0 / 0 / 1 | 100% | 2 draws; 2/3 unique feasible orders (66.7%) | Exact ordering enumeration; uniform simulator-validation sample |
| UC1 | MAINLINE/FV_STOP | BB3 | 4 / period; 8.0 trains/hour | 3 | 2 / 2 | 0 | 0 / 0 / 1 | 100% | 2 draws; 2/3 unique feasible orders (66.7%) | Exact ordering enumeration; uniform simulator-validation sample |
| UC1 | MAINLINE/FV_STOP | BB1 | 5 / period; 10.0 trains/hour | 6 | 2 / 2 | 0 | 0 / 0 / 4 | 100% | 2 draws; 2/6 unique feasible orders (33.3%) | Exact ordering enumeration; uniform simulator-validation sample |
| UC1 | MAINLINE/FV_STOP | BB2 | 5 / period; 10.0 trains/hour | 6 | 2 / 2 | 0 | 0 / 0 / 4 | 100% | 2 draws; 2/6 unique feasible orders (33.3%) | Exact ordering enumeration; uniform simulator-validation sample |
| UC1 | MAINLINE/FV_STOP | BB3 | 5 / period; 10.0 trains/hour | 6 | 2 / 2 | 0 | 0 / 0 / 4 | 100% | 2 draws; 2/6 unique feasible orders (33.3%) | Exact ordering enumeration; uniform simulator-validation sample |
| UC1 | MAINLINE/FV_STOP | BB1 | 30 / period; 60.0 trains/hour | 53,522,021,856 | 0 / 0 | 53,522,021,856 | 0 / 0 / 0 | 100% | Not needed; sound resource-occupation rejection covers the denominator | Whole-composition necessary-condition rejection |
| UC1 | MAINLINE/FV_STOP | BB2 | 30 / period; 60.0 trains/hour | 53,522,021,856 | 0 / 0 | 53,522,021,856 | 0 / 0 / 0 | 100% | Not needed; sound resource-occupation rejection covers the denominator | Whole-composition necessary-condition rejection |
| UC1 | MAINLINE/FV_STOP | BB3 | 30 / period; 60.0 trains/hour | 53,522,021,856 | 0 / 0 | 53,522,021,856 | 0 / 0 / 0 | 100% | Not needed; sound resource-occupation rejection covers the denominator | Whole-composition necessary-condition rejection |
| UC1 | METRO/RV_STOP | BB1 | 4 / period; 8.0 trains/hour | 1 | 1 / 1 | 0 | 0 / 0 / 0 | 100% | 1/1 ordering validated (100%) | Exact ordering enumeration and simulator validation |
| UC2 | METRO/BALANCED | BB1 | 4 per direction; 32.0 trains/hour total | 2 | 2 / 2 | 0 | 0 / 0 / 0 | 100% | 2/2 unique feasible orders validated (100%) | Exact product of independent entry-order enumerations |
| UC2 | METRO/BALANCED | BB1 | 6 per direction; 48.0 trains/hour total | 4 | 2 / 1 | 0 | 0 / 0 / 3 | 100% | 2 draws; 1/4 unique feasible orders (25%) | Exact ordering enumeration; uniform simulator-validation sample |

The UC1 MAINLINE/FV_STOP volumes 4 and 5, and UC2 METRO/BALANCED volume 6, have exact solver classification but sampled simulator validation. The validation draws use the configured seed `4711` and sample count `2`; their point estimates are not exact percentages. The intervals are two-sided 95% Clopper-Pearson intervals over independent uniform draws with replacement. Three solver-feasible UC1 orders remain unvalidated at volume 4 across each block; four remain unvalidated at volume 5. UC2 volume 6 has three solver-feasible orders not validated. These are unresolved, not no results.

At UC2 volume 4, the three used physical entries have separate cyclic sequences: `f04` contains `RV_AB` and `RV_AC`, `r07` contains `RV_BA`, and `r08` contains `RV_CA`. The joint denominator is the product of those per-entry necklace counts. Relative timing between entries, internal routes and platforms, overtaking, and exit order remain free.

At UC1 MAINLINE/FV_STOP volume 30, the exact denominator is covered by a sound resource-occupation necessary-condition rejection before per-order solving or simulation. This is a conditional no under the existing fixed-profile capacity-model assumptions, not an unrestricted claim of physical infeasibility. No result here changes the separate capacity frontier or promotes finite simulator evidence into an exhaustive capacity claim.

## Acceptance provenance

Acceptance used the checked-in configurations under `examples/flexibility/` and Java 25. Final reports and resumable state were written outside the checkout under `/tmp/railsim-flexibility-acceptance-20261104/verified-*`. The five final runs covered UC1 MAINLINE/FV_STOP on BB1-BB3, UC1 METRO/RV_STOP on BB1, and UC2 METRO/BALANCED on BB1. Together they used 138.75 CPU seconds, 115.57 wall seconds, 40 simulator calls, and zero model calls.

Across all acceptance attempts, including reruns and failures, the external run ledger records 449.47 CPU seconds and 384.73 wall seconds. The report/checkpoint budgets record 127 simulator calls and zero model calls. The aggregate simulator-call ceiling of 120 was exceeded by 7 calls during repeated acceptance attempts; this is reported explicitly rather than omitting earlier or failed attempts. One Java-selection smoke attempt exited before producing a flexibility report. A separate UC2 run completed its simulator work but hit a report-finalization `NameError`; the corrected implementation was rerun successfully in the final five-run set.

The complete Python suite passed: 102 tests, 10 skipped. The focused flexibility suite passed 14 tests. Existing capacity behavior is covered by the full suite's capacity regression tests.
