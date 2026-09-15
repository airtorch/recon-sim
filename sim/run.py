"""Experiment sweeps for the reconciliation simulator.

Usage:  python -m sim.run [--quick]

Writes results/results.csv with one row per (experiment, config, seed).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.model import Config, run_once  # noqa: E402

RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "results", "results.csv")


def _job(args):
    experiment, cfg = args
    row = run_once(cfg)
    row["experiment"] = experiment
    return row


def build_jobs(seeds: int, seeds_scale: int):
    jobs = []
    base = Config()

    def add(experiment: str, cfg: Config, n_seeds: int):
        for k in range(n_seeds):
            jobs.append((experiment, replace(cfg, seed=1000 + k)))

    # E1: policy x fault intensity
    for x in (0.5, 1.0, 2.0, 4.0):
        add("intensity", replace(base, policy="P0", fault_x=x), seeds)
        add("intensity", replace(base, policy="P1", fault_x=x), seeds)
        add("intensity", replace(base, policy="P2", tau=900.0, fault_x=x), seeds)
        add("intensity", replace(base, policy="P3", tau=30.0, fault_x=x), seeds)

    # E2: safety-overhead trade-off at baseline intensity
    add("tradeoff", replace(base, policy="P0"), seeds)
    add("tradeoff", replace(base, policy="P1"), seeds)
    for tau in (60.0, 300.0, 900.0, 3600.0):
        add("tradeoff", replace(base, policy="P2", tau=tau), seeds)
    for tau in (15.0, 30.0, 120.0, 300.0, 900.0, 3600.0):
        add("tradeoff", replace(base, policy="P3", tau=tau), seeds)
    add("tradeoff", replace(base, policy="P3", tau=30.0, fresh_guard=True), seeds)

    # E3: staleness sweep (false freezes / availability), P3
    for st in (0.5, 2.0, 10.0, 30.0):
        add("staleness", replace(base, policy="P3", tau=30.0,
                                 poll_staleness=st), seeds)

    # E4: scaling with number of agents
    for n in (2, 5, 10, 20, 50):
        add("scale", replace(base, policy="P0", n_agents=n), seeds_scale)
        add("scale", replace(base, policy="P1", n_agents=n), seeds_scale)
        add("scale", replace(base, policy="P2", tau=900.0, n_agents=n), seeds_scale)
        add("scale", replace(base, policy="P3", tau=30.0, n_agents=n), seeds_scale)

    # E5: retry-mode ablation under elevated fault intensity
    add("ablation", replace(base, policy="P3", tau=30.0, fault_x=2.0,
                            retry_mode="target"), seeds)
    add("ablation", replace(base, policy="P3", tau=30.0, fault_x=2.0,
                            retry_mode="delta"), seeds)
    add("ablation", replace(base, policy="P3", tau=30.0, fault_x=2.0,
                            fresh_guard=True), seeds)

    # E5b: component ablation (freeze floors vs sell guard), including the
    # stale-observation regime where the freeze matters
    for st_ in (2.0, 10.0, 30.0):
        add("components", replace(base, policy="P3", tau=30.0,
                                  poll_staleness=st_), seeds)
        add("components", replace(base, policy="P3", tau=30.0,
                                  poll_staleness=st_,
                                  freeze_floors=False), seeds)
    add("components", replace(base, policy="P3", tau=30.0,
                              sell_guard=False), seeds)

    # E6: bursty vs memoryless incident faults (matched long-run rates)
    for pol, tau in (("P0", 30.0), ("P1", 30.0), ("P2", 900.0), ("P3", 30.0)):
        add("bursty", replace(base, policy=pol, tau=tau, bursty=False), seeds)
        add("bursty", replace(base, policy=pol, tau=tau, bursty=True), seeds)

    # E8: activity-gated detection. P1 reconciles only when orders flow, so
    # its detection latency is gated on agent activity; P2/P3 detect within
    # tau regardless. Sweep the mean decision interval from the baseline
    # (15 min) to a dormant fleet (24 h).
    for wm in (900.0, 3600.0, 14400.0, 86400.0):
        add("activity", replace(base, policy="P1", wake_mean=wm), seeds)
        add("activity", replace(base, policy="P2", tau=900.0, wake_mean=wm),
            seeds)
        add("activity", replace(base, policy="P3", tau=30.0, wake_mean=wm),
            seeds)

    # E7: fault-model robustness, one-factor-at-a-time wide sweeps (P0 vs P3)
    grid: list[tuple[str, Config]] = []
    for v in (0.03, 0.12, 0.30, 0.60):
        grid.append((f"p_partial={v}", replace(base, p_partial=v)))
    for v in (0.01, 0.05, 0.15, 0.30):
        grid.append((f"p_lost={v}", replace(base, p_lost=v)))
    for v in (0.01, 0.03, 0.10, 0.20):
        grid.append((f"p_api={v}", replace(base, p_api=v)))
    for v in (1800.0, 7200.0, 14400.0, 28800.0):
        grid.append((f"oob_interval={v}", replace(base, oob_interval=v)))
    for v in (2.0, 8.0, 30.0, 120.0):
        grid.append((f"report_lag={v}", replace(base, report_lag_mean=v)))
    for name, g in grid:
        add(f"robust:{name}", replace(g, policy="P0"), seeds_scale)
        add(f"robust:{name}", replace(g, policy="P3", tau=30.0), seeds_scale)

    return jobs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="3 seeds, for smoke tests")
    args = ap.parse_args()
    seeds = 3 if args.quick else 100
    seeds_scale = 3 if args.quick else 15

    jobs = build_jobs(seeds, seeds_scale)
    print(f"running {len(jobs)} simulations...")
    rows = []
    with ProcessPoolExecutor() as pool:
        for i, row in enumerate(pool.map(_job, jobs, chunksize=4)):
            rows.append(row)
            if (i + 1) % 100 == 0:
                print(f"  {i + 1}/{len(jobs)}")

    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    cols = ["experiment"] + [c for c in rows[0] if c != "experiment"]
    with open(RESULTS, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows -> {RESULTS}")


if __name__ == "__main__":
    main()
