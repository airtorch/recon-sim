"""Discrete-event fault-injection simulator for multi-agent brokerage
state reconciliation.

Entities
--------
Venue      : ground-truth aggregate positions of one brokerage account,
             with an order lifecycle (accept / partial fill / reject),
             fill latency, fill-report lag/loss, API timeouts, and
             out-of-band (OOB) trades by the account holder.
Platform   : the agent platform's ledger: per-agent position books
             ("claims"), an unclaimed-pool estimate, pending orders, and
             the last polled venue snapshot. Implements the
             reconciliation policies.
Agents     : N strategy agents waking on independent schedules and
             declaring target positions on their symbols.

Ground truth vs. ledger
-----------------------
The simulator tracks true per-agent allocations t[i][s] and the account
holder's pool h[s]; the invariant sum_i t[i][s] + h[s] == venue.pos[s]
holds at all times. The platform only sees its own books b[i][s], its
unclaimed-pool estimate hhat[s], and (via polls) venue positions.

Policies
--------
P0 : no reconciliation. Trust the local ledger; never poll positions.
     Unresolved orders are optimistically booked at intended quantity.
P1 : reconcile-per-order. Poll positions before every order; refuse the
     order if its symbol has an unresolved shortfall.
P2 : periodic reconciliation every tau seconds; discrepancies are
     realigned after a fixed investigation delay; trading continues.
P3 : continuous reconciliation (small tau) + freeze floors: on an
     unexplained shortfall the affected symbols are frozen (agents
     refuse orders) until realignment completes; sells are additionally
     guarded against the last reconciled floor. (Proposed mechanism.)

Retry semantics (ablation)
--------------------------
retry_mode="target": an order that times out ambiguously stays tracked
     as in-flight; re-issuing the target recomputes a zero delta, so no
     duplicate is ever submitted (idempotent execution).
retry_mode="delta": the client re-submits the same signed delta after a
     timeout, duplicating the trade whenever the first submission was
     actually accepted.
"""

from __future__ import annotations

import heapq
import math
import random
from bisect import bisect_right
from dataclasses import dataclass, field


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

@dataclass
class Config:
    # workload
    horizon: float = 5 * 86400.0      # 5 simulated days, continuous trading
    n_agents: int = 10
    n_symbols: int = 8
    syms_per_agent: int = 2
    init_book: int = 100              # shares per agent per symbol
    init_human: int = 150             # account holder's unclaimed pool per symbol
    wake_mean: float = 900.0          # mean seconds between an agent's decisions
    target_sigma: float = 30.0        # stddev of target changes (shares)

    # faults (base rates; scaled by fault_x)
    oob_interval: float = 7200.0      # mean seconds between out-of-band trades
    oob_base: float = 30.0            # OOB trade size = base + Exp(oob_exp)
    oob_exp: float = 60.0
    p_partial: float = 0.12           # probability an order fills partially
    partial_lo: float = 0.30
    partial_hi: float = 0.90
    p_lost: float = 0.05              # probability a fill report is lost
    p_api: float = 0.03               # probability an order submission times out
    p_accept_after_timeout: float = 0.5   # P(venue accepted | timeout)

    # latencies
    fill_med: float = 2.0             # median fill latency (lognormal), seconds
    fill_sigma: float = 1.0
    fill_cap: float = 120.0
    report_lag_mean: float = 8.0      # mean fill-report delivery lag (exponential)
    poll_staleness: float = 2.0       # position polls reflect state this many seconds ago
    watchdog: float = 300.0           # unresolved-order watchdog timeout
    retry_delay: float = 10.0         # client retry delay after ambiguous timeout
    realign_time: float = 120.0       # investigation delay before realignment applies

    # policy
    policy: str = "P3"                # P0 | P1 | P2 | P3
    tau: float = 30.0                 # reconciliation interval (P2, P3)
    fresh_guard: bool = False         # P3 variant: poll before every sell
    freeze_floors: bool = True        # P3 component: freeze on shortfall
    sell_guard: bool = True           # P3 component: pre-sell floor check
    retry_mode: str = "target"        # target | delta

    # burstiness (two-state modulated fault process; long-run average rates
    # are preserved exactly: burst_frac*burst_mult + (1-burst_frac)*quiet_mult
    # == 1, so bursty and memoryless runs are directly comparable).
    # Modulates the incident-driven faults (report loss, API timeouts, OOB
    # trades); partial fills are liquidity-driven and stay memoryless.
    bursty: bool = False
    burst_frac: float = 0.10          # long-run fraction of time in burst state
    burst_mult: float = 6.0           # fault-rate multiplier inside a burst
    burst_mean: float = 1800.0        # mean burst duration, seconds

    # experiment
    fault_x: float = 1.0              # fault-intensity multiplier
    seed: int = 0

    def quiet_mult(self) -> float:
        return (1.0 - self.burst_frac * self.burst_mult) / (1.0 - self.burst_frac)

    def quiet_mean(self) -> float:
        return self.burst_mean * (1.0 - self.burst_frac) / self.burst_frac

    # effective (scaled) fault parameters
    def eff_oob_interval(self) -> float:
        return self.oob_interval / self.fault_x

    def eff_p_partial(self) -> float:
        return min(0.6, self.p_partial * self.fault_x)

    def eff_p_lost(self) -> float:
        return min(0.5, self.p_lost * self.fault_x)

    def eff_p_api(self) -> float:
        return min(0.3, self.p_api * self.fault_x)

    def label(self) -> str:
        if self.policy in ("P2", "P3"):
            lab = f"{self.policy}({int(self.tau)})"
        else:
            lab = self.policy
        if self.fresh_guard:
            lab += "+fg"
        if self.policy == "P3" and not self.freeze_floors:
            lab += "-fz"
        if self.policy == "P3" and not self.sell_guard:
            lab += "-sg"
        if self.retry_mode == "delta":
            lab += "+dr"
        if self.bursty:
            lab += "+b"
        return lab


# ----------------------------------------------------------------------
# Orders and drift events
# ----------------------------------------------------------------------

@dataclass
class Order:
    oid: int
    agent: int
    sym: int
    delta: int                 # signed intended quantity
    t_submit: float
    ambiguous: bool = False    # submission timed out; acceptance unknown to platform
    accepted: bool = False     # ground truth
    filled: int = 0            # signed actual fill (ground truth, set at fill time)
    fill_done: bool = False
    booked: bool = False
    lost: bool = False         # fill report lost


@dataclass
class DriftEvent:
    t: float
    detected_at: float | None = None


# ----------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------

@dataclass
class Metrics:
    orders_submitted: int = 0
    orders_filled: int = 0
    refused: int = 0
    unsafe_orders: int = 0
    unsafe_shares: int = 0
    duplicates: int = 0
    overshoot_shares: int = 0
    data_calls: int = 0            # position polls + order-status queries
    freezes: int = 0
    false_freezes: int = 0
    realigns: int = 0
    writedown_shares: int = 0      # realignment cuts charged to agent books
    drift_integral: float = 0.0    # share-seconds of unexplained ledger-venue divergence
    frozen_integral: float = 0.0   # agent-seconds frozen
    det_lats: list = field(default_factory=list)    # drift detection latencies (s)
    cons_lats: list = field(default_factory=list)   # time-to-consistency (s)
    oob_events: int = 0
    censored_events: int = 0       # OOB drift events never detected by horizon end


# ----------------------------------------------------------------------
# Simulator
# ----------------------------------------------------------------------

class Sim:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self.t = 0.0
        self._seq = 0
        self._oid = 0
        self.events: list = []       # heap of (time, seq, kind, payload)
        self.m = Metrics()

        M, N = cfg.n_symbols, cfg.n_agents
        # agent -> symbols traded (round-robin, adjacent agents share symbols)
        self.agent_syms = [
            [(i + k) % M for k in range(cfg.syms_per_agent)] for i in range(N)
        ]

        # ground truth
        self.pos = [0] * M                          # venue aggregate positions
        self.t_alloc = [[0] * M for _ in range(N)]  # true per-agent allocations
        self.h = [cfg.init_human] * M               # account holder's pool
        for i in range(N):
            for s in self.agent_syms[i]:
                self.t_alloc[i][s] = cfg.init_book
        for s in range(M):
            self.pos[s] = sum(self.t_alloc[i][s] for i in range(N)) + self.h[s]

        # venue position history for stale reads: per symbol (times, values)
        self.hist_t = [[0.0] for _ in range(M)]
        self.hist_v = [[self.pos[s]] for s in range(M)]

        # platform ledger
        self.b = [row[:] for row in self.t_alloc]   # books start consistent
        self.hhat = self.h[:]                       # unclaimed-pool estimate
        self.pending: dict[int, Order] = {}
        self.vhat = self.pos[:]                     # last polled snapshot
        self.frozen_syms: set[int] = set()
        self.realign_pending: set[int] = set()
        self.drift_events: list[list[DriftEvent]] = [[] for _ in range(M)]

        # integration bookkeeping
        self._last_t = 0.0

        # burst-state machine (bursty mode only)
        self.burst = False

        # schedule initial events
        for i in range(N):
            self._push(self._exp(cfg.wake_mean), "wake", i)
        if cfg.bursty:
            # OOB events are generated by thinning a Poisson process running
            # at the burst-state (maximum) rate, which is exact for
            # Markov-modulated arrivals.
            self._push(self._exp(cfg.eff_oob_interval() / cfg.burst_mult),
                       "oob", None)
            self._push(self._exp(cfg.quiet_mean()), "burst_on", None)
        else:
            self._push(self._exp(cfg.eff_oob_interval()), "oob", None)
        if cfg.policy in ("P2", "P3"):
            self._push(cfg.tau, "tick", None)

    # ---------------- event machinery ----------------

    def _push(self, t: float, kind: str, payload):
        self._seq += 1
        heapq.heappush(self.events, (t, self._seq, kind, payload))

    def _exp(self, mean: float) -> float:
        return self.t + self.rng.expovariate(1.0 / mean)

    # ---------------- burst modulation ----------------

    def _fmult(self) -> float:
        """Current multiplier on incident-driven fault rates."""
        if not self.cfg.bursty:
            return 1.0
        return self.cfg.burst_mult if self.burst else self.cfg.quiet_mult()

    def _p_api_now(self) -> float:
        return min(0.3, self.cfg.p_api * self.cfg.fault_x * self._fmult())

    def _p_lost_now(self) -> float:
        return min(0.5, self.cfg.p_lost * self.cfg.fault_x * self._fmult())

    # ---------------- helpers ----------------

    def _pos_change(self, s: int, new: int):
        """Record a venue position change (for stale reads)."""
        self.pos[s] = new
        self.hist_t[s].append(self.t)
        self.hist_v[s].append(new)
        if len(self.hist_t[s]) > 4096:               # prune old history
            self.hist_t[s] = self.hist_t[s][-1024:]
            self.hist_v[s] = self.hist_v[s][-1024:]

    def _pos_at(self, s: int, when: float) -> int:
        idx = bisect_right(self.hist_t[s], when) - 1
        return self.hist_v[s][max(idx, 0)]

    def _poll(self):
        """One position poll (all symbols), subject to read staleness."""
        self.m.data_calls += 1
        when = self.t - self.cfg.poll_staleness
        for s in range(self.cfg.n_symbols):
            self.vhat[s] = self._pos_at(s, when)

    def _pending_on(self, s: int):
        return [o for o in self.pending.values() if o.sym == s]

    def _claims(self, s: int) -> int:
        return sum(self.b[i][s] for i in range(self.cfg.n_agents)) + self.hhat[s]

    def _bounds(self, s: int) -> tuple[int, int]:
        """Explainable range of the venue position given pending orders."""
        lo = hi = self._claims(s)
        for o in self._pending_on(s):
            lo += min(0, o.delta)
            hi += max(0, o.delta)
        return lo, hi

    def _filled_unbooked_not_lost(self, s: int) -> int:
        return sum(
            o.filled
            for o in self._pending_on(s)
            if o.fill_done and not o.booked and not o.lost
        )

    def _drift_now(self) -> int:
        """Current unexplained |ledger - venue| divergence, in shares."""
        d = 0
        for s in range(self.cfg.n_symbols):
            expected = self._claims(s) + self._filled_unbooked_not_lost(s)
            d += abs(expected - self.pos[s])
        return d

    def _frozen_agents(self) -> int:
        if self.cfg.policy != "P3" or not self.frozen_syms:
            return 0
        return sum(
            1
            for i in range(self.cfg.n_agents)
            if any(s in self.frozen_syms for s in self.agent_syms[i])
        )

    def _agent_frozen(self, i: int) -> bool:
        return self.cfg.policy == "P3" and any(
            s in self.frozen_syms for s in self.agent_syms[i]
        )

    # ---------------- detection & realignment ----------------

    def _check_sym(self, s: int):
        """Compare the last snapshot with explainable bounds; act on drift."""
        lo, hi = self._bounds(s)
        v = self.vhat[s]
        if lo <= v <= hi:
            return
        # unexplained discrepancy detected
        for ev in self.drift_events[s]:
            if ev.detected_at is None:
                ev.detected_at = self.t
                self.m.det_lats.append(self.t - ev.t)
        if v > hi:
            # surplus: absorb into the unclaimed pool immediately (harmless)
            self.hhat[s] += v - hi
            for ev in self.drift_events[s]:
                self.m.cons_lats.append(self.t - ev.t)
            self.drift_events[s].clear()
            self.m.realigns += 1
            return
        # shortfall: venue holds fewer shares than claimed
        if s not in self.realign_pending:
            self.realign_pending.add(s)
            self._push(self.t + self.cfg.realign_time, "realign", s)
            if (self.cfg.policy == "P3" and self.cfg.freeze_floors
                    and s not in self.frozen_syms):
                self.frozen_syms.add(s)
                self.m.freezes += 1
                if not self.drift_events[s]:
                    # no real out-of-band change pending: staleness artifact
                    self.m.false_freezes += 1

    def _rebase_truth(self, s: int):
        """After realignment the ledger allocation is authoritative."""
        N = self.cfg.n_agents
        for i in range(N):
            unbooked = sum(
                o.filled
                for o in self._pending_on(s)
                if o.agent == i and o.fill_done and not o.booked
            )
            self.t_alloc[i][s] = self.b[i][s] + unbooked
        self.h[s] = self.pos[s] - sum(self.t_alloc[i][s] for i in range(N))

    def _do_realign(self, s: int):
        self._poll()                                  # fresh snapshot
        lo, hi = self._bounds(s)
        v = self.vhat[s]
        if v > hi:
            self.hhat[s] += v - hi
        elif v < lo:
            need = lo - v
            take = min(self.hhat[s], need)
            self.hhat[s] -= take
            need -= take
            if need > 0:
                # write down agent books pro-rata (largest-remainder rounding)
                books = [self.b[i][s] for i in range(self.cfg.n_agents)]
                total = sum(x for x in books if x > 0)
                if total > 0:
                    cuts = [
                        (need * x) // total if x > 0 else 0 for x in books
                    ]
                    rem = need - sum(cuts)
                    order = sorted(
                        range(len(books)), key=lambda i: -books[i]
                    )
                    for i in order:
                        if rem <= 0:
                            break
                        if books[i] - cuts[i] > 0:
                            cuts[i] += 1
                            rem -= 1
                    for i, c in enumerate(cuts):
                        self.b[i][s] = max(0, self.b[i][s] - c)
                    self.m.writedown_shares += sum(cuts)
                else:
                    self.hhat[s] -= need              # recognized deficit
        self._rebase_truth(s)
        for ev in self.drift_events[s]:
            self.m.cons_lats.append(self.t - ev.t)
            if ev.detected_at is None:
                self.m.det_lats.append(self.t - ev.t)
        self.drift_events[s].clear()
        self.m.realigns += 1
        self.realign_pending.discard(s)
        self.frozen_syms.discard(s)

    # ---------------- order path ----------------

    def _execute(self, i: int, s: int, target: int):
        cfg = self.cfg
        if self._agent_frozen(i):
            self.m.refused += 1
            return
        pend = sum(
            o.delta for o in self.pending.values() if o.agent == i and o.sym == s
        )
        delta = target - self.b[i][s] - pend
        if delta == 0:
            return

        if cfg.policy == "P1":
            self._poll()
            for sym in range(cfg.n_symbols):
                self._check_sym(sym)
            if s in self.realign_pending:
                self.m.refused += 1
                return
            pend = sum(
                o.delta
                for o in self.pending.values()
                if o.agent == i and o.sym == s
            )
            delta = target - self.b[i][s] - pend
            if delta == 0:
                return

        if cfg.policy == "P3" and delta < 0 and cfg.sell_guard:
            if cfg.fresh_guard:
                self._poll()
            self._check_sym(s)
            if s in self.realign_pending or self._agent_frozen(i):
                self.m.refused += 1
                return

        self._submit(i, s, delta)

    def _submit(self, i: int, s: int, delta: int, is_retry: bool = False,
                orig: Order | None = None):
        cfg = self.cfg
        self._oid += 1
        o = Order(self._oid, i, s, delta, self.t)
        self.m.orders_submitted += 1
        if is_retry and orig is not None and orig.accepted:
            self.m.duplicates += 1
            self.m.overshoot_shares += abs(delta)
        if self.rng.random() < self._p_api_now():
            o.ambiguous = True
            o.accepted = self.rng.random() < cfg.p_accept_after_timeout
            if cfg.retry_mode == "delta":
                self._push(self.t + cfg.retry_delay, "retry", o.oid)
        else:
            o.accepted = True
        self.pending[o.oid] = o
        if o.accepted:
            lat = min(
                cfg.fill_cap,
                self.rng.lognormvariate(math.log(cfg.fill_med), cfg.fill_sigma),
            )
            self._push(self.t + lat, "fill", o.oid)
        self._push(self.t + cfg.watchdog, "watchdog", o.oid)

    def _book(self, o: Order, qty: int):
        self.b[o.agent][o.sym] += qty
        o.booked = True
        self.pending.pop(o.oid, None)

    # ---------------- event handlers ----------------

    def _on_fill(self, o: Order):
        cfg = self.cfg
        s = o.sym
        if o.delta > 0:                                  # buy
            f = o.delta
            if self.rng.random() < cfg.eff_p_partial():
                f = max(1, math.ceil(f * self.rng.uniform(cfg.partial_lo,
                                                          cfg.partial_hi)))
            self._pos_change(s, self.pos[s] + f)
            self.t_alloc[o.agent][s] += f
            o.filled = f
        else:                                            # sell
            qty = -o.delta
            base = min(qty, self.pos[s])
            if base > 0 and self.rng.random() < cfg.eff_p_partial():
                base = max(1, math.ceil(base * self.rng.uniform(cfg.partial_lo,
                                                                cfg.partial_hi)))
            f = base
            if f > 0:
                others = sum(
                    max(self.t_alloc[j][s], 0)
                    for j in range(cfg.n_agents)
                    if j != o.agent
                )
                enc_before = max(0, others - self.pos[s])
                self._pos_change(s, self.pos[s] - f)
                enc_after = max(0, others - self.pos[s])
                excess = enc_after - enc_before
                if excess > 0:
                    self.m.unsafe_orders += 1
                    self.m.unsafe_shares += excess
                self.t_alloc[o.agent][s] -= f
            o.filled = -f
        o.fill_done = True
        if o.filled != 0:
            self.m.orders_filled += 1
        if self.rng.random() < self._p_lost_now():
            o.lost = True                                # report never arrives
        else:
            self._push(self.t + self.rng.expovariate(1.0 / cfg.report_lag_mean),
                       "report", o.oid)

    def _on_report(self, o: Order):
        if o.booked:
            return
        self._book(o, o.filled)

    def _on_watchdog(self, o: Order):
        if o.booked or o.oid not in self.pending:
            return
        if self.cfg.policy == "P0":
            # optimistic booking at intended quantity; never queried again
            self._book(o, o.delta)
            return
        # order-status query resolves ground truth
        self.m.data_calls += 1
        if o.accepted and o.fill_done:
            self._book(o, o.filled)
        else:
            o.booked = True
            self.pending.pop(o.oid, None)                # never executed

    def _on_retry(self, oid: int):
        o = self.pending.get(oid)
        if o is None or o.booked or not o.ambiguous:
            return
        # delta-mode client blindly re-sends the same signed delta
        self._submit(o.agent, o.sym, o.delta, is_retry=True, orig=o)

    def _on_oob(self):
        cfg = self.cfg
        if cfg.bursty:
            # thinning: reschedule at max rate, keep with prob mult/burst_mult
            self._push(self._exp(cfg.eff_oob_interval() / cfg.burst_mult),
                       "oob", None)
            if self.rng.random() > self._fmult() / cfg.burst_mult:
                return
        else:
            self._push(self._exp(cfg.eff_oob_interval()), "oob", None)
        s = self.rng.randrange(cfg.n_symbols)
        qty = round(cfg.oob_base + self.rng.expovariate(1.0 / cfg.oob_exp))
        if self.rng.random() < 0.5:                      # out-of-band sell
            q = min(qty, self.pos[s])
            if q > 0:
                self._pos_change(s, self.pos[s] - q)
                self.h[s] -= q
                self.drift_events[s].append(DriftEvent(self.t))
                self.m.oob_events += 1
        else:                                            # out-of-band buy
            self._pos_change(s, self.pos[s] + qty)
            self.h[s] += qty
            self.drift_events[s].append(DriftEvent(self.t))
            self.m.oob_events += 1

    def _on_tick(self):
        self._poll()
        for s in range(self.cfg.n_symbols):
            self._check_sym(s)
        self._push(self.t + self.cfg.tau, "tick", None)

    def _on_wake(self, i: int):
        cfg = self.cfg
        s = self.rng.choice(self.agent_syms[i])
        pend = sum(
            o.delta for o in self.pending.values() if o.agent == i and o.sym == s
        )
        virtual = self.b[i][s] + pend
        target = max(0, virtual + round(self.rng.gauss(0.0, cfg.target_sigma)))
        self._execute(i, s, target)
        self._push(self._exp(cfg.wake_mean), "wake", i)

    # ---------------- main loop ----------------

    def run(self) -> Metrics:
        cfg = self.cfg
        while self.events:
            t, _, kind, payload = heapq.heappop(self.events)
            if t > cfg.horizon:
                break
            # piecewise-constant integration up to this event
            dt = t - self._last_t
            if dt > 0:
                self.m.drift_integral += self._drift_now() * dt
                self.m.frozen_integral += self._frozen_agents() * dt
            self._last_t = self.t = t

            if kind == "wake":
                self._on_wake(payload)
            elif kind == "fill":
                self._on_fill(self.pending[payload]) \
                    if payload in self.pending else None
            elif kind == "report":
                o = self.pending.get(payload)
                if o is not None:
                    self._on_report(o)
            elif kind == "watchdog":
                o = self.pending.get(payload)
                if o is not None:
                    self._on_watchdog(o)
            elif kind == "retry":
                self._on_retry(payload)
            elif kind == "oob":
                self._on_oob()
            elif kind == "burst_on":
                self.burst = True
                self._push(self._exp(cfg.burst_mean), "burst_off", None)
            elif kind == "burst_off":
                self.burst = False
                self._push(self._exp(cfg.quiet_mean()), "burst_on", None)
            elif kind == "tick":
                self._on_tick()
            elif kind == "realign":
                self._do_realign(payload)

        # integrate the tail
        dt = cfg.horizon - self._last_t
        if dt > 0:
            self.m.drift_integral += self._drift_now() * dt
            self.m.frozen_integral += self._frozen_agents() * dt
        self.m.censored_events = sum(len(v) for v in self.drift_events)
        return self.m


# ----------------------------------------------------------------------
# One-run summary (flat dict for CSV rows)
# ----------------------------------------------------------------------

def _percentile(xs: list, q: float):
    if not xs:
        return None
    ys = sorted(xs)
    k = (len(ys) - 1) * q
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    if lo == hi:
        return ys[lo]
    return ys[lo] + (ys[hi] - ys[lo]) * (k - lo)


def run_once(cfg: Config) -> dict:
    m = Sim(cfg).run()
    days = cfg.horizon / 86400.0
    detected = len(m.cons_lats)
    total_ev = detected + m.censored_events
    return {
        "policy": cfg.label(),
        "base_policy": cfg.policy,
        "tau": cfg.tau,
        "fault_x": cfg.fault_x,
        "n_agents": cfg.n_agents,
        "wake_mean": cfg.wake_mean,
        "poll_staleness": cfg.poll_staleness,
        "report_lag_mean": cfg.report_lag_mean,
        "retry_mode": cfg.retry_mode,
        "fresh_guard": int(cfg.fresh_guard),
        "bursty": int(cfg.bursty),
        "seed": cfg.seed,
        "orders_per_day": m.orders_submitted / days,
        "filled_per_day": m.orders_filled / days,
        "refused_per_day": m.refused / days,
        "unsafe_orders_per_day": m.unsafe_orders / days,
        "unsafe_shares_per_day": m.unsafe_shares / days,
        "duplicates_per_day": m.duplicates / days,
        "overshoot_per_day": m.overshoot_shares / days,
        "data_calls_per_day": m.data_calls / days,
        "freezes_per_day": m.freezes / days,
        "false_freezes_per_day": m.false_freezes / days,
        "writedowns_per_day": m.writedown_shares / days,
        "availability": 1.0 - m.frozen_integral / (cfg.n_agents * cfg.horizon),
        "drift_share_hours_per_day": m.drift_integral / 3600.0 / days,
        "det_lat_median": _percentile(m.det_lats, 0.5),
        "det_lat_p90": _percentile(m.det_lats, 0.9),
        "cons_lat_median": _percentile(m.cons_lats, 0.5),
        "oob_events": m.oob_events,
        "detected_fraction": (detected / total_ev) if total_ev else None,
        "censored_events": m.censored_events,
        "realigns_per_day": m.realigns / days,
    }
