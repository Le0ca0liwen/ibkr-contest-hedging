# Optimizer runtime improvements

The objective is absolute, interest-adjusted closing NAV deviation plus a weighted
close-to-reopen term (below). Mandatory quantities, integer positions,
transaction-cost relationships, liquidity, margin, cardinality, borrow, delta,
gross-notional and stress limits remain enforced. No separate surplus-expense
controller or new directional forecast is activated; whether costs may spend a
surplus is the explicit `policy.burn_surplus` switch.

## Cost treatment and the overnight term

With `g` the scenario closing gap before costs (NAV estimate minus today's target,
plus modeled changes and carry) and `c` the scored execution cost (zero on Day 0):

- `policy.burn_surplus: true` (default, unchanged behaviour): `E|g - c|`. Costs sit
  inside the gap, so while NAV is above target trading partly spends the surplus.
- `policy.burn_surplus: false`: `E|g| + c`, an additive penalty; a surplus is never
  a reason to trade. The SPCX/RKLB/SPY contest template uses this setting: the
  operator spends a surplus only near the close, through the separate closing
  burner (docs/CLOSING_BURNER.md), not through all-day hedge costs.

**The day's statement interest (`policy.statement_interest`).** The scored statement credits
the day's whole interest accrual (three days on a Monday), but IBKR's live AccruedCash takes it
only overnight (22:20 ET Oct 22, 23:17 ET Oct 23). A carry pro-rated to the close therefore left a
false deficit of about $80 on a weekday: on Oct 26 the app saw -$236 at 15:45 while the
statement estimate at 16:20 was +$141, $233.58 of it the weekend's interest. With the switch on,
the service adds the accrual learned from imported statements to the estimate the optimizer
scores (`TradingService._closing_estimate`), the same amount the post-close statement estimate
adds. The closing carry then leaves out the cash credit; borrow and debit interest stay. The
overnight term keeps its credit: tomorrow's statement holds tomorrow's interest. IBKR paid
3.38% a year on cash above $10,000 in October, on the settled cash of two days earlier, against
the scoring benchmark's 3.44% on the whole NAV: about $13 a weekday less than the target's growth.

**Burner-aware closing loss and short-dated tier: tested, left off (Oct 26).**
`policy.surplus_weight` (< 1, with `burn_capacity`) scores the close as
`max(-g, w g, g - (1 - w) C)` while the post-close burner is in paper;
`policy.short_dated_candidates` screens strikes of the shortest listed expiry at least
`short_dated_min_days` out (options may not expire before the final holding session, so SPY's
nearest is Nov 5/6). Replayed through the real service on the Oct 8-21 sessions (1-minute stock
paths, fixed implied vols, IBKR-paper-like fills), each 16:00 miss scored with the shipped burner
(spend to prediction - $50, error N(+28, 30), capacity $2,000), mean daily |miss|:

| 10 sessions | With the burner | Without | Better than the symmetric loss |
|---|---|---|---|
| Symmetric `E|g|` (live) | **$456** | $1,009 | |
| `surplus_weight` 0.25, capacity $2,000 | $557 | $1,118 | 3 of 10 |
| The same + Nov 6 SPY tier | $490 | $1,061 | 1 of 10 |

Weighting a surplus lightly made the optimizer favour long gamma (theta -$385 a day against
-$354): upside it could burn, paid for by decay it could not. Both stay off. The burner itself
more than halves the replayed miss.

Each decision records `cost_treatment` and `burn_component = E|g| + c - E|g - c|`
(between 0 and 2c; 2c means the whole cost was credited as surplus burn), plus
`overnight_score` and the target's `components` (closing, overnight, scored_cost,
burn). The dashboard decision log shows them.

Today's close is the last instant the app can act before the next scored session,
so decay and gaps until it can trade again land in tomorrow's score. The objective
adds `w * E|O h + carry_next - ((1 + r/360)^n_next - 1) * target_today|`, where
`w = policy.overnight_weight` (default 0.5, 0 disables, at most 2), `O` holds
per-contract changes from today's close to the next scored session's open + 15
minutes, `n_next` is that session's accrual days and `r` the scoring rate
(`Manifest.accrual_days` and `Manifest.benchmark_growth`). Carry accrues over the
same `n_next` calendar days as the benchmark; the next session's own accrual is
deterministic, so stopping it at 9:45 would add a spurious ~$25 shortfall that
tomorrow recovers without trading. There is no term on the final scored session.

`O` reuses the closing scenarios' joint shocks. Options decay in calendar time to
the reopen. Spot and IV variance follow a simple trading-time clock, a **modeled
assumption** rather than an estimate: one weeknight carries 0.30 of a trading
day's variance (sigma^2/252, or one row of the daily covariance) and every extra
calendar day adds 0.10, so Friday to Monday is 0.50.

Scores are evaluated identically by the CVXPY objective, `evaluate_target`, the
post-rounding certificate check and no-trade targets, and the certified primary
bound includes the term. `historical_l1` uses the same treatment; `two_close`
keeps its next-close term instead of this one. When the overnight term is active the
next-close tie-breaker stage is skipped, because it would re-rank the $1 band by
the next-day exposure the primary objective already prices; with `w = 0` the
previous primary/next-close/turnover sequence is unchanged. Instruments outside
their order window (options after the 15-minute cutoff) are frozen, mirroring the
RiskGate's window check, so close-time overnight hedges never propose an option
order the gate would reject.

Synthetic timings (`ops/benchmark_optimizer.py --baseline --baseline-ref 40007bd`,
one machine, computation only): on the default 7-contract demo the full solve
went from 4.1-4.6 s to 2.8 s (no next-close stage) and stock-only stayed at about
0.13 s. On a contest-shaped synthetic universe (SPCX/RKLB/SPY legs, 20 permitted
candidates) stock-only went from 0.88 s to 1.37 s, but the full solve hit the 10 s
deadline (`USER_LIMIT`, no trade) both before and after, also with 16 candidates.
Benchmark the real contest universe before relying on option-side overnight hedges.

## Model changes

- Only net target quantities need integer domains. Sign and trade-direction gates
  imply integer positive/negative holdings and buy/sell quantities. Zero-current
  positions reuse trade-direction indicators for sign and selection. Forced signs
  use constants instead of additional binaries.
- Stock bounds incorporate available borrow and an implied delta interval when
  its symbol has only one variable stock and frozen options.
- Equal buy/sell execution schedules share one exact slice count. One-contract
  option slices use the integer trade quantity directly. Asymmetric displayed
  liquidity retains separate direction-specific schedules.
- Count remainder allowances are gated by trade activity. Inactive trades cannot
  contribute fractional phantom commissions to the LP relaxation; integer cost
  relationships remain exactly the same as before.
- Cash positive-part functions use proved interval bounds and omit binaries when
  the sign is fixed. Zero cash-credit rates omit the unused eligible-credit split.
  Original absolute cash bounds remain in force.
- Stress rows with identical candidate coefficients retain the lowest fixed
  portfolio value, which is the strongest constraint in that group. Exact floating
  equality is required; no stress sampling, rounding or approximation is used.
- A single parameterized CVXPY problem serves primary, next-close and turnover
  stages. Public warm starts provide HiGHS with the previous solution while the
  canonical variable layout remains unchanged.

## Certified score selection and bounded ranking

The original design permits selecting a portfolio within $1 of the optimal modeled
primary score. Primary solves now request a $0.25 absolute gap, with zero relative
gap. If L is HiGHS' certified lower bound and P* is the true model optimum, the
subsequent constraint is primary score <= L + $1. Since L <= P*, every returned
portfolio stays within $1 of P*. The limit is not incumbent + $1, which could
otherwise add the incumbent's gap to the tolerance. The baseline profiles keep
their existing 0.01 selection band and use a 0.0025 initial gap.

No unsuccessful primary solve is accepted. Status, finite bounds and the actual
primal-dual gap are checked. Numerical costs and cash carry are recalculated after
integer rounding, and the returned objective certificate is checked again.
Independent execution risk/economics gates still decide whether an order can trade.

Next-close selection uses the same lower-bound method for its $1 band. Turnover
keeps its 1e-6 gap. Secondary work shares a configurable one-second budget and
32-node per-stage limit (`policy.ranking_seconds`, `policy.ranking_nodes`). The
next-close attempt receives at most half the ranking time. If it cannot finish,
turnover is not run out of order. Only a copied solution from a successfully
certified earlier stage is retained; later timeout values are discarded.

`Target.status == OPTIMAL` means an accepted certified target in the declared
selection band. It does not assert that optional ranking finished or that the
initial primary gap was exactly zero. Decisions record the accepted stage,
ranking completeness, objective lower bound, actual returned objective, variable
counts, reduced stress rows, node counts, and phase/total timings. The dashboard
shows runtime and incomplete-ranking status. Failed primary certificates produce
no-trade targets with an explicit failure status.

The overall ten-second budget now includes formulation/canonicalization. Calls
are single-threaded with the existing fixed seed. Node limits reduce ranking
variability, but wall-time expiry under different machine loads can still affect
whether a solve finishes; it never permits an uncertified target.

## Offline evidence and reproduction

Use the diagnostic below from the repository. It uses synthetic quotes and
mandatory-only inventory, never connects to IBKR, Moomoo or PostgreSQL, and does
not submit orders. Configuration is used to size the universe, not as live data.

```sh
.venv/bin/python ops/benchmark_optimizer.py --baseline --baseline-ref 6b9d1fc --repeats 3
.venv/bin/python ops/benchmark_optimizer.py --config config/local.practice.yaml --baseline --baseline-ref 6b9d1fc --repeats 1
```

Initial measurements on the configured 24-instrument/14-option universe changed
full-portfolio computation from a roughly ten-second primary timeout to roughly
two seconds with a certified primary target. A harder stock-only case still took
roughly nine seconds; it produced a certified target rather than discarding a
successful primary solve due to unfinished secondary work. Final repeated timings
are recorded in the validation report. These are computation measurements, not
historical contest-score improvement, market execution latency, or a guarantee
that every future solve will meet its deadline.

Validation covers exact cash-sign functions, asymmetric slice counts, brute-force
integer objective comparisons, borrow/holdings constraints, exact stress-row
dominance, rounded objective certificates, rejected invalid bounds, primary
deadlines, retention after failed ranking, and honest ranking-status reporting.
The full existing scoring, valuation, broker, execution, dashboard and recovery
regression suite remains required before deployment.
