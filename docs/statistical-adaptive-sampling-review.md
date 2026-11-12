# Statistical review: local BB3 adaptive-sampling pilot

**Verdict:** the proposed finite-checkpoint design gives at least 95%
simultaneous coverage, including optional stopping, subject to uniform iid
replacement draws and sound classifications for one unchanged fixed-profile
model. No complex anytime method is necessary. This is mathematical approval
of the design; the pinned implementation signoff is recorded below.
Neither is simulator validation.

## Target and endpoints

The four predeclared UC1/BB3 MAINLINE/FV_STOP points are 26/28/30/32 total
trains/hour: 13/14/15/16 trains per 1,800-second cycle, largest-remainder
40/40/20 mix. At point j, p_j is the feasible fraction of distinct cyclic
multiset orders; rotations are equivalent, and timing/platform choices remain
solver freedoms. A feasible witness and a conditional-infeasibility certificate
must address the same target model. Neither establishes simulator validity.

Use checkpoints n = 100, 200, 400, 800, 1600, 3200, 6400, M = 4, K = 7,
alpha = .05, and one-sided error a = alpha/(2*M*K) = 1/1120.
For 0 <= x <= n, define the one-sided exact binomial limits [1,2]:

- L(0,n) = 0; otherwise L(x,n) solves P(Bin(n,L) >= x) = a.
- H(n,n) = 1; otherwise H(x,n) solves P(Bin(n,H) <= x) = a.

Equivalently, L = BetaQuantile(a; x,n-x+1) for x > 0, and
H = BetaUpperQuantile(a; x+1,n-x) for x < n. In particular,
L(n,n) = exp(log(a)/n) and H(0,n) = -expm1(log(a)/n).
Report **[L(F,n), H(F+U,n)]**, where F and U count definite-feasible and
UNKNOWN *draw occurrences*, not unique cached orders. A two-sided equal-tail
Clopper-Pearson helper must receive alpha = 2*a = 1/560.

## Why optional stopping and adaptive retries are valid

Let Y_i be the fixed latent feasibility label of iid uniform order draw i and
T_n = sum(Y_i). Then T_n is Bin(n,p_j). Sound partial classifications imply
F <= T_n <= F+U pathwise. Exact limits are nondecreasing in their success
count, so L(F,n) <= L(T_n,n) and H(F+U,n) >= H(T_n,n).
Each endpoint therefore fails with probability at most a. The union bound
over 2*M*K = 56 endpoint events is at most .05; no independence between
points or checkpoints is required. On the simultaneous event, the interval
selected at any declared completed checkpoint contains its target.

Retries and sound cached-label upgrades may depend on previous outcomes:
the proof uses iid *latent draws*, not iid observed classifier labels.
Preserve repeated draw multiplicities and certificate/model identity.
The existing orbit rejection is correct: an orbit of size d has d times
one linear-arrangement probability, then acceptance probability 1/d.
Conditioning on acceptance makes every cyclic orbit equally likely.

Do not deduplicate statistical draws, select fastest completions, inspect
undeclared sample counts, or treat an incomplete batch as a completed
checkpoint. Stop at the first completed checkpoint with unrounded total
width <= .10. A budget exit retains only the last complete scheduled
interval and reports `precision_not_reached` unless its criterion was met;
before the first checkpoint there is no nontrivial certified interval.
Do not silently reallocate error after dropping a point or extending the grid.

## Numerical blocker and independent checks

At baseline revision `aa035fc4a3b9cce0142ac076ca1ae87c20242fb8`,
`flexibility.py` evaluates raw binomial coefficients times floating powers
and computes a survival tail as 1-CDF. Extracting only its interval function
and calling `clopper_pearson(n//2,n,alpha=1/560)` reproduced
`OverflowError: int too large to convert to float` at n = 1600 and 6400.
That implementation cannot be reused unchanged.

Use vetted beta/tail routines, or stable log-domain/scaled tail summation
with bracketed inversion; avoid enormous float binomial coefficients and
subtraction of nearly equal probabilities [2,3]. Treat p = 0/1 explicitly,
use `log1p`/`expm1` near boundaries, retain outward root brackets, and do not
round endpoints before the stopping decision.

Independent checks used installed R 4.5.2 `qbeta` lower/upper tails, compared
against an original Python 80-digit Decimal direct binomial-tail recurrence:
73 endpoint pairs across all seven checkpoints: unique sorted success counts
0, 1, 2, round(n*r) for r in {.01,.15,.50,.85,.99}, n-2, n-1, n.
Maximum absolute tail
residual was 1.265e-13 (required <= 2e-12). Boundary closed forms agreed
within 1e-15. R checks of every success count at n = 385, 800, 986, 987,
1600, 6400 confirmed monotonicity, ordering and reflection symmetry.
These are design-reference checks; replacement-code checks are recorded below.

Selected reference intervals (a = 1/1120):

| n | x | L(x,n) | H(x,n) |
|---:|---:|---:|---:|
| 100 | 0 | 0 | .067802744952 |
| 100 | 50 | .343250174343 | .656749825657 |
| 6400 | 1 | .000000139571 | .001461378733 |
| 6400 | 3200 | .480406818243 | .519593181757 |
| 6400 | 6400 | .998903557164 | 1 |

## Precision, UNKNOWN and census comparisons

Ten percentage points is **total absolute width**, not relative precision.
The familiar fixed-sample 385 is a normal approximation for one unadjusted
95% interval near p = .5. Here its exact corrected central width is .160646;
at n = 800 it is .111310, and at 1600 it is .078585.
With fully resolved labels, checking every success count at 1600 confirmed
that .078585 is the maximum: this checkpoint suffices regardless of rate.

For orientation only, nearest-integer counts at the supplied frozen census
label rates give the following first *scheduled* widths <= .10. These are
not expected stopping times, coverage evidence, or inputs to sampling/stopping.

| Total/hour | Supplied census target bracket | Illustrative n | Width |
|---:|---:|---:|---:|
| 26 | 4733/5544 = .853715729 | 800 | .079155 |
| 28 | [5528,5532]/12012 = [.460206460,.460539461] | 1600 | .078961 |
| 30 | [4142,4299]/28032 = [.147759703,.153360445] | 800 | .086292 |
| 32 | [824,964]/60060 = [.013719614,.016050616] | 200 | .063447 |

All four are plausibly compatible with the sample cap if classification
quality resembles these supplied records, but draw counts are not a wall/CPU
budget guarantee. UNKNOWN yields width >= U/n and a persistent unresolved
fraction is an asymptotic precision floor. Even 400 UNKNOWN among 6400 draws
(F = 3000) gives width .101543; all UNKNOWN gives [0,1].
Rare-event samples can stop early (zero successes at 100 already gives
width .067803) without establishing a precise relative percentage or absence
of feasible orders. No precision guarantee survives arbitrary UNKNOWN rates
or an early resource limit.

The last three census brackets are deterministic uncertainty intervals, not
known true p values. A single observed containment establishes no 95%
coverage. Cheap many-seed frozen-record replay may separately report coverage
of the fixed feasible-label rate, UNKNOWN-label rate, and upper-label
(feasible+UNKNOWN) rate; marginal diagnostics need their own error accounting
before any joint-confidence claim. For a fixed sound record map, this
two-endpoint construction also simultaneously contains the census bracket
with at least 95% family confidence. For evolving retry labels, the latent-p
proof remains valid, but that fixed-label interpretation need not.
Intersection with a census bracket, full bracket containment, and actual
latent-p coverage are different diagnostics. Never use census labels/rates
to steer the random order stream, retry selection or stopping rule.
Certificate/model errors are outside the statistical error budget.

## Sources and scope

1. Clopper and Pearson (1934), *The Use of Confidence or Fiducial Limits
   Illustrated in the Case of the Binomial*,
   [original DOI](https://doi.org/10.1093/biomet/26.4.404).
2. R official [`binom.test`](https://stat.ethz.ch/R-manual/R-devel/library/stats/html/binom.test.html)
   documents conservative exact coverage;
   [`Binomial`](https://stat.ethz.ch/R-manual/R-devel/library/stats/html/Binomial.html)
   documents beta-based tail evaluation.
3. SciPy official [`binom`](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.binom.html)
   documents direct survival tails and numerical accuracy; its
   [`proportion_ci`](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats._result_classes.BinomTestResult.proportion_ci.html)
   identifies exact Clopper-Pearson inversion.

No solver/simulator/server runs, implementation-source changes, dependency
installations or fresh saved-record replay were performed for this review.
Census totals above were supplied, not independently audited.

## Committed implementation signoff

**APPROVE**, bounded to implementation revision
`b11eb985a3e2178fd23e6aca345d1dcb046c063e`. No blocking statistical defect was
found in `adaptive_sampling.py` or its CLI plan/report wiring.

The replacement uses regularized-beta continued fractions and bracketed
inversion, explicit zero/all-success boundaries, and reflected upper limits.
The same 73 endpoint pairs were checked independently against R `qbeta`
and an 80-digit Decimal direct binomial-tail recurrence: maximum endpoint
deviation 7.08e-15, maximum absolute tail residual 8.42e-13, both below the
predeclared 2e-12 tolerance. Sampled-count monotonicity/reflection checks,
the UNKNOWN sandwich, and the 1/1120 allocation at every declared checkpoint
also passed. These are numerical checks, not a formal floating-point proof.

All **13 existing focused tests passed** from a temporary snapshot of that
exact committed tree. Because no solver run was permitted and OR-Tools was
not installed, only the solver import was replaced by a fail-closed sentinel;
the existing policy tests used their own mocks. No dependency was installed.
Additional independent in-memory-provider checks passed for reverse completion
order, an interrupted multi-point batch producing no premature interval,
resuming that batch, preserving all 20 repeated UNKNOWN draw occurrences,
interval [0,1] and `precision_not_reached` for all UNKNOWN, no repeated provider
calls after completion, and rejection of changed seed/checkpoints/alpha/M.
The temporary snapshot and all synthetic checkpoint files were removed.

Source inspection confirmed fixed iid prefix regeneration before evaluation,
deduplicated solver tasks but multiplicity-preserving summaries, a barrier
across every active point before checkpoint summaries, and persistence of
the last completed checkpoint on budget interruption. Resumes bind the plan,
provider and source identities. Saved-source conflicts are rejected; only
zero-gap INFEASIBLE is classified conditional no. The tiny fixed timing pilot
reports no confidence interval, including when partially completed.

This signoff relies on sound underlying solver classifications and unchanged
model identity; it does not independently check saved witnesses, prove
simulator equivalence, validate real saved-record replay or authorize a larger
fresh solve sweep. The coordinator retains execution authorization.
