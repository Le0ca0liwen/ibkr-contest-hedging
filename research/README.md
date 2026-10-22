# Offline research harnesses and real market data

This folder holds the offline studies behind the contest-day tuning work: the cooldown/threshold comparison on real
sessions, the spread-aware model replay and its prototype pipeline, solver-speed profiling, the option-universe
frontier, and some smaller harnesses. It contains **code and inputs only**: no run outputs, databases or caches.
Every harness imports the app (`hedge/`) read-only from a source tree you choose. Nothing here talks to IBKR or
Moomoo or writes to the app.

Target machine: Ubuntu 24.04 (WSL2 on Windows is fine), Python 3.12, 16 cores. Every command below was smoke-tested
with Python 3.12.3 and `requirements.lock` (see [Verification](#verification)).

## Contents

| Folder | What it is | Main entry points |
|---|---|---|
| `marketdata/` | Real data: 1-minute bars for SPCX, RKLB and SPY over 10 sessions, 1 year of daily bars, the option tables built from the Oct-20 CBOE chain and the Oct-21 13:13 ET quotes, the full chain snapshot, Nasdaq daily closes for peers, and CBOE VIX term-structure history. Read `marketdata/README.md` for provenance and caveats. | `replay_loader.py`, `scripts/build_options.py` |
| `config/` | `small.contest.yaml` (the contest manifest with the real 17-contract hedge universe, fake paper account `DU1234567`) and `local.contest.yaml` (the larger dry-run universe used only by `realbench/`). | |
| `cooldown/` | **Cooldown comparison.** Replays each real session through the real `TradingService` loops on a discrete-event clock. | `runner.py`, `sim_replay.py`, `precompute.py`, `analyze*.py` |
| `spread_model/` | **Spread-aware model replay** and the **pipeline prototype** (`spread_aware_proto.patch`). Same idea as `cooldown/`, with a Poisson passive-fill model and per-variant app source trees. | `batch.py`, `replay_sim.py`, `make_proto.sh`, `final_table.py` |
| `solverspeed/` | **Solver-speed profiling**: per-phase timing of `ScenarioEngine.build` and `HedgeOptimizer.solve` at 12 to 38 candidates. | `harness.py`, `summarize.py`, `profile_hotspots.py`, `replay_highs.py` |
| `frontier/` | **Option-universe frontier**: hedge-universe variants V0 to V8 priced at the Oct-21 market and solved through the real app path. | `frontier.py`, `frontier_x.py`, `run_*.sh`, `summarize2.py` |
| `scan/` | Universe-size scaling: synthetic N-underlying universes, with options kept per symbol (`--keep`). Gives the solve-time side of the frontier. | `scale3.py`, `bench_scale.py`, `cprof6.py` |
| `threshold/` | Score-threshold study ($30/$50/$100) on synthetic random-walk paths. | `sim.py`, `launch.sh`, `aggregate.py`, `pooled.py` |
| `contest_day/` | The original contest-day simulator (burner, autostart, SPY jumps). `threshold/sim.py` and `cooldown/sim_replay.py` were derived from it. | `sim.py`, `analyze.py`, `run_twostep*.py` |
| `realbench/` | Solve-time benchmark on the large dry-run universe at four realistic clocks. | `bench.py` |
| `econ/` | Historical-scenario hedge-value study: the scan-value question on the 88 real joint daily closes. | `peers.py` → `sim.py` → `greekhedge.py`, `sim_app.py`, `greeks.py` |

## Setup

```bash
# Inside WSL2 Ubuntu: clone into the Linux filesystem (~/...), not /mnt/c. That is much faster and safe for SQLite.
sudo apt install -y python3.12-venv git
git clone <repo-url> ibkr-contest-hedging && cd ibkr-contest-hedging
git checkout claude/research-harness
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock          # the app's pinned deps (QuantLib 1.43, cvxpy 1.9.3, highspy 1.15.1, ...)
pip install pyinstrument                  # optional: only for solverspeed/profile_hotspots.py --pyinstrument
cd research
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 PYTHONWARNINGS=ignore
```

### Path variables

All paths come from environment variables. With the defaults, `cd research && python <lane>/<script>.py` works in any clone.

| Variable | Meaning | Default |
|---|---|---|
| `RESEARCH_ROOT` | this `research/` folder: inputs are read from it and outputs are written under it | the folder above the running script |
| `REPO` | the app source tree to import `hedge` from | the parent of `RESEARCH_ROOT` (the repository root) |
| `HEDGE_SRC` | overrides `REPO` for `solverspeed/`, `scan/` and `econ/` (their historical variable name) | `$REPO` |
| `PROTO_REPO` | the prototype tree for `spread_model/replay_sim.py --repo proto` | `spread_model/proto` (built by `make_proto.sh`) |
| `PYTHON` | the interpreter that the shell wrappers and job runners launch | `python3` in shell, `sys.executable` in Python |
| `BENCH_CONFIG` | the manifest for `realbench/bench.py` | `config/local.contest.yaml` |

### Which app version? (read this before comparing numbers)

The harnesses monkeypatch app internals (clock, broker fills, solver budget), so their results depend on the `hedge/`
source they import. The default is this checkout. Each study originally ran on an older commit:

| Study | App commit used | Reproduces exactly? |
|---|---|---|
| `cooldown/` | `92b4121` | Yes. The 2026-10-20 warm-up gives miss 2050.91 with 37 fills, as in the study. On `c782c84` it gives 2241.57 with 38 fills, because the app changed. |
| `spread_model/` (`base`, and `proto` = base + patch) | `92b4121` | Yes. `h0.json` regenerates byte-identical. `make_proto.sh` rebuilds the study's `proto/hedge` tree file-for-file. |
| `solverspeed/`, `frontier/` | `1973abb` | Same inputs. Timings depend on the machine. |
| `scan/`, `econ/` | `982eeed` | `econ/` outputs (`sim_1.json`, `greeks_1.json`, `returns_aligned.csv`) are byte-identical even on `c782c84`. |
| `threshold/`, `contest_day/` | the `burner/closing-surplus` worktree on 2026-10-21 (between `0758d36` and `1973abb`) | Not pinned. Use `1973abb` or the current HEAD. |

To run against a study commit, check it out next to the clone. All the commits are ancestors of this branch.

```bash
git -C .. worktree add --detach ../hedge-92b4121 92b4121     # creates a sibling of the clone (git -C: paths relative to the repo root)
export REPO=$(realpath ../../hedge-92b4121)                  # from research/: then run the cooldown / spread_model commands below
```

The market tables (`cooldown/market/*.npz`, `spread_model/cache/*.npz`) do **not** depend on the commit:
`hedge/valuation.py` is unchanged, and they regenerate bit-identically on `c782c84`.

## Parallel runs on 16 cores

* Every simulation is a **single-threaded process** using about 300 to 350 MB RSS. `runner.py` sets
  `OMP/OPENBLAS/MKL_NUM_THREADS=1` for its children, so export the same variables (see Setup) for anything you launch
  yourself.
* Use **12 workers** (`runner.py --workers 12`, `batch.py 12 ...`, `xargs -P 12`). That leaves cores for the OS,
  WSL and HiGHS's helper threads. 12 workers need about 4.5 GB. WSL2 gets 50% of host RAM by default; raise it in
  `%UserProfile%\.wslconfig` (`[wsl2]` `memory=12GB` `processors=16`) if needed.
* The replays run on a **frozen discrete-event clock**: simulated time does not pass while code computes. Results
  therefore do not depend on CPU load, except for one thing: the solver's **wall-clock budget** (HiGHS `time_limit`
  and the optimizer's deadline). The studies ran on a 4-core VM at a load of 11 to 16, so the budget was stretched
  with `--solver-scale 3` (`cooldown/`) and `--time-scale 4` (`spread_model/`). Keep the defaults to stay comparable
  with the study's runs. To model the production 10 s budget on a free core, pass `--solver-scale 1` or
  `--time-scale 1`. Time-limited MIP solves are never bit-reproducible across machines; expect small differences in
  some runs.
* Do not run `solverspeed/` or the frontier timing scripts (`run_timing.sh`) alongside other jobs: they measure wall
  time.
* `spread_model/replay_sim.py` keeps its SQLite file in `/dev/shm/spread_model/<run>/`. This is a tmpfs, present in
  WSL2 too, and the file is removed at the end of the run.

The runtimes below were **measured on the 4-core VM at a load of 11 to 16**, so each process had roughly a third of
a core. On dedicated cores, expect them to be shorter.

---

## 1. Cooldown comparison: option cooldown 900/300/120/60 s × score threshold $100/$50, over the 10 real sessions

**What.** Each real session (2026-10-08 ... 10-21) is replayed as contest day 2026-10-22 through the real
`TradingService` loops (event loop, monitor, burner, closing recorder, close marks). Quotes replay the real 1-minute
paths as relative moves from the Oct-21 13:13 spots (SPCX 160.30, RKLB 53.24, SPY 773.85), interpolated linearly
within each minute. Options are priced every second with the app's `ValuationEngine` at each contract's chain or
today's IV, quoted with its real relative spread. Fills use `--fill touch` (IBKR-paper-like: a limit fills only when
marketable, at the bid or ask) or `--fill mid`. Per session there is one 09:30 to 09:45 **warm-up** with the default
policy. Then every variant runs 09:45 to 16:00 from the warm-up's `state.json`. The miss is scored against the
official close.

**Inputs.** `marketdata/bars_1m.csv`, `sessions_summary.csv`, `daily_1y.csv`, `options_contracts.csv`,
`config/small.contest.yaml`, and `$REPO/config/contest.template.yaml` (burner block).

```bash
# 1) market tables: market/<session>.npz (per-second option prices, 5 s greeks). About 4-9 min per session per core.
JOBS=10 bash cooldown/run_precompute.sh                       # all 10 sessions at once (one core each)
#    or one session: python cooldown/precompute.py 2026-10-20

# 2) the whole grid: 10 warm-ups + 8 variants x 10 sessions = 90 jobs. Resumable: a job with summary.json is skipped.
python cooldown/runner.py --workers 12                         # writes cooldown/runs/<job>/{log.jsonl,summary.json,state.json}
#    detached:  bash cooldown/start_runner.sh 12                (log: cooldown/runs_runner.out, cooldown/runs/runner.log)
#    subsets:   --sessions 2026-10-20 2026-10-15   --variants 900:100,60:100   --fill mid   --solver-scale 1

# 3) reports
python cooldown/analyze.py  > cooldown/runs/report.txt         # |miss|, costs, flips, stops per variant, paired vs o900_t100
python cooldown/analyze2.py                                     # residual risk, plans, bootstrap CIs vs touch_o900_t100
python cooldown/diagnostics.py                                  # realized vs modeled spread cost, stalled plans, stop causes
python cooldown/reference.py                                    # no-hedge and hold-09:45-hedge reference misses
python cooldown/waitjobs.py 600                                 # helper: block until the next job finishes
```

Job names follow `touch_o<seconds>_t<threshold>__<session>`. A single job by hand (this is the smoke test: one
15-minute window each):

```bash
python cooldown/sim_replay.py --session 2026-10-20 --out cooldown/runs/warm_touch__2026-10-20 \
    --fill touch --solver-scale 3 --start 09:30:00 --end 09:45:00
python cooldown/sim_replay.py --session 2026-10-20 --out cooldown/runs/touch_o60_t50__2026-10-20 \
    --fill touch --solver-scale 3 --start 09:45:00 --end 10:00:00 \
    --policy '{"option_seconds": 60, "score_threshold": 50}' --state cooldown/runs/warm_touch__2026-10-20/state.json
```

**Runtime.** Warm-up: 2 to 3 min. Day jobs (09:45 to 16:00), median [min to max] in seconds:

| | t100 | t50 |
|---|---|---|
| o900 | 940 [716–2944] | 661 [476–3066] |
| o300 | 1047 [846–1193] | 886 [777–3399] |
| o120 | 1388 [1333–1914] | 1353 [1227–1478] |
| o60 | 2500 [1848–2680] | 1951 [1603–2299] |

The whole grid is about 30 job-hours at the measured speed, so about 2.5 h of wall time with 12 workers, and less
on dedicated cores. The shorter cooldowns are slower because they solve more often.

## 2. Spread-aware model replay (`spread_model/`, `--repo base`)

**What.** Same replay idea with these differences. Each session is anchored to the previous official close
(`S_t = S_today × raw_t / close(D−1)`, which includes the overnight gap). Every run starts from the mandated book plus
the shared overnight hedge **H0** (`h0.json`, one Day-0 15:44 app solve). Prices move on a 5 s grid. The fill model
runs every simulated second: marketable orders fill at the touch, and passive limits fill with a **Poisson hazard**
that rises toward the touch (`--fill poisson`, the default, with common random numbers across variants). The other
models are `touch` (passive limits never fill) and `instant`. Each run writes `runs/<name>/{log.jsonl,report.json}`
with the miss, the miss of holding H0 (`miss_hold`), fills, spread paid, decisions and close marks.

**Inputs.** `marketdata/*` (via `replay_loader.py`), `config/small.contest.yaml` and `h0.json`. Sessions are the 9
complete ones; the study used 10-09, 10-14, 10-15, 10-16 and 10-20.

```bash
# 1) per-session caches spread_model/cache/<day>.npz. Build them BEFORE batch runs: two jobs of the same day would
#    otherwise both build the cache at once. About 4-9 min per session per core.
printf '%s\n' 2026-10-09 2026-10-14 2026-10-15 2026-10-16 2026-10-20 | xargs -P 5 -I{} python spread_model/precompute.py {}
#    (no argument = all 9 complete sessions, one after another)
python spread_model/h0.py            # optional: regenerates h0.json (committed; identical at 92b4121)

# 2) runs: a JSON list of jobs {name, day, repo, policy, execution, fill}. Resumable (skips runs with report.json; "force": true reruns).
python spread_model/batch.py 12 spread_model/jobs/final.json      # needs the proto tree (section 3) for its proto jobs
#    one run by hand:
python spread_model/replay_sim.py --name A_C300_1020 --day 2026-10-20 --repo base --policy '{"option_seconds": 300}'
#    or: bash spread_model/runone.sh NAME DAY REPO POLICYJSON EXECJSON FILL

# 3) reports
python spread_model/final_table.py                  # per-variant metrics and paired differences vs A_B900 (the study's final table)
python spread_model/final_report.py A_B900 A_C300 A_C60 A_P3 A_C3q
python spread_model/compare.py A_ T2_               # aggregate by run-name prefix
python spread_model/analyze.py spread_model/runs/A_P3_1020        # one JSON line per run
python spread_model/planexec.py spread_model/runs/A_P3_1020       # planned vs executed per eligible decision, slice-gate reasons
(cd spread_model/runs && python ../diag_realize.py A_B900_1009)   # modeled vs realized plan benefit -> spread_model/diag/
```

`jobs/` holds the study's batches as they were run, A to J, plus `final.json`. `final.json` is a consolidation written
for this package: the 24 runs behind the final table, which are A_B900, A_C300, A_C60 and A_P3 on the 5 sessions,
A_C3q on 10-15 and 10-20, and T2_B900 and T2_P3 (touch fills) on 10-20. `jobsD.json` and `jobsE.json` used an
intermediate prototype policy key (`gross_headroom`) that the final patch replaced with `cap_hysteresis`. They are
kept for the record only.

**Runtime per day job** (median in seconds; the slowest single run in brackets): B900 1543 [3079], C300 1218 [3832],
C60 4332 [8924], P3 4779 [4900]. That is about 10 job-hours for `final.json`, or about 1.5 h of wall time on 12
workers at the measured speed.

## 3. Pipeline prototype (`spread_model/spread_aware_proto.patch`, `--repo proto`)

**What.** The spread-aware decision and execution pipeline, applied to the baseline source as a patch to
`hedge/config.py`, `execution.py`, `models.py`, `optimizer.py` and `service.py`, plus `tests/test_spread_reserve.py`.
It adds:
* a **reversal reserve** (`option_reversal_reserve`, `stock_reversal_reserve`): a plan must also pay for the expected
  round trip, which creates a spread-scaled no-trade band;
* **cap hysteresis** (`cap_hysteresis`): new plans stay at 97% of the gross cap, or 90% of the delta cap, so that a
  plan does not park the book on a cap edge;
* **plan-level slice economics** (`execution.slice_economics: "plan"`): the plan carries the significance test, and
  slices get path headroom with an executable-part check;
* an optional passive option fraction (`option_passive_fraction`).

Every default keeps the original behaviour. The study's final variant P3 is `{"option_seconds": 60,
"option_reversal_reserve": 1.0, "cap_hysteresis": true}` with `{"slice_economics": "plan"}`.

```bash
bash spread_model/make_proto.sh            # git archive 92b4121 (hedge, config, tests) + patch -> spread_model/proto/
#    another base: bash spread_model/make_proto.sh c782c84    (the patch also applies cleanly to c782c84, with offsets)
#    elsewhere:    PROTO_REPO=/path/to/tree bash spread_model/make_proto.sh
(cd spread_model/proto && python -m pytest -q tests/test_spread_reserve.py)      # the prototype's own tests (17 pass)
python spread_model/replay_sim.py --name A_P3_1020 --day 2026-10-20 --repo proto \
    --policy '{"option_seconds": 60, "option_reversal_reserve": 1.0, "cap_hysteresis": true}' \
    --execution '{"slice_economics": "plan"}'
python spread_model/batch.py 12 spread_model/jobs/final.json       # base and proto variants together
```

`--repo` accepts `base` (= `$REPO`), `proto` (= `$PROTO_REPO`) or any path to an app source tree.

## 4. Solver-speed profiling at 12 to 38 candidates (`solverspeed/`)

**What.** No broker and no network. The mandated book is held. Optimizer candidates are SPCX and RKLB shares plus
nested sets of nearby options (12, 14, 17, 20, 26 or 38), all of which pass `screen_options`. They are quoted at
today's spots with chain IVs. The cached quote tables in `cases/quotes_*.json` make every source tree see identical
inputs. The harness splits each solve into construct, canonicalization, HiGHS presolve and run, unpack and post, and
each scenario build into its phases. One JSON line per measurement goes to `--out`, followed by a summary table.

**Inputs.** `cases/` (quotes and history, committed), `marketdata/econ/chains.csv` and `daily.csv` (only if `cases/`
is deleted), `config/small.contest.yaml`.

```bash
HEDGE_SRC=$REPO python solverspeed/harness.py --tag base       # 12,14,17,20,26,38 x 10:00,14:00,15:40 x 3 reps (+ stock-only at 12, 38)
HEDGE_SRC=/path/to/other/tree python solverspeed/harness.py --tag variant
python solverspeed/summarize.py solverspeed/results.jsonl --tags base,variant --by-clock
python solverspeed/profile_hotspots.py --n 38 --clock 15:40 --what canon,solve,build [--pyinstrument]   # cProfile -> solverspeed/prof/
python solverspeed/harness.py --cands 38 --clocks 15:40 --reps 1 --dump-mps /tmp/mps          # dump the stage models, then:
python solverspeed/replay_highs.py /tmp/mps/n38_1540_r0_full_stage1.mps --stage primary --reps 3
```

**Runtime.** The full default sweep takes about 6 to 10 min. Solves at 26 and 38 candidates hit the 10 s budget, so
run the sweep **alone**. The smoke test (`--cands 12,38 --clocks 10:00 --reps 1 --stock-only 12`) takes 20 s.

## 5. Option-universe frontier (`frontier/` and `scan/`)

**What (`frontier/`).** Hedge-universe variants V0 to V8 (`VARIANTS` in `frontier_x.py`) are priced at the Oct-21
13:13 ET market and solved on scored day 2026-10-22 through the real app path:
`screen_snapshot → ScenarioEngine.build → HedgeOptimizer.solve(full) → RiskGate.validate`. Each run starts from the
mandated book. Clocks are 10:00, 12:30, 14:00, 15:00 and 15:40. There are 3 quote seeds that perturb spots and IV,
and 5 out-of-sample scenario seeds that score the target against independent scenario sets.

Regimes:
* `live`: every spread is capped at $0.45, so every candidate is tradable. This gives the solve-time frontier.
* `asq`: the quoted spreads.
* `asqc`: chain spreads floored at today's spread for the same expiry and right (only in `frontier_x.py`).

`frontier_x.py` is a superset of `frontier.py`: it adds V6 to V8 and `asqc`.

**Inputs.** `calib.json` (today's IVs implied from the monitor mids, plus per-expiry chain scale factors; regenerate
it with `python frontier/calib.py`), `daily-contest.csv` (daily history for 19 symbols),
`marketdata/econ/chains.csv`, `config/small.contest.yaml`.

```bash
git -C .. worktree add --detach ../hedge-1973abb 1973abb && export REPO=$(realpath ../../hedge-1973abb)   # the study's commit (optional)
bash frontier/run_all2.sh          # V0..V5 x {live, asq} + V5 --noscreen, sequential -> frontier/results2.jsonl   (~56 min)
bash frontier/run_extra.sh         # V6..V8 x {live, asq} + asqc V0,V4,V6..V8      -> frontier/results_x.jsonl  (~30 min)
#    run_extra.sh first waits until frontier/run2.log (written by run_all2.sh) contains DONE; run run_all2.sh first.
bash frontier/run_timing.sh        # timing-only re-run, no OOS, nice -n -15         -> frontier/results_timing.jsonl (~7 min)
python frontier/summarize2.py frontier/results2.jsonl      # value and solve-time table per (regime, variant)
python frontier/components.py                              # score components (closing miss, overnight, cost) per variant
#    one process: python frontier/frontier_x.py --variant V6 --regime asq [--clocks 1000,1540 --qseeds 0 --oos-seeds 101]
```

Each (variant, regime) process takes 2 to 4 min and is independent of the others, so the value results can run 12
at a time, for example:
`for r in live asq; do for v in V0 V1 V2 V3a V3b V3c V4 V5 V6 V7 V8; do echo "$v $r"; done; done | xargs -P 12 -L1 sh -c 'python frontier/frontier_x.py --variant $0 --regime $1 --tag par --out frontier/results_par.jsonl'`.
Wall and CPU timings are only meaningful from the sequential scripts. `run_timing.sh` uses `nice -n -15`, which
needs root; without root, GNU `nice` prints a warning and runs the command at normal priority.

**What (`scan/`).** This is the solve-time side of the frontier: synthetic contest-shaped universes of N underlyings
(SPCX, RKLB and SPY plus synthetic names), keeping the `--keep` nearest-money screened options per symbol. Output
goes to `scan/out/scale3.jsonl`, `history_N.csv` and `state_*.pkl`.

```bash
# the study's frontier grid (tag "frontier"): (N, keep) at 10:30 and 15:40, full solves only. Each run takes 10-40 s.
for nk in "3 4" "3 6" "3 8" "6 2" "6 4" "6 6" "12 1" "12 2" "12 4" "24 1" "24 2"; do set -- $nk
  for hm in "10 30" "15 40"; do set -- $1 $2 $hm
    python scan/scale3.py --n $1 --keep $2 --hour $3 --minute $4 --modes full --tag frontier; done; done
python scan/bench_scale.py --n 12 --modes stock,full --tag n12     # build and solve vs universe size -> scan/out/results.jsonl
python scan/cprof6.py 12                                           # cProfile of one full solve (needs scan/out/history_12.csv from a run above)
```

## 6. Other harnesses

**Score-threshold study (`threshold/`).** Synthetic random-walk paths (`--path walk --seed S --vol-scale V`) run
through the real service with the frozen clock. The study used paths W11, W23, W47 and W59 at vol 1.0 and C37
(calm) at vol 0.35, each at thresholds $30, $50 and $100: 15 day runs of 30 to 46 min each.

```bash
bash threshold/launch.sh W11_t100 walk 11 1.0 100          # NAME PATH SEED VOLSCALE THRESHOLD (09:30-16:01) -> threshold/runs/NAME/
for s in "W11 11 1.0" "W23 23 1.0" "W47 47 1.0" "W59 59 1.0" "C37 37 0.35"; do set -- $s
  for t in 30 50 100; do echo "${1}_t$t walk $2 $3 $t"; done; done | xargs -P 12 -L1 bash threshold/launch.sh
python threshold/analyze_thr.py threshold/runs/W11_t100
python threshold/aggregate.py threshold/runs/*_t*/ > threshold/runs/agg.json      # table on stderr
python threshold/pooled.py threshold/runs/*_t*/
```

**Contest-day simulator (`contest_day/`).** The default window is 15:25 to 16:01, with the burner in paper mode and
$150 of surplus. It is not frozen: simulated time runs at wall speed while code computes. Options cover missing legs,
IOC patterns, SPY jumps and `--dash`. A full window takes 5 to 10 min.

```bash
python contest_day/sim.py --name B_paper --burner paper --surplus 150      # -> contest_day/runs/B_paper/
python contest_day/run_twostep.py ; python contest_day/run_twostep_down.py  # SPY +0.15% then +/-0.05% (D_spyjump2/3)
python contest_day/analyze.py contest_day/runs/B_paper
```

**Realistic solve-time benchmark (`realbench/`).** The large dry-run universe from `config/local.contest.yaml`, with
real spots and IVs, at four clocks. It takes about 1 min.
`python realbench/bench.py "${REPO:-..}" [policy_key=value ...]` (the app tree is a positional argument)

**Historical-scenario hedge value (`econ/`).** It runs in under a minute and reproduces the study's outputs
byte-for-byte.

```bash
python econ/peers.py               # aligned daily returns + VIX change -> econ/out/returns_aligned.csv
python econ/sim.py 1               # 88 historical one-session scenarios -> econ/out/scen_1.npz, greeks_1.json, sim_1.json
python econ/greekhedge.py 1        # greek-matched fixed hedge, marginal value of scanned names
python econ/sim_app.py             # fit on app-style scenarios, score on realized days
python econ/greeks.py              # mandated-book greeks
#    sensitivity variants: env ATM_RATIO (0.75), EXP_NOISE (0.10), TWIST (1) and TAG (output suffix), e.g.
#    ATM_RATIO=0.9 TAG=_ratio90 python econ/sim.py 1 && TAG=_ratio90 python econ/greekhedge.py 1
```

`econ/parse_chain.py` and `parse_hist.py` (CBOE and Nasdaq JSON to CSV) and `marketdata/scripts/*` (WSJ and CBOE
downloads, `build_bars.py`, `build_options.py`) rebuild the data from raw downloads, which are not packaged. Run them
with `python -I` because their inputs are untrusted downloads. `bash marketdata/scripts/refresh.sh OUT_DIR`
re-downloads into a new folder.

## Caveats

These come from `marketdata/README.md`; read that file for the details.

* **Option prices are modeled. There are no real intraday option prints.** Simulated quotes are `ValuationEngine`
  prices on the real stock paths at a fixed per-contract IV (sticky strike, unless you use `options_smiles.json`),
  with a constant relative spread taken from one snapshot. Real IV moves with spot (SPCX IV fell about 8% on the
  +8.6% gap of Oct-21), and real spreads widen at the open, near the close and on fast moves. A constant-IV replay
  understates P&L from vol and from spread-crossing in stressed minutes.
* **One snapshot each:** the chain IVs and spreads come from the Oct-20 close (`econ/chains.csv`, valued at the
  put-call-parity spot, because its `under` column is an after-hours print). Today's come from one 13:13 ET look.
  The SPCX Nov13 ratio is borrowed from Nov20.
* **Paths:** 10 sessions of 1-minute data, which is short. 2026-10-21 has 389 bars: the 15:59 bar is missing, so the
  path stays flat for the last minute, and the official close is present. RKLB's window contains an unusual sell-off
  (Oct 14 to 16), so treat its tails as stressed, not typical. No longer intraday history was obtainable (Yahoo
  returned HTTP 429; Stooq, Barchart and CNBC were blocked).
* **Within the minute** the path is interpolated (linear, or a seeded Brownian bridge in `replay_loader`). Real moves
  inside a minute and bid/ask bounce are not in the data. Stock spreads are added by the simulators: 2 cents for SPCX
  and RKLB, 1 cent for SPY.
* **WSJ prices are consolidated last trades**, not NBBO mids. The official close can differ from the 15:59 bar by up
  to 10.8 bp (SPCX), so scored closes use `official_close`.
* **Replay convention:** past sessions are replayed as relative moves from today's spots on contest day 2026-10-22.
  `cooldown/` scales by the session open; `spread_model/` scales by the previous official close, which includes the
  overnight gap. Option time-to-expiry is therefore that of Oct-22, not of the historical date.
* **App version and solver budget:** see the commit table and "Parallel runs" above. Results on the current HEAD
  differ from the study's numbers by design.
* `jobsD.json` and `jobsE.json` need an intermediate prototype revision that is not packaged (see section 2).

## Verification

Before this branch was committed, it was checked out in a fresh worktree of `c782c84`, with `research/` copied in
and `RESEARCH_ROOT`/`REPO` pointing there. One short job of each harness was run under Python 3.12.3 with
`requirements.lock`, and every one started and wrote output:

* the cooldown precompute, a warm-up (09:30 to 09:45), a variant from its state (09:45 to 10:00), `runner.py`, and
  the analysis scripts;
* the spread-model cache, `base` and `proto` replays (09:30 to 09:45), `batch.py`, `make_proto.sh` and the
  analysis scripts;
* `solverspeed` at 12 and 38 candidates, `profile_hotspots.py` and `replay_highs.py`;
* `frontier.py` and `frontier_x.py` (one clock), `calib.py` (`calib.json` regenerates identically), `scale3.py`,
  `bench_scale.py` and `cprof6.py`;
* `threshold` (09:30 to 09:45) with `analyze_thr.py`, `aggregate.py` and `pooled.py`;
* `contest_day` (15:25 to 15:40) and `run_twostep.py`;
* `realbench`, the `econ` chain, and `build_options.py` (all four option tables rebuild byte-identically).

`refresh.sh`, `wsj_fetch.sh`, `yf_retry.sh`, `build_bars.py` and the econ parsers need network downloads or the raw
files, so they were only syntax-checked.
