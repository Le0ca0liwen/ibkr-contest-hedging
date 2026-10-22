# Solver-speed study: report

Machine: i7-13700K (WSL2), Python 3.12.15, cvxpy 1.9.3, highspy 1.15.1, QuantLib 1.43. Timings: one solve at a time, pinned
(`taskset -c 4`), HiGHS `threads=1` (the app's setting), load average 1.0-1.6 during the final sweep (background WSL noise only),
`OMP/OPENBLAS/MKL_NUM_THREADS=1`. Base commit `ff47b13` (= app `c782c84` + research/). Raw data: `research/solverspeed/out/`.

## Findings

1. **HiGHS branch-and-bound dominates; CVXPY is 0.1-0.7 s.** Non-HiGHS time per solve: 0.11-0.75 s at 12-38 candidates, almost
   all stage-1 canonicalisation (CVXPY `cone_matrix_stuffing` + DCP/DPP tree walks, profile in `out/prof_*.txt`). Later stages
   re-canonicalise in ~1 ms. Model construction 9-23 ms, post-processing ~1 ms.
2. **Model sizes** (rows x cols, nnz, integer cols): n12 738x234, 15.4k, 38; n20 850x298, 24.6k, 62; n26 934x346, 31.4k, 80;
   n38 1102x442, 45.1k, 116. 275 stress rows in the model.
3. **The turnover stage almost always stops at the 1 s `ranking_seconds` cap** (0-few nodes, 2-41 % gap). The returned hedge is the
   incumbent of a wall-clock-limited stage, so the in-band choice is timing dependent in the *current* app too.
4. **Seed variance is large.** Same primary-stage LP, `random_seed` 1..7,17: n38 10:00 certifies in 5.9-18 s; n38 14:00 in
   10.7-19.2 s (never inside 10 s); n26 14:00 in 0.9-2.3 s. Capacity is heavy-tailed, not a smooth function of N.
5. **Scenario build** 0.22 s (n12) - 0.55 s (n38); ~88 % inside `ValuationEngine.price` (QuantLib object construction); NPV itself ~6 %.

## Prototype A - build the MILP without CVXPY, solve with highspy (`patches/A_direct_highspy.patch`)

`hedge/linmodel.py` (new, ~250 lines) is a small affine-expression layer; `_solve` is rewritten on it; stages use a fresh
`Highs()` with the same options and warm start as CVXPY's interface. It reproduces CVXPY's LP **bit for bit**: column order,
row order (equalities first), abs-epigraph rows, and constant offsets (accumulated like CVXPY's backend).

- **Equivalence evidence.** 48 seeded random instances (12/14/17/20 candidates, 7 clocks, spot/IV/spread perturbation, mandated and
  hedged books, 8 stock-only): every HiGHS model of every stage identical (`lp_identical` 48/48), statuses equal 48/48,
  primary incumbent and bound equal to 1e-6 relative 48/48, quantities identical 47/48. The one difference (seed 1029, 17 cand.,
  15:00) is the 1 s turnover cutoff: the prototype finished the stage (OPTIMAL), the baseline timed out; scores 932.17 vs 915.68,
  both inside the $20 band. Identical LPs also give identical branch-and-bound paths: n38 15:40 and n12 10:00 identical nodes and
  incumbents (n38 10:00 14:00 differ only in time-limit-dependent counts).
- **Tests.** Repo suite 560 passed, 2 skipped, 1 failed - the same failure as baseline (`test_dashboard_javascript_controls`
  needs Node.js, not installed here). Five tests that monkeypatched `cp.Problem.solve` were ported to the HiGHS wrapper.
- **Effect.** Non-HiGHS overhead falls to 10-20 ms. Wall saved per solve: 0.11-0.8 s (median 0.15 s at n12-17, 0.3-0.4 s at n20-26,
  0.8 s at n38 15:40). HiGHS time is unchanged, so n38 10:00/14:00 still sit at the 10 s limit.
- `min_variance` strategy still imports cvxpy for its OSQP baseline (rare, unchanged behaviour).

## Prototype B - keep CVXPY, reuse / options / warm start / deadline - evaluated, not recommended

- **HiGHS options** (`out/sweep.jsonl`, 8 configs x 8 LPs x 3 seeds, one config per physical core): no option is robustly better than
  the default. `mip_heuristic_effort` 0.02/0.3, `mip_detect_symmetry=false`, `mip_lp_age_limit=5`, `threads=4` are neutral or worse;
  `presolve=off` is much worse at n38; `mip_pscost_minreliable=2` is a modest win at n38 (29 vs 33 s summed medians) but is within seed noise
  and changes the search path (hence possibly the chosen hedge). Gap settings are already tied to the $20 band (`primary_band/4`).
- **Cross-solve warm start** from another clock's solution (`warm_cross.py`): mixed (0.65 s vs 1.24 s, 2.59 s vs 1.86 s,
  6.4 s vs 8.5 s) and at n38 10:00 it changed the returned incumbent (1934.8 vs 1937.2), so it can change the chosen hedge. Rejected.
- **Compiled-problem reuse** across solves would need the quotes/scenarios as CVXPY Parameters (big refactor); it could save at most
  the 0.1-0.7 s that A removes entirely. Not built.
- **Deadline guard before canonicalisation:** moot under A (no uninterruptible canonicalisation; the existing deadline checks cover
  the 10-20 ms of model building). On the CVXPY path the cost to guard is 0.7 s at n38 here (~2 s on a loaded VM).
- **Seed racing** (several HiGHS processes with different seeds, first certified wins): best-of-4 on n38 10:00 5.9 s vs 8.7 s, n38
  14:00 13.0 s vs 15.4 s, n26 14:00 1.0 vs 2.3 s. Helps the tail but makes the returned in-band hedge nondeterministic; not prototyped.

## Prototype C - cached QuantLib sessions (`patches/C_cached_quantlib.patch`)

`ValuationEngine.price` keeps one QuantLib option per (contract, valuation time) with mutable spot/vol/rate quotes; contracts with
cash dividends keep the original one-shot path (none in the contest manifest). 52 lines in `hedge/valuation.py`.

- **Equivalence:** 40 random builds (12-38 candidates, 7 clocks, spot x0.9-1.1, IV x0.8-1.3): `changes`, `stress`, `next_changes`,
  `overnight_changes`, `rhos` **bit-identical** (max relative difference 0.0), scenario `identifier` hash identical 40/40, rebuild stable.
  Repo suite 560 passed, same single Node-only failure; also with A+C combined.
- **Speed** (build median of 4 warm builds, pinned): n12 0.204 -> 0.077 s, n20 0.303 -> 0.121 s, n26 0.38 -> 0.157 s, n32 0.455 ->
  0.192 s, n38 0.52 -> 0.23 s (2.3-2.7x). Full table in `out/build_timing.json`.

## Baseline vs A: solve wall (median / max of 3, full mode; CPU = wall, single thread)

| cand | 10:00 base | 10:00 A | 14:00 base | 14:00 A | 15:40 base | 15:40 A |
|---|---|---|---|---|---|---|
| 12 | 1.28/1.29 | 1.15/1.15 | 1.55/1.56 | 1.41/1.42 | 1.33/1.35 | 1.17/1.17 |
| 14 | 1.08/1.09 | 0.93/0.93 | 1.50/1.51 | 1.34/1.35 | 1.52/1.52 | 1.37/1.38 |
| 17 | 1.44/1.46 | 1.22/1.22 | 1.65/1.66 | 1.43/1.44 | 1.56/1.57 | 1.36/1.36 |
| 20 | 1.21/1.22 | 0.94/0.95 | 1.69/1.72 | 1.43/1.43 | 2.99/2.99 | 2.71/2.74 |
| 26 | 2.49/2.51 | 2.10/2.10 | 3.75/3.80 | 3.36/3.37 | 2.71/2.72 | 2.29/2.31 |
| 38 | 10.02 OPT | 9.64 OPT | 10.02 USER_LIMIT | 10.01 USER_LIMIT | 3.70/3.72 | 2.88/2.92 |

Scenario build (base -> C): 0.21 -> 0.08 (n12), 0.31 -> 0.12 (n20), 0.39 -> 0.16 (n26), 0.55 -> 0.23 (n38). A solve + C build
together: n26 worst case 3.75+0.39 = 4.14 s -> 3.36+0.16 = 3.5 s; n38 14:00 remains a USER_LIMIT.

## Capacity (this machine, one core; `out/capacity_*.jsonl`, nested candidate sets 20..35 via SPLIT 23/29/32/35)

Full solve incl. the 1 s turnover stage, base -> A, worst of the 3 clocks: n23 3.7 -> 3.4 s, n26 3.8 -> 3.4 s, n29 2.6 -> 2.1 s,
n32 3.2 -> 2.7 s, n35 7.6 -> 6.8 s (14:00), **n38 USER_LIMIT at 14:00** and OPTIMAL only at the 10 s limit at 10:00. In this
universe family, 30-35 candidates fit inside the 10 s limit with margin at every tested clock, and the 5 s freshness gate is
never the problem at these sizes (build <= 0.5 s). The cliff between 35 and 38 is a branch-and-bound effect (seed-dependent, see
finding 4), so capacity is not guaranteed at any single N: expect one-clock outliers. The 4-core loaded VM that the earlier numbers
came from is ~2-3x slower per solve than this single pinned core, so scale these accordingly (n20-26 ~3-6 s there).

## Risks

- A replaces the model-building layer of the optimizer. LP-identity checks and tests make it low risk, but it is new code on the
  money path; it relies on CVXPY-1.9 canonicalisation order only for reproducing the *same* search path (a different order would still
  be a correct model, just not the same tie-broken hedge). If CVXPY changes versions the equivalence evidence no longer applies
  to the CVXPY path, not to A.
- The timing-limited turnover stage means no two runs are guaranteed to return the same in-band hedge, with or without these changes.
- C changes only object lifetime in QuantLib; evaluation-date handling is unchanged (still inside `_QL_LOCK`). Cache is per
  engine, bounded at 4096 entries.

## Recommendation

- **Deploy over the weekend: C** (bit-identical output, 2.3-2.7x faster builds, 52 lines, trivial to revert). **A is also safe to
  deploy** on the evidence above (identical LPs, identical statuses and primary results on 48/48, suite green), but its gain is
  small (0.1-0.8 s per solve) and it touches the core solve path, so if the weekend freeze matters keep A for Monday.
- **Not worth deploying:** HiGHS option changes, cross-solve warm starts (can change the chosen hedge), seed racing.
- **Capacity guidance:** 20-30 candidates are comfortably inside the 10 s limit at every tested clock; up to ~35 works here but with
  worst-case 6.8 s on this machine; do not rely on 38.
