# recon-sim

A discrete-event fault-injection simulator for **state reconciliation in
multi-agent brokerage trading**: N autonomous agents share one brokerage
account, and the platform's ledger must be kept consistent with the venue of
record under partial fills, lost/delayed fill reports, API timeouts, client
retries, agent crashes, and out-of-band trades by the account holder.

This code accompanies a paper currently under double-blind review.
`PROOFS.md` contains the full case-analysis proofs of the paper's
Propositions 1–3 and the supplementary observation-floor derivation.

## Reconciliation policies

| Policy | Description |
|--------|-------------|
| P0 | No reconciliation; trusts the local ledger, books unresolved orders optimistically |
| P1 | Reconcile-per-order; polls positions before every order, refuses orders on instruments with unresolved shortfalls |
| P2(tau) | Periodic reconciliation every tau seconds; realigns after an investigation delay, never halts trading |
| P3(tau) | Proposed mechanism: P2's detection plus per-agent claims, freeze floors, and a sell guard |

All policies use target-based idempotent execution unless configured
otherwise (`retry_mode="delta"` reproduces naive delta retries).

## Requirements

Python 3.10+. The simulator itself (`sim/model.py`, `sim/run.py`,
`sim/smoke.py`) is pure standard library. Plotting (`sim/plots.py`) needs
`numpy` and `matplotlib`:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Reproducing the paper's results

```bash
# ~7,800 simulation runs, a few minutes on a laptop (all cores):
.venv/bin/python sim/run.py            # writes results/results.csv

# figures (PDF) and the aggregate text summary:
.venv/bin/python sim/plots.py          # writes figures/*.pdf, results/summary.txt

# quick sanity check of all four policies (single runs):
.venv/bin/python sim/smoke.py
```

`sim/run.py --quick` runs a 3-seed smoke version of every experiment.

## Experiments

- **E1 `intensity`** — policy x fault-intensity sweep (unsafe executions, drift).
- **E2 `tradeoff`** — safety versus reconciliation overhead as the interval tau sweeps.
- **E3 `staleness`** — false freezes and availability versus poll staleness (P3).
- **E4 `scale`** — scaling with the number of agents.
- **E5 `ablation`** — target-based versus delta-retry execution; sell-guard variant.
- **E6 `bursty`** — two-state Markov-modulated (bursty) faults versus memoryless
  faults at matched long-run rates.
- **E7 `robust:*`** — one-factor-at-a-time robustness sweeps over every fault
  parameter (partial-fill probability, report-loss probability, API-timeout
  probability, out-of-band trade rate, report lag).
- **E8 `activity`** — activity-gated detection: sweeps the mean agent decision
  interval from 15 minutes to 24 hours; per-order reconciliation (P1) observes
  the account only while orders flow, so its detection latency degrades with
  dormancy while periodic policies stay bounded by tau.
- **E10 `trace:*`** — production schedule replay: agent decision times come
  from real (anonymized) production schedules in `sim/traces/wake_times.csv`
  instead of Poisson clocks. Two ten-agent cohorts over a 5-day window: an
  `active` cohort (top decile by activity) and a `median` cohort (ranks
  45-54 of 98; one decision per ~14.5 h, longest fleet-wide gap ~20 h). The
  CSV contains only cohort, agent index, and seconds offsets — no
  identifiers or absolute dates.

Baseline parameters are in `Config` in `sim/model.py`; every experiment
configuration is defined in `build_jobs()` in `sim/run.py`. All randomness is
seeded; results are exactly reproducible.

## Ground truth and metrics

The simulator tracks true per-agent allocations at all times, so unsafe
executions (fills that encroach on other agents' recognized holdings),
unexplained ledger drift (share-hours), detection latency, availability, and
API overhead (data calls) are measured exactly rather than estimated. See the
module docstring in `sim/model.py` for the entity model.

## License

MIT (see `LICENSE`).
