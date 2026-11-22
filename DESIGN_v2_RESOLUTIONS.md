# EvalForge — design v2: Goldfish resolutions

Three stateless reviewers (SRE, staff engineer, security/readiness) each found 12–15 defects
with no knowledge of the author's reasoning. They converged on five blockers. This document
records the **decisions taken**, because a design that doesn't answer its reviewers is just
a document with opinions.

Verdict from the readiness Goldfish was **NOT READY**. v2 is the answer to that.

---

## R1 — The digest contract (was: G1 false)

**Finding.** `EvalPolicy` contains `max_cost_inr`, so budget is inside `spec_hash` — but
which items get scored is decided by the mutable ledger. Same spec, different truncation
point, different digest. And `[]` from `score()` meant both "clean" and "never looked at".

**Decision.** Coverage becomes a first-class, digested quantity.

- Every item record carries `status ∈ {scored, skipped_budget, skipped_policy, failed, dead_letter}`.
- The digest is computed over `(item_id → status, findings)` sorted by `item_id`, plus the
  metric block, plus `coverage = scored / total`.
- **A run with coverage < 1.0 has a different digest from a complete run, by construction.**
  Truncation cannot masquerade as completeness.
- `RunSpec` no longer contains `max_cost_inr`. Budget is *execution policy*, passed at run
  creation and recorded on the run row, **outside** the identity of the spec. Two runs with
  different budgets have the same `spec_hash` and legitimately different digests — and the
  run row says why.
- Determinism CI job: two runs, same spec, same budget, cold vs warm cache, assert identical
  digest. Cross-machine assertion is documented as a known limit (float/BLAS), not claimed.

## R2 — Tier-0 isolation (was: aspiration)

**Finding.** "A `ctx` with no provider client" constrains only code that consents. A scorer
can `import httpx`, read `os.environ`, open a socket.

**Decision.** Enforce it at the socket, not the API.

- Tier-0 scorers execute inside `egress.blocked()`, a context manager that replaces
  `socket.socket`, `socket.create_connection` and `ssl.SSLContext.wrap_socket` with stubs
  that raise `EgressViolation`. Any attempt to reach the network fails loudly and is recorded
  as a `failed` item with reason `egress_violation` — it does not silently pass.
- The guard is tested: a scorer that tries to connect must raise, and the run must mark the
  item failed rather than scored.
- **Honest limit, stated in the README:** this is a guard against accident and casual
  misuse, not a sandbox against hostile code. Real isolation (subprocess + no-network
  namespace + seccomp) is v0.2. Until then, scorers are trusted code from a vetted registry.

## R3 — Budget arithmetic (was: "never overspends", false)

**Finding.** Simulated with correct accounting: 8 workers, ceiling ₹100, estimate ₹10,
actual ₹30 → settled ₹120. Overshoot is bounded by `in_flight × (actual − estimate)`, a term
the design never bounded. Plus leaked reservations on crash/breaker paths.

**Decision.** Stop claiming zero overshoot; bound it and report it.

- `reserve()` is a single atomic UPDATE with a `WHERE spent + reserved + est <= ceiling`
  predicate. No read-then-write.
- `settle(actual)` charges `min(actual, reserved)` and records `overrun = max(0, actual − reserved)`
  in a separate column. **The ceiling governs reservations; overrun is measured, never hidden.**
- Every reservation has a TTL and a `release()`; a reaper releases reservations whose lease
  expired, so a crashed worker cannot permanently consume budget.
- **New invariant, tested:** `spent_actual <= ceiling + overrun_total`, and `overrun_total`
  is surfaced in `/metrics` and in the report. A run that overran is flagged `budget_overrun`
  and **cannot pass the gate**.

## R4 — The gate (was: the product was unsound)

**Finding.** Recall is a ratio. A run that skipped 40% of items computes recall over the
remaining 60% — a different population from the baseline. It can pass a real regression.
There is no `inconclusive` outcome.

**Decision.** Three outcomes, and a coverage precondition.

- `gate` returns `PASS | FAIL | INCONCLUSIVE` with distinct exit codes `0 | 1 | 2`.
- **Coverage guard:** if `coverage < policy.min_coverage` (default 0.98) or if candidate and
  baseline coverage differ by more than `policy.coverage_tolerance` (default 0.01), the
  result is `INCONCLUSIVE` — never `PASS`.
- If any `budget_overrun` occurred, the result is `INCONCLUSIVE`.
- Categories present in one run but absent in the other are reported as `incomparable` and
  do not contribute to the verdict.
- Denominators are stated in the code and in the report: recall = TP / (TP + FN) over
  *scored* items; skipped items are excluded from both numerator and denominator **and**
  reported separately, so a reader can see the population.

## R5 — Queue lifecycle (was: no lease, no reaper, no drain)

**Finding.** `SKIP LOCKED` alone: hold the transaction and you get idle-in-transaction; commit
and a `kill -9` strands the row as `running` forever. No drain on deploy, which contradicts
the queue's own justification.

**Decision.** Lease-based claim with heartbeat, reaper, and drain.

- `claim()` sets `status=running, lease_expires_at=now()+lease_ttl, attempts=attempts+1,
  worker_id=...` in one statement. The transaction does **not** span item processing.
- Workers `heartbeat()` while working; a reaper requeues rows whose lease expired and whose
  `attempts < max_attempts`, else marks them `dead_letter`.
- `SIGTERM` → stop claiming → finish in-flight → exit. Grace period documented as
  `lease_ttl + max_provider_timeout`.
- Run finalisation takes a row lock (`SELECT ... FOR UPDATE`) and is idempotent: the run
  transitions `running → complete` exactly once, guarded by state.

## R6 — Multi-tenancy (was: convention, untested)

**Decision.** v0.1 is explicitly **single-tenant**, and the README says so. The `project_id`
column exists and is indexed, but no isolation is claimed. RLS and a mandatory repository
layer are v0.2, listed as a blocking prerequisite for multi-tenant use. **Claiming a control
you don't exercise is worse than not having it.**

## R7 — Everything else the Goldfish raised, triaged

| Finding | Decision |
|---|---|
| Audit can't name a principal | **Fixed.** `runs.created_by` records the API-key id; keys are scoped and identified, not just hashed. |
| Cache key omits scorer version/config | **Fixed.** Cache key = `(scorer_name, scorer_version, config_hash, item_hash)`. |
| Unbounded `run` label on a metric | **Fixed.** Cost metric is a counter with no run label; per-run cost is in the report, not in Prometheus. |
| No idempotency on run creation | **Fixed.** `POST /runs` accepts `Idempotency-Key`; same key returns the existing run. |
| Rate limiter is per-process | **Documented limit.** v0.1 is single-worker-safe; distributed limiting is v0.2 with the Redis queue. |
| Plugin supply chain / sandbox | **Documented limit** (R2). Trusted registry only. |
| PII / retention | **Documented limit.** Dataset deletion endpoint is v0.2; README states it plainly. |
| Throughput number unproven | **Removed.** No throughput claim in v2. A measured benchmark with a ramp is v0.2 work. |
| No backups | **Documented** in the deployment section as a deployment responsibility, with `pg_dump` guidance. |

---

## What this means for the build

The five blockers are the reason this project exists in the shape it does. Each one is
implemented and has a test that fails if the fix is removed:

1. `tests/test_digest.py` — truncation changes the digest
2. `tests/test_egress.py` — a tier-0 scorer that opens a socket raises and marks the item failed
3. `tests/test_ledger.py` — concurrent reservations cannot exceed the ceiling; overrun is recorded
4. `tests/test_gate.py` — a truncated run returns INCONCLUSIVE, never PASS
5. `tests/test_queue.py` — an expired lease is reclaimed; max attempts dead-letters

That is the difference between a design document and a system.
