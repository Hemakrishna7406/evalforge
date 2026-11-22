# EvalForge

A production evaluation harness. Versioned runs, budgeted scorers, and a gate that refuses
to guess.

The interesting thing about this repository is not that it works — it's *how it was designed*.
It was built with Google's [Elephant-Goldfish Model](https://github.com/google/elephant-goldfish-model):
a context-rich design phase, then **three stateless adversarial reviewers** who saw only the
design document and none of the author's reasoning. They found five blockers that invalidated
the first design's headline claims. Those five blockers are the reason the code looks the way
it does, and each one has a test that fails if its fix is removed.

The reviews are checked in, unedited, in [`docs/reviews/`](docs/reviews/).

---

## The process, in order

| Phase | Artefact | What happened |
|---|---|---|
| **Elephant** — design | [`DESIGN.md`](DESIGN.md) | Wrote the architecture with full context: goals, non-goals, components, risks. No code. |
| **Goldfish** — adversarial review | [`docs/reviews/`](docs/reviews/) | Three fresh-context reviewers (SRE, staff engineer, security + readiness) each produced 12–15 findings. Verdict: **NOT READY**. |
| **Elephant** — resolve | [`DESIGN_v2_RESOLUTIONS.md`](DESIGN_v2_RESOLUTIONS.md) | Each blocker answered with a decision, not a defence. |
| **Build** | `src/evalforge/` | Implemented against the resolutions. |
| **Verify** | `tests/test_blockers.py` | One test per blocker, named after the defect. |

### What the Goldfish killed

All three independently found the same five defects. Convergence across reviewers with no
shared context is the signal that a finding is real.

| # | Blocker | Why the first design was wrong |
|---|---|---|
| 1 | **Digest vs budget** | Budget lived in the spec hash but the ledger decided *which items got scored*. Same spec, different truncation, different digest — so "same spec ⇒ same digest" was false. `score() -> []` also meant both "clean" and "never looked at". |
| 2 | **Tier-0 isolation** | "A context object with no provider client" is capability *hiding*. A scorer can `import httpx`. |
| 3 | **Reserve/settle** | Simulated with *correct* accounting: 8 workers, ₹100 ceiling, estimate ₹10, actual ₹30 → settled **₹120**. "Never overspends" was arithmetically false, and crashed workers leaked reservations. |
| 4 | **The gate** | Recall is a ratio. A run that skipped 40% of items computed recall over a different population from its baseline and could **pass a real regression**. |
| 5 | **The queue** | `SELECT … FOR UPDATE SKIP LOCKED` alone is not a queue: no lease, no heartbeat, no reaper. A `kill -9` stranded the row as `running` forever. |

### How each was answered

| # | Fix | Test |
|---|---|---|
| 1 | Coverage is a **digested** quantity. A truncated run has a different digest by construction. Budget moved out of `RunSpec` into `ExecutionPolicy`, recorded on the run row. | `test_truncation_changes_the_digest` |
| 2 | Tier-0 scorers execute inside `egress.blocked()`, which replaces the socket entry points. A network attempt raises and the item is marked `failed`, never silently scored. | `test_tier0_scorer_cannot_open_a_socket` |
| 3 | Atomic `reserve` with the ceiling in the `WHERE` clause; `settle` charges at most the reservation and records the excess as **overrun**, which is reported and blocks the gate; every reservation has a TTL and a reaper. | `test_reservations_cannot_exceed_the_ceiling`, `test_settle_records_overrun_instead_of_hiding_it` |
| 4 | Three outcomes — `PASS` / `FAIL` / `INCONCLUSIVE` — with a coverage precondition. A truncated run or a budget overrun returns `INCONCLUSIVE`, never `PASS`. | `test_truncated_run_is_inconclusive_never_pass` |
| 5 | Claims are leases: `lease_expires_at` + `attempts`, `heartbeat()` while working, `reap()` requeues expired leases and dead-letters past `max_attempts`. The transaction never spans item processing. | `test_expired_lease_is_reclaimed`, `test_max_attempts_dead_letters` |

---

## Quickstart

```bash
git clone <this repo> && cd evalforge
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -q                                  # 15 tests
evalforge run --dataset data/sample.jsonl --out ./runs/a
evalforge gate --candidate ./runs/a/run.json --baseline ./runs/a/run.json
```

The gate exits **0** (pass), **1** (regression), **2** (inconclusive). The third exit code
exists so CI can distinguish "it broke" from "we couldn't measure it".

## Architecture

```
CLI / CI ──▶ runner ──▶ ItemQueue (leases)  ──▶ scorers (tier 0, egress-blocked)
                │                              └▶ scorers (tier 1, budgeted)
                ├──▶ Ledger (atomic reserve / settle / release / reap)
                └──▶ digest (items + statuses + coverage) ──▶ report
                                                          └▶ gate (PASS/FAIL/INCONCLUSIVE)
```

Every module has one job and one failure mode it owns:

| Module | Owns |
|---|---|
| `spec.py` | Run identity. What makes two runs *the same run*. |
| `digest.py` | Proof that two runs measured the same thing, including what they missed. |
| `ledger.py` | Spend, with the ceiling in the `WHERE` clause and overrun made visible. |
| `queue.py` | Item liveness under crashes and deploys. |
| `egress.py` | The tier-0 network boundary. |
| `gate.py` | The verdict, and the refusal to guess. |
| `runner.py` | Composition, and nothing else. |

## What is actually implemented

Honest scope, because a README that oversells is worse than a small one.

**Working, tested (15 tests):**
- Run identity and content-addressed hashing
- Digest over item outcomes **and** coverage; order-independent; truncation-sensitive
- Budget ledger: atomic reserve, overrun accounting, release, TTL reaper
- Lease-based queue: claim, heartbeat, reap, dead-letter
- Tier-0 egress guard with restore-on-exit
- Four free scorers (`empty_output`, `numeric_preservation`, `repetition`, `length_ratio`)
- Three-outcome gate with coverage and overrun preconditions
- CLI: `run`, `gate`

**Designed but not built (documented, not pretended):**
- **Postgres backend.** The SQL is written for both SQLite and Postgres, and `claim()` emits
  `FOR UPDATE SKIP LOCKED` under `dialect="postgresql"`, but only SQLite is exercised by the
  tests. This is the largest gap.
- **REST API, worker process, Docker Compose, Prometheus export.** Designed in `DESIGN.md` §4–6.
- **LLM judge (tier 1).** The budget plumbing and the failure path exist; the provider client
  does not.
- **Multi-tenancy.** v0.1 is explicitly single-tenant. The `project_id` column is not there
  yet and no isolation is claimed — per resolution R6, claiming a control you don't exercise
  is worse than not having it.
- **Plugin sandbox.** `egress.blocked()` stops accident, not hostile code. Real isolation
  (subprocess + no-network namespace) is v0.2.

## Known limitations, stated plainly

- **Cross-machine determinism is not claimed.** Canonical JSON with fixed float formatting
  handles dict ordering and float repr, but not BLAS dispatch differences or Unicode
  normalisation. Same-machine determinism is tested; cross-machine is not.
- **The egress guard is not a sandbox.** It patches stdlib entry points; determined code can
  get around it. It is a guard against mistake, and the README says so.
- **The gate compares recall by category only.** It has no notion of statistical significance;
  tolerances are absolute thresholds, not confidence intervals.
- **Single-writer queue.** Correct under one worker and safe under several, but only SQLite
  is tested.

## Roadmap

1. **v0.2** — Postgres backend + integration tests against a real database; REST API; worker
   process; Docker Compose; Prometheus metrics with bounded labels only.
2. **v0.3** — LLM judge with cache-by-exact-input and consistency sampling; provider client
   with retry, circuit breaker, and distributed rate limiting.
3. **v0.4** — Plugin sandbox (subprocess + seccomp); multi-tenant RLS with an isolation test
   that actually attempts a cross-tenant read.

## AI assistance

The design was written with an AI assistant acting as the Elephant. The adversarial reviews
were produced by three separate stateless AI reviewers given only the document — Google's
Goldfish pattern. Every finding they raised is answered in `DESIGN_v2_RESOLUTIONS.md`, and the
ones that were out of scope are listed as limitations rather than quietly dropped. Human
judgment decided which blockers to fix, which to bound, and which to defer.
