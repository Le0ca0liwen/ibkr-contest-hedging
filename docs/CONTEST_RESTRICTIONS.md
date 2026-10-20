# SPCX / RKLB / SPY contest restrictions

Use `config/contest.template.yaml` for this contest, not the BA practice
template. Set `contest_rules: spcx_rklb_spy` and fill in the verified
`contest_option_expiry`, contract definitions, account, calendar, and source.
The handout disagrees on expiry/date/year; no production date, strike or conId
is inferred from it. The template intentionally cannot activate as supplied.

The manifest validates exactly seven mandatory legs, all standard 100-share
options at the confirmed expiry:

| Position | Required quantities |
| --- | --- |
| SPCX calls, ascending strikes | +25 / -50 / +25 |
| RKLB puts, ascending strikes | -25 / +25 |
| SPY call and put, same strike | +25 / +25 |

The following are forbidden for both long and short holdings:

- SPY shares, at any time.
- RKLB calls at the confirmed contest expiry.
- SPY options at that expiry, except the two mandatory contracts.

SPY stock can remain in `contracts` for underlying market data. It cannot be in
`allowlist`. Other-expiry SPY options, other-expiry RKLB calls, SPCX/RKLB shares,
and other permitted stock/option hedges still need explicit approval through the
allowlist and must pass the ordinary risk checks. This version only supports
stocks and options; permission in the contest rules does not add futures support.

Validation rejects forbidden allowlist entries. Screening and optimization also
filter them independently; the final risk gate rejects forbidden target
positions, actual holdings, and working orders, including in corrective mode.
Held forbidden instruments remain visible to risk calculations. If such a
holding exists, automatic trading stops for reconciliation; the system does not
silently submit a liquidation outside its approved universe.

Outside initial setup, both actual and proposed mandatory quantities must match
exactly, including the negative legs. The legacy automatic setup adapter supports
long mandates paired with protective options, not this seven-leg signed
portfolio. It is explicitly disabled for this profile. Establishing the mandated
portfolio requires a separate validated setup workflow; once holdings reconcile
and all other readiness checks pass, ordinary hedge trading can use this profile.
Short mandatory legs are included in assignment-exposure reporting and
conservative incremental-margin estimates.

These changes enforce trading restrictions, not the handout's scoring formula.
The template leaves scoring pending: calendar-day interest accrual (including
weekends), contest dates, and the rate must be separately verified before activation.
