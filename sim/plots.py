"""Generate IEEE-sized figures and a numeric summary from results/results.csv."""

from __future__ import annotations

import csv
import math
import os
from collections import defaultdict

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(HERE, "results", "results.csv")
FIGDIR = os.path.join(HERE, "figures")
SUMMARY = os.path.join(HERE, "results", "summary.txt")

plt.rcParams.update({
    "font.family": "serif",
    "font.size": 8,
    "axes.titlesize": 8,
    "axes.labelsize": 8,
    "legend.fontsize": 7,
    "xtick.labelsize": 7,
    "ytick.labelsize": 7,
    "lines.linewidth": 1.1,
    "lines.markersize": 3.5,
    "figure.dpi": 200,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.02,
    "axes.grid": True,
    "grid.linewidth": 0.3,
    "grid.alpha": 0.4,
})

STYLE = {
    "P0":       dict(color="#c0392b", marker="o", ls="-",  label="P0 none"),
    "P1":       dict(color="#7f8c8d", marker="s", ls="--", label="P1 per-order"),
    "P2(900)":  dict(color="#e67e22", marker="^", ls="-.", label="P2 periodic (900 s)"),
    "P3(30)":   dict(color="#2c3e50", marker="D", ls="-",  label="P3 freeze-floor (30 s)"),
}


def load():
    rows = []
    with open(RESULTS) as f:
        for r in csv.DictReader(f):
            for k, v in r.items():
                if k in ("experiment", "policy", "base_policy", "retry_mode"):
                    continue
                r[k] = float(v) if v not in ("", "None") else None
            rows.append(r)
    return rows


def agg(rows, metric):
    """mean and 95% CI half-width over seeds; ignores None."""
    xs = [r[metric] for r in rows if r[metric] is not None]
    if not xs:
        return None, None, 0
    m = float(np.mean(xs))
    ci = 1.96 * float(np.std(xs, ddof=1)) / math.sqrt(len(xs)) if len(xs) > 1 else 0.0
    return m, ci, len(xs)


def sel(rows, **kv):
    out = rows
    for k, v in kv.items():
        out = [r for r in out if r[k] == v]
    return out


# ----------------------------------------------------------------------

def fig_intensity(rows):
    data = sel(rows, experiment="intensity")
    xs = sorted({r["fault_x"] for r in data})
    fig, axes = plt.subplots(2, 1, figsize=(3.45, 3.6), sharex=True)
    for pol, st in STYLE.items():
        ms, cs, ds, dcs = [], [], [], []
        for x in xs:
            m, c, _ = agg(sel(data, policy=pol, fault_x=x), "unsafe_shares_per_day")
            ms.append(m); cs.append(c)
            m2, c2, _ = agg(sel(data, policy=pol, fault_x=x),
                            "drift_share_hours_per_day")
            ds.append(m2); dcs.append(c2)
        axes[0].errorbar(xs, ms, yerr=cs, **st)
        axes[1].errorbar(xs, ds, yerr=dcs, **st)
    axes[0].set_yscale("symlog", linthresh=1.0)
    axes[0].set_ylabel("Unsafe executions\n(shares/day)")
    axes[1].set_yscale("log")
    axes[1].set_ylabel("Unexplained drift\n(share-hours/day)")
    axes[1].set_xlabel("Fault-intensity multiplier")
    axes[1].set_xscale("log", base=2)
    axes[1].set_xticks(xs)
    axes[1].set_xticklabels([f"{x:g}" for x in xs])
    axes[0].legend(ncol=2, columnspacing=0.8, handlelength=1.6, loc="upper left")
    fig.savefig(os.path.join(FIGDIR, "fig_intensity.pdf"))
    plt.close(fig)


def fig_tradeoff(rows):
    data = sel(rows, experiment="tradeoff")
    fig, ax = plt.subplots(figsize=(3.45, 2.5))

    def point(policy, **stkw):
        d = sel(data, policy=policy)
        x, xc, _ = agg(d, "data_calls_per_day")
        y, yc, _ = agg(d, "unsafe_shares_per_day")
        return x, y, xc, yc

    # P2 and P3 tau curves
    for base, color, marker in (("P2", "#e67e22", "^"), ("P3", "#2c3e50", "D")):
        taus = sorted({r["tau"] for r in data
                       if r["base_policy"] == base and not r["fresh_guard"]})
        pts = []
        for tau in taus:
            d = [r for r in data if r["base_policy"] == base and r["tau"] == tau
                 and not r["fresh_guard"]]
            x, _, _ = agg(d, "data_calls_per_day")
            y, _, _ = agg(d, "unsafe_shares_per_day")
            pts.append((x, y, tau))
        pts.sort()
        ax.plot([p[0] for p in pts], [p[1] for p in pts], ls="-", color=color,
                marker=marker, label=f"{base} ($\\tau$ sweep)", zorder=3)
        for x, y, tau in pts:
            ax.annotate(f"{int(tau)}s", (x, y), textcoords="offset points",
                        xytext=(3, 4), fontsize=6, color=color)

    x, y, _, _ = point("P0")
    ax.scatter([max(x, 0.6)], [y], color="#c0392b", marker="o", zorder=4,
               label="P0 none")
    ax.annotate("P0", (max(x, 0.6), y), textcoords="offset points",
                xytext=(4, -2), fontsize=6, color="#c0392b")
    x, y, _, _ = point("P1")
    ax.scatter([x], [y], color="#7f8c8d", marker="s", zorder=4,
               label="P1 per-order")
    x, y, _, _ = point("P3(30)+fg")
    ax.scatter([x], [y], color="#16a085", marker="*", s=40, zorder=4,
               label="P3+fresh guard (30 s)")

    ax.set_xscale("log")
    ax.set_yscale("symlog", linthresh=0.1)
    ax.set_ylim(bottom=0, top=1500)
    ax.set_xlabel("Reconciliation overhead (data calls/day)")
    ax.set_ylabel("Unsafe executions (shares/day)")
    ax.legend(loc="upper right", handlelength=1.4)
    fig.savefig(os.path.join(FIGDIR, "fig_tradeoff.pdf"))
    plt.close(fig)


def fig_latency(rows):
    data = sel(rows, experiment="tradeoff")
    fig, ax = plt.subplots(figsize=(3.45, 2.2))
    for base, color, marker in (("P2", "#e67e22", "^"), ("P3", "#2c3e50", "D")):
        taus = sorted({r["tau"] for r in data
                       if r["base_policy"] == base and not r["fresh_guard"]})
        ms, cs = [], []
        for tau in taus:
            d = [r for r in data if r["base_policy"] == base and r["tau"] == tau
                 and not r["fresh_guard"]]
            m, c, _ = agg(d, "det_lat_median")
            ms.append(m); cs.append(c)
        ax.errorbar(taus, ms, yerr=cs, color=color, marker=marker, ls="-",
                    label=f"{base} (interval $\\tau$)")
    m1, c1, _ = agg(sel(data, policy="P1"), "det_lat_median")
    ax.axhline(m1, color="#7f8c8d", ls="--", lw=1)
    ax.annotate(f"P1 per-order ({m1:.0f} s)", (20, m1 * 1.15), fontsize=6.5,
                color="#7f8c8d")
    ax.plot([], [], color="#7f8c8d", ls="--", label="P1 per-order")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Reconciliation interval $\\tau$ (s)")
    ax.set_ylabel("Median detection\nlatency (s)")
    ax.legend(loc="upper left", handlelength=1.4)
    fig.savefig(os.path.join(FIGDIR, "fig_latency.pdf"))
    plt.close(fig)


def fig_staleness(rows):
    data = sel(rows, experiment="staleness")
    xs = sorted({r["poll_staleness"] for r in data})
    ff, ffc, av, avc = [], [], [], []
    for x in xs:
        d = sel(data, poll_staleness=x)
        m, c, _ = agg(d, "false_freezes_per_day"); ff.append(m); ffc.append(c)
        m, c, _ = agg(d, "availability"); av.append(m * 100); avc.append(c * 100)
    fig, ax1 = plt.subplots(figsize=(3.45, 2.2))
    ax1.errorbar(xs, ff, yerr=ffc, color="#2c3e50", marker="D", ls="-",
                 label="False freezes/day")
    ax1.set_xlabel("Position-read staleness (s)")
    ax1.set_ylabel("False freezes/day")
    ax1.set_xscale("log")
    ax2 = ax1.twinx()
    ax2.errorbar(xs, av, yerr=avc, color="#16a085", marker="s", ls="--",
                 label="Availability")
    ax2.set_ylabel("Agent availability (%)")
    ax2.grid(False)
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="center left")
    fig.savefig(os.path.join(FIGDIR, "fig_staleness.pdf"))
    plt.close(fig)


def fig_scale(rows):
    data = sel(rows, experiment="scale")
    xs = sorted({r["n_agents"] for r in data})
    fig, axes = plt.subplots(2, 1, figsize=(3.45, 3.6), sharex=True)
    for pol, st in STYLE.items():
        cs_, cc_, us_, uc_ = [], [], [], []
        for x in xs:
            d = sel(data, policy=pol, n_agents=x)
            m, c, _ = agg(d, "data_calls_per_day"); cs_.append(m); cc_.append(c)
            m, c, _ = agg(d, "unsafe_shares_per_day"); us_.append(m); uc_.append(c)
        axes[0].errorbar(xs, cs_, yerr=cc_, **st)
        axes[1].errorbar(xs, us_, yerr=uc_, **st)
    axes[0].set_yscale("log")
    axes[0].set_ylabel("Data calls/day")
    axes[1].set_yscale("symlog", linthresh=1.0)
    axes[1].set_ylim(bottom=0)
    axes[1].set_ylabel("Unsafe executions\n(shares/day)")
    axes[1].set_xlabel("Number of agents on one account")
    axes[0].legend(ncol=2, columnspacing=0.8, handlelength=1.6, loc="upper left")
    fig.savefig(os.path.join(FIGDIR, "fig_scale.pdf"))
    plt.close(fig)


# ----------------------------------------------------------------------

def summary(rows):
    out = []

    def line(s=""):
        out.append(s)

    def stat(d, metric, fmt="{:.2f}"):
        m, c, n = agg(d, metric)
        if m is None:
            return "n/a"
        return (fmt + " +- " + fmt + " (n={})").format(m, c, n)

    line("=== E1 intensity, x=1 (baseline) ===")
    for pol in STYLE:
        d = sel(rows, experiment="intensity", policy=pol, fault_x=1.0)
        line(f"[{pol}]")
        for met in ("unsafe_orders_per_day", "unsafe_shares_per_day",
                    "drift_share_hours_per_day", "data_calls_per_day",
                    "det_lat_median", "availability", "false_freezes_per_day",
                    "freezes_per_day", "refused_per_day", "detected_fraction",
                    "orders_per_day"):
            line(f"    {met:30s} {stat(d, met, '{:.3f}')}")
    line()
    line("=== E1 intensity, x=4 (stress) ===")
    for pol in STYLE:
        d = sel(rows, experiment="intensity", policy=pol, fault_x=4.0)
        line(f"[{pol}] unsafe_shares={stat(d, 'unsafe_shares_per_day')} "
             f"drift={stat(d, 'drift_share_hours_per_day')}")
    line()
    line("=== E2 tradeoff points ===")
    pols = sorted({r["policy"] for r in sel(rows, experiment="tradeoff")})
    for pol in pols:
        d = sel(rows, experiment="tradeoff", policy=pol)
        line(f"[{pol:12s}] calls={stat(d, 'data_calls_per_day', '{:.0f}')} "
             f"unsafe={stat(d, 'unsafe_shares_per_day')} "
             f"detlat={stat(d, 'det_lat_median', '{:.0f}')} "
             f"avail={stat(d, 'availability', '{:.4f}')}")
    line()
    line("=== E3 staleness (P3) ===")
    for st_ in sorted({r["poll_staleness"] for r in sel(rows, experiment="staleness")}):
        d = sel(rows, experiment="staleness", poll_staleness=st_)
        line(f"[stale={st_:5.1f}] false_fr={stat(d, 'false_freezes_per_day')} "
             f"avail={stat(d, 'availability', '{:.4f}')} "
             f"unsafe={stat(d, 'unsafe_shares_per_day')}")
    line()
    line("=== E4 scale ===")
    for n in sorted({r["n_agents"] for r in sel(rows, experiment="scale")}):
        for pol in STYLE:
            d = sel(rows, experiment="scale", policy=pol, n_agents=n)
            line(f"[N={int(n):2d} {pol:8s}] calls={stat(d, 'data_calls_per_day', '{:.0f}')} "
                 f"unsafe={stat(d, 'unsafe_shares_per_day')}")
    line()
    line("=== E5 ablation (fault_x=2) ===")
    for pol in sorted({r["policy"] for r in sel(rows, experiment="ablation")}):
        d = sel(rows, experiment="ablation", policy=pol)
        line(f"[{pol:12s}] dup={stat(d, 'duplicates_per_day')} "
             f"overshoot={stat(d, 'overshoot_per_day')} "
             f"unsafe={stat(d, 'unsafe_shares_per_day')} "
             f"calls={stat(d, 'data_calls_per_day', '{:.0f}')} "
             f"avail={stat(d, 'availability', '{:.4f}')}")
    line()
    line("=== E5b component ablation ===")
    for st_ in sorted({r["poll_staleness"] for r in sel(rows, experiment="components")}):
        for pol in sorted({r["policy"] for r in sel(rows, experiment="components",
                                                    poll_staleness=st_)}):
            d = sel(rows, experiment="components", policy=pol, poll_staleness=st_)
            line(f"[stale={st_:4.1f} {pol:10s}] unsafe={stat(d, 'unsafe_shares_per_day', '{:.3f}')} "
                 f"avail={stat(d, 'availability', '{:.4f}')} "
                 f"freezes={stat(d, 'freezes_per_day')} "
                 f"refused={stat(d, 'refused_per_day')} "
                 f"writedowns={stat(d, 'writedowns_per_day', '{:.0f}')}")
    line()
    line("=== writedowns charged to agent books (intensity x=1) ===")
    for pol in STYLE:
        d = sel(rows, experiment="intensity", policy=pol, fault_x=1.0)
        line(f"[{pol:12s}] writedowns={stat(d, 'writedowns_per_day')}")
    line()
    line("=== E6 bursty vs memoryless (matched avg rates) ===")
    for pol in sorted({r["policy"] for r in sel(rows, experiment="bursty")}):
        d = sel(rows, experiment="bursty", policy=pol)
        line(f"[{pol:12s}] unsafe={stat(d, 'unsafe_shares_per_day')} "
             f"drift={stat(d, 'drift_share_hours_per_day')} "
             f"detlat={stat(d, 'det_lat_median', '{:.0f}')} "
             f"avail={stat(d, 'availability', '{:.4f}')} "
             f"freezes={stat(d, 'freezes_per_day')}")
    line()
    line("=== E8 activity-gated detection (wake_mean sweep) ===")
    for wm in sorted({r["wake_mean"] for r in sel(rows, experiment="activity")}):
        for pol in sorted({r["policy"] for r in sel(rows, experiment="activity",
                                                    wake_mean=wm)}):
            d = sel(rows, experiment="activity", policy=pol, wake_mean=wm)
            line(f"[wake={int(wm):6d}s {pol:8s}] "
                 f"detlat_med={stat(d, 'det_lat_median', '{:.0f}')} "
                 f"detlat_p90={stat(d, 'det_lat_p90', '{:.0f}')} "
                 f"drift={stat(d, 'drift_share_hours_per_day')} "
                 f"detected={stat(d, 'detected_fraction', '{:.3f}')} "
                 f"calls={stat(d, 'data_calls_per_day', '{:.0f}')} "
                 f"unsafe={stat(d, 'unsafe_shares_per_day', '{:.3f}')}")
    line()
    line("=== E7 robustness grid (P0 vs P3(30)) ===")
    worst_p3, worst_cfg, min_ratio = 0.0, "", float("inf")
    for exp in sorted({r["experiment"] for r in rows
                       if str(r["experiment"]).startswith("robust:")}):
        d0 = sel(rows, experiment=exp, base_policy="P0")
        d3 = sel(rows, experiment=exp, base_policy="P3")
        m0, _, _ = agg(d0, "unsafe_shares_per_day")
        m3, _, _ = agg(d3, "unsafe_shares_per_day")
        ratio = (m0 / m3) if m3 else float("inf")
        if m3 is not None and m3 > worst_p3:
            worst_p3, worst_cfg = m3, exp
        if ratio < min_ratio:
            min_ratio, min_cfg = ratio, exp
        line(f"[{exp:28s}] P0={m0:8.1f}  P3={m3:6.3f}  ratio={ratio:9.1f}")
    line(f"worst P3 unsafe: {worst_p3:.3f} shares/day at {worst_cfg}; "
         f"min P0/P3 ratio: {min_ratio:.0f}x at {min_cfg}")

    text = "\n".join(out)
    with open(SUMMARY, "w") as f:
        f.write(text + "\n")
    print(text)


if __name__ == "__main__":
    os.makedirs(FIGDIR, exist_ok=True)
    rows = load()
    print(f"loaded {len(rows)} rows")
    fig_intensity(rows)
    fig_tradeoff(rows)
    fig_latency(rows)
    fig_staleness(rows)
    fig_scale(rows)
    summary(rows)
    print("figures written to", FIGDIR)
