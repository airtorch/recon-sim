# Full proofs for Propositions 1–3

This supplement gives the full case analyses for the three propositions
whose proof sketches appear in Section IV of the paper. Notation follows
the paper and `sim/model.py`.

## Notation and lifecycle

For instrument `s` and agent `i`:

- `b[i][s]` — agent `i`'s book (claim) on `s`; `hhat[s]` — unclaimed pool.
- `C_s = Σ_i b[i][s] + hhat[s]` — total claimed holdings.
- `P_s` — pending orders on `s`, each with signed intent `q_o`.
- `pend[i][s] = Σ q_o` over `i`'s pending orders on `s`.
- Explainable interval:
  `L_s = C_s + Σ_{o∈P_s} min(q_o, 0)`, `U_s = C_s + Σ_{o∈P_s} max(q_o, 0)`.
- `V_s(t)` — authoritative venue aggregate; a snapshot read at time `t`
  reflects `V_s(t − δ)` for staleness `δ ≥ 0`.
- Realignment delay `T_r`; reconciliation interval `τ`; watchdog timeout `W`;
  report lag `R` (random, a.s. finite; lost reports resolved by the
  watchdog's status query).

An order passes through the following states (see `Order` in
`sim/model.py`):

```
submitted ──(ack)──────────────► accepted ──(fill)──► filled ─┬─(report)──► booked
    │                                                          └─(report lost;
    ├──(timeout: ambiguous)──► pending, acceptance unknown          watchdog+status)► booked
    └──(reject)────────────── ► pending until watchdog resolves as never-executed
```

**Membership rule.** An order enters `P_s` at submission — including
ambiguous submissions — and leaves `P_s` only when (a) a fill report books
it, (b) a watchdog-triggered status query books its true fill (possibly 0),
or (c) the status query establishes it was never executed. There is no
other exit. This rule is the crux of all three proofs.

---

## Proposition 1 (Idempotence)

**Claim.** Under target-based execution with ambiguous orders charged as
pending, the net executed quantity attributable to a declared target `T`
on instrument `s` by agent `i` is at most `|T − b₀|`, where `b₀` is the
book value when the target was first declared, regardless of how many
times the client re-issues the request.

**Proof.** Define the *virtual position* `v[i][s] = b[i][s] + pend[i][s]`.
The delta computed at (re-)issue time is `Δ = T − v[i][s]`. We show by
induction over events that after the first submission for target `T`,
`v[i][s] = T` holds until an order resolves *below* its intent, and that
every subsequent submission only tops up a shortfall, never duplicates.

Base step: first issue computes `Δ₀ = T − b₀` (empty `pend`), submits an
order with `q = Δ₀`, and `v = b₀ + Δ₀ = T`.

Inductive step — case analysis over every event that can change `v`:

1. **Re-issue while the order is pending (any pending state, including
   ambiguous).** By the membership rule the order is still in `P_s`, so
   `Δ = T − v = T − T = 0`; nothing is submitted. This covers client
   retries after timeouts, agent crash/restart followed by re-declaration,
   and duplicate scheduler wake-ups. `v` unchanged.

2. **Fill report with full fill `f = q`.** Booking sets `b += q` and
   removes the order (`pend −= q`): `v` unchanged (`= T`). A later
   re-issue computes `Δ = 0`.

3. **Fill report with partial fill `f`, `|f| < |q|`.** Booking sets
   `b += f`, `pend −= q`, so `v = T − (q − f)`. A re-issue computes
   `Δ = q − f`: exactly the unfilled remainder. Cumulative submitted
   intent is `q + (q − f) = 2q − f`, but cumulative *executed* quantity is
   `f + (executed remainder) ≤ f + (q − f) = q = |T − b₀|`. The same
   argument applies recursively to the remainder order.

4. **Watchdog status query on an ambiguous order — venue accepted.** The
   query returns the true fill `f`; booking proceeds as in case 2/3.
   No second submission for the same intent ever occurred (case 1 blocked
   it while pending), so no duplication.

5. **Watchdog status query — venue never executed the order.** The order
   leaves `P_s` with no booking: `v = T − q`. A re-issue computes
   `Δ = q`, a fresh submission for intent that provably never executed.
   Cumulative executed ≤ `|T − b₀|` still holds.

6. **Lost fill report.** The order remains in `P_s` (case 1 applies to
   any re-issue) until the watchdog fires; then case 4 books the true
   fill. No path books twice: booking is guarded by the `booked` flag and
   removal from `P_s` (see `_book` in `sim/model.py`).

In every case the cumulative executed quantity toward `T` is bounded by
`|T − b₀|`. ∎

**Contrast (delta retries).** If the client re-submits the signed delta
`q` after an ambiguous timeout (case 1 replaced by a blind re-send), and
the original was in fact accepted, both orders may execute: net executed
quantity reaches `2|q|`. This is the duplication measured in the paper's
retry-semantics ablation (Table II).

---

## Proposition 2 (Bounded encroachment)

**Claim.** With exact snapshots (`δ = 0`) and reconciliation interval
`τ`, unsafe executed quantity (Definition 1) is bounded by the OOB
outflow occurring within a window of length `τ + T_r` before the
corresponding sell; with no OOB activity it is zero.

**Proof.** A fill is unsafe only if it increases
`E_s = max(0, Σ_{j≠i} max(a[j][s], 0) − V_s)`. Buys only increase `V_s`,
so (long-only) only *sell* fills can be unsafe. We enumerate every event
class that moves `V_s` or the claims:

1. **Platform-mediated fills (any agent).** Every platform order is in
   `P_s` from submission until booking, so its possible outcomes are
   contained in the interval sums of `L_s`/`U_s`; moreover its fill
   simultaneously moves `V_s` and the true allocation `a[i][s]` of the
   *selling* agent. Such a fill can encroach only if the seller's claim
   was not backed at fill time — which requires a prior unexplained
   shortfall (case 3).

2. **Partial fills, lost reports, ambiguous orders, crashes.** Each
   changes when/whether an order in `P_s` resolves, but the order's
   contribution to `[L_s, U_s]` covers every outcome in
   `[min(q,0), max(q,0)]`. Hence none of these can push a true snapshot
   outside the explainable interval: they cause no unexplained shortfall
   and no encroachment by themselves. With no OOB activity this proves
   the zero clause: claims are always backed, and the sell guard's floor
   check is never violated by a fill.

3. **OOB outflow of `x` shares at time `t₀`.** This is the only event
   that lowers `V_s` without a corresponding pending order. From `t₀`
   until the next reconciliation observation, claims may exceed backing
   by up to `x` (plus any further OOB outflow). Sub-cases:

   a. **Sells submitted in `[t₀, t_d)`,** where `t_d ≤ t₀ + τ` is the
      next tick (or pre-sell check): the guard evaluates the floor
      against the last snapshot, taken at some `t_snap ≥ t_d − τ ≥ t₀ − τ`.
      With `δ = 0` the snapshot is exact at its read time, so the guard's
      error is at most the OOB outflow in `(t_snap, t]` — i.e., outflow
      within the last `τ` seconds. Sell intent already pending is netted
      by the guard (it subtracts pending sells), so it is not
      double-counted.

   b. **At `t_d`** the snapshot violates `L_s` (the shortfall is
      unexplainable by construction of case 2), the freeze halts all new
      orders on `s`, and a realignment is scheduled for `t_d + T_r`.

   c. **In `[t_d, t_d + T_r]`** no new sells are accepted (frozen);
      in-flight sells submitted before `t_d` may still fill, but their
      intent is part of sub-case (a)'s accounting.

   d. **At `t_d + T_r`** realignment re-polls, absorbs the loss from the
      pool then pro-rata from books, and rebases recognized allocations
      (`_rebase_truth`): claims are again fully backed and `E_s = 0`.

   Summing: unsafe quantity per OOB event is bounded by the outflow that
   occurred within `τ` of a guarded sell plus fills of sells in flight
   during `[t_d, t_d + T_r]` — all within a `τ + T_r` window before the
   corresponding fill. ∎

**Remark (δ > 0).** With stale snapshots the guard and the tick both act
on `V_s(t − δ)`, widening the exposure window to `τ + T_r + δ` and adding
false freezes when a legitimate in-flight resolution is misread as a
shortfall. The paper quantifies both effects empirically (Section V-D);
we do not claim an analytic bound for `δ > 0`.

---

## Proposition 3 (Time to consistency)

**Claim.** After the last fault event, every instrument's ledger
satisfies `Ṽ_s ∈ [L_s, U_s]` and all freezes are released within
`τ + T_r` plus the resolution time of in-flight orders.

**Proof.** Let `t_f` be the time of the last fault event. Step 1:
every order pending at `t_f` resolves by `t_f + max(R, W)` — a delivered
report books it within lag `R`; a lost report or ambiguous order is
resolved by the watchdog status query at submission time `+ W`, which by
the membership rule is the latest possible exit from `P_s`. After this
point `P_s` shrinks to (at most) newly submitted, fault-free orders,
whose lifecycle keeps `V_s` within `[L_s, U_s]` (Proposition 2, case 1–2
analysis).

Step 2: any residual discrepancy left by the faults (e.g., an OOB change
not yet observed) is a fixed offset between `C_s` and `V_s`. The next
tick occurs within `τ`; the snapshot (exact after `δ`, which is dominated
by `τ` in all configurations studied) then either (a) exceeds `U_s` — the
surplus is absorbed into `hhat[s]` immediately, restoring the interval
test, or (b) falls below `L_s` — a realignment is scheduled and, `T_r`
later, rebuilds books and pool against a fresh poll so that
`C_s = V_s` with `P_s`'s remaining orders explainable. In both branches
the freeze on `s` (if any) is released at realignment completion
(`_do_realign`).

Step 3: absent new faults no event can re-violate the interval test:
platform fills are pre-explained by pending intent (case 1 of
Proposition 2), and there are no OOB changes. Hence no new freeze is
triggered, and total time is bounded by
`max(R, W) + τ + T_r`. ∎

---

*All three arguments are implemented verbatim in `sim/model.py`; the
simulator's ground-truth tracking (`t_alloc`, `h`) exists precisely so
that violations of these propositions would be measured rather than
assumed away.*
