"""Single-run smoke test: print key metrics per policy at defaults."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.model import Config, run_once  # noqa: E402

KEYS = [
    "orders_per_day", "refused_per_day", "unsafe_orders_per_day",
    "unsafe_shares_per_day", "drift_share_hours_per_day", "data_calls_per_day",
    "freezes_per_day", "false_freezes_per_day", "availability",
    "det_lat_median", "detected_fraction", "censored_events",
    "duplicates_per_day", "overshoot_per_day",
]


def show(cfg: Config):
    t0 = time.time()
    r = run_once(cfg)
    dt = time.time() - t0
    print(f"\n=== {r['policy']}  (fault_x={cfg.fault_x}, {dt:.2f}s) ===")
    for k in KEYS:
        v = r[k]
        if isinstance(v, float):
            print(f"  {k:32s} {v:10.3f}")
        else:
            print(f"  {k:32s} {v}")


if __name__ == "__main__":
    base = Config(seed=7)
    show(replace(base, policy="P0"))
    show(replace(base, policy="P1"))
    show(replace(base, policy="P2", tau=900.0))
    show(replace(base, policy="P3", tau=30.0))
    show(replace(base, policy="P3", tau=30.0, fresh_guard=True))
    show(replace(base, policy="P3", tau=30.0, retry_mode="delta", fault_x=2.0))
    show(replace(base, policy="P3", tau=30.0, retry_mode="target", fault_x=2.0))
    show(replace(base, policy="P0", fault_x=4.0))
    show(replace(base, policy="P3", fault_x=4.0))
