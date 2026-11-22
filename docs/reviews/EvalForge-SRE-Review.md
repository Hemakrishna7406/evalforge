# EvalForge design review — SRE / production-operability lens

**Reviewer:** Goldfish reviewer (adversarial SRE pass)
**Subject:** DESIGN.md, draft 2026-11-22
**Verdict:** not shippable as written. Five blockers, all in the failure path. The
happy path is well specified; the unhappy path is almost entirely absent. Most of
the blockers are one-paragraph fixes, but three of them (lease/reclaim, drain,
budget atomicity) are architectural and change the schema.

---

## BLOCKER-1 — No visibility timeout, no lease, no reclaim: crashed work is lost forever
**Attacks:** §4 (line 51, 70), §8 ("Provider outage mid-run … resumable runs (items are independent rows)")

`claim work (SELECT … FOR UPDATE SKIP LOCKED)` is the whole of the queue design.
There is no lease, no heartbeat, no `claimed_at`, no `attempts` column, no
visibility timeout, and no reaper. Two implementations are possible and both are
broken:

- **Lock held for the item's duration** (transaction open across provider calls).
  A tier-1 judge call is seconds to tens of seconds. That is an idle-in-transaction
  transaction per worker, holding a row lock, blocking autovacuum, pinning a
  connection, and unbounded if the provider socket hangs. §6's "short transactions"
  is incompatible with this, and §6 does not say which of the two you get.
- **Claim committed immediately** (`status='running'`). Now a `kill -9`, an OOM
  kill, a node eviction, or a deploy leaves the row `running` with no owner and no
  expiry. Nothing in the document ever moves it back to `pending`. The item is gone
  and the run can never reach a correct aggregate.

§8 claims resumability because "items are independent rows". That is not resumability.
Independence tells you a row *can* be retried; it does not tell you *which* rows need
retrying, because a claimed-but-abandoned row is byte-identical to a claimed-and-in-progress
row. There is also nothing that restarts a run at all — resumption is asserted, not
mechanised.

**Need:** `claimed_at`, `lease_expires_at`, `attempts`, `last_error`; a
`reap_expired_leases()` sweeper (or `FOR UPDATE SKIP LOCKED` with
`WHERE lease_expires_at < now()` in the claim predicate so expiry is implicit and
there is no separate reaper process to fail); and a hard statement timeout so no
transaction outlives its lease.

---

## BLOCKER-2 — No worker drain on deploy, which directly contradicts "must survive a deploy"
**Attacks:** §4 (line 70, "must survive a deploy"), §6, §9

§4 justifies the worker queue with "must survive a deploy". §6 then describes the
deploy and says nothing about shutdown. There is no SIGTERM handler, no
"stop claiming → finish in-flight items → exit" sequence, no
`terminationGracePeriodSeconds` guidance, and no statement of the maximum item
duration that a grace period must cover.

Consequences, and they compound with BLOCKER-1:

1. Every rolling deploy severs in-flight tier-1 calls. Tokens are already paid for;
   the result is discarded; the reservation is never settled (BLOCKER-3).
2. Those items stay `running` forever (BLOCKER-1). Deploy frequency becomes a
   monotonic leak of unscored items.
3. Default container grace periods are ~30s. A judge call plus retries can exceed
   that. So the drain window is not a nice-to-have; it needs to be a stated
   invariant: `max_item_deadline < terminationGracePeriodSeconds`.
4. Nothing says the API is drained either. In-flight `POST /runs` during a
   migration-running restart either 502s the CI job or, worse, half-enqueues a run.

**Need:** explicit lifecycle (readiness → SIGTERM → drain → exit), a stated
per-item deadline budget, and a CI test that kills a worker mid-item and asserts
the item is re-claimed and completed exactly once.

---

## BLOCKER-3 — The cost ceiling is a check-then-act race, and reservations leak
**Attacks:** G3 (§2), §5.4 (line 114)

"`reserve(estimated_cost)` before the call; `settle(actual)` after … If reservation
would breach `policy.max_cost_inr`" — this is read-balance, compare, write. Under
N workers it is only correct if the balance read takes a row lock (`SELECT … FOR
UPDATE` on the run's ledger row) or the ledger is append-only with a serialized
insert-then-check. §5.4 says "a transactional ledger" but never names the isolation
or locking discipline, so the guarantee in G3 ("never overspends") is asserted, not
established. With 4 workers this is trivially violated: all four read
`spent = max − ε` and all four reserve.

Three more holes in the same paragraph:

- **Reservation leak.** A worker that dies between `reserve` and `settle` holds
  budget permanently. There is no reservation TTL and no reconciliation job, so the
  run marks later items `skipped_budget` for money that was never spent. Silent
  under-delivery of results, attributed to a budget ceiling that was never reached.
- **`settle(actual)` where actual > estimate.** Undefined. Does it clamp (lying
  about spend), refuse to write (losing the audit trail), or overspend (violating
  G3)? Price tables (§5.5) make estimate drift normal, not exceptional.
- **Cache hits.** §5.3/§8 cache the judge by exact input. Does a cache hit reserve?
  If yes, you burn budget on free calls; if no, the ledger under-reports and the
  `cost` metric in §5.6 disagrees with `evalforge_cost_inr_total` in §5.7.
- **Per-run only.** There is no project-level or global ceiling. Twenty parallel CI
  pipelines each respect their own `max_cost_inr` and the org still gets a surprise
  bill. "Bounded spend" is bounded per run, which is not what an on-call wants.

---

## BLOCKER-4 — The reproducibility contract (G1) is self-contradicting whenever `llm_judge` is enabled
**Attacks:** G1 (§2), §5.1 (line 88), §5.3, §5.6 (line 124), §8 (line 167)

Line 88: "Two runs with the same spec_hash and dataset hash **must** produce the same
digest … asserted in CI." Line 124: the digest is SHA-256 over items+metrics. §5.6
lists "MQM-style severity score" among the metrics, and §5.3 ships `llm_judge` as a
tier-1 scorer producing findings. §8 itself concedes "LLM judge non-determinism".

So the doc simultaneously promises a bit-identical digest and admits the input to
that digest is non-deterministic. The §8 mitigation — "gate uses tier-0 metrics for
pass/fail, judge reported separately" — fixes the *gate*, not the *digest*. G1 is
still false for any run that names `llm_judge`, which is the flagship scorer.

Also unaddressed: `ModelConfigRef` is "provider, model id, params hash". Model-id
pinning does not give you determinism. Provider aliases get re-pointed; `params`
does not record temperature/seed requirements; most providers do not guarantee
seeded determinism at all. And `consistency-sampled` (§5.3) means k calls per item
and a vote — which multiplies both cost and variance, and is not reflected in the
budget estimate.

**Need:** split the contract. `digest_tier0` (deterministic, byte-identical, CI-asserted)
vs `digest_full` (recorded, comparable, not asserted). State explicitly that G1 holds
only over tier-0 scorers. Otherwise the first CI run that includes a judge flakes and
the team disables the determinism test — which is how reproducibility contracts die.

---

## BLOCKER-5 — Budget-skipped and errored items have no defined effect on the gate's denominators, so `gate` can block or pass on arithmetic rather than quality
**Attacks:** §5.4 (line 114), §5.6 (line 122–126), G4 (§2)

§5.4: "the item is marked `skipped_budget` and the run continues to completion with
partial results." §5.6: the gate exits 1 when "any category's recall drops beyond
tolerance". Recall is a ratio. The document never says what the denominator is when
items are skipped or failed.

Both choices are wrong in a way that ships bad releases:

- **Skipped counted as miss** → recall falls → `gate` fails a release because the
  run ran out of money. That is a false block, and it trains the team to raise the
  budget ceiling or ignore the gate.
- **Skipped excluded from the denominator** → recall is computed over a
  self-selected subset. If the budget ran out mid-run, the surviving items are the
  early ones — i.e. not a random sample. Recall is inflated and a real regression
  hides. That is a false pass on the one thing the product exists to catch.

The same problem applies to errored items (BLOCKER-1, MAJOR-6) and to comparing
against a baseline with a *different* skip profile. §5.6 compares this run's recall
to the baseline's recall with no mention of skip-aware comparison, no minimum-coverage
threshold, and no "inconclusive" exit code. A gate has three outcomes, not two.

**Need:** a stated coverage threshold (`if scored_items / total_items < 0.95: exit 3 =
inconclusive`), skip-aware comparison, and an explicit rule for the denominator.

---

## MAJOR-6 — No per-item error taxonomy: poison messages either loop forever or vanish silently
**Attacks:** §5.3 (line 104), §5.7 (line 130), §8 (line 164)

§8 says a broken scorer "cannot break unrelated runs" — true, and that is a good
property. It does not say what happens to the item the broken scorer was scoring.
There is no `attempts` cap, no dead-letter state, no terminal `failed` status, and
`evalforge_items_total{status}` never enumerates its `status` values. So:

- A scorer that raises deterministically on one item is either retried forever
  (retry storm, worker pinned, budget burned on a call that will never succeed), or
  the item is silently dropped and the run reports success.
- "A run never fails because it ran out of money" is a stated principle (§5.4). There
  is no equivalent principle for "a run never reports success while items are
  unaccounted for." That asymmetry is the bug.
- A malformed item (missing `reference` for a scorer that needs it, a 40MB input
  that blows the token window, a non-UTF8 field) is a data problem, not an
  infrastructure problem, and needs a distinct status from a provider 500.

**Need:** `status ∈ {pending, running, done, failed_retryable, failed_permanent,
skipped_budget}`, `attempts` with a cap (e.g. 5) and exponential backoff, terminal
failures surfaced in the run summary and the gate's coverage number.

---

## MAJOR-7 — Nothing defines who finalises a run, and a dead run hangs CI forever
**Attacks:** §5.6, §6, G4

Aggregation and digest computation are described as outcomes, not as a mechanism.
With N workers all decrementing a pending counter, two can observe zero and both
compute/write the digest, or the aggregate can be computed before the last item's
`results` rows commit and produce a digest over a truncated set. §6 mentions
advisory locks only for migrations, not for finalisation.

And the failure case: if a worker dies holding the last item (BLOCKER-1), the run
stays `running` indefinitely. `evalforge gate` on that run does what? The document
does not say. If it polls, the CI job hangs until the pipeline timeout — a 6-hour
GitHub Actions job holding a runner because an item is orphaned. If it exits
non-zero immediately, then every transient failure blocks a release.

**Need:** a run state machine (`queued → running → finalising → complete | failed |
incomplete`), finalisation under an advisory lock on `run_id`, and a bounded gate
wait with an `incomplete` exit code.

---

## MAJOR-8 — The rate limiter is per-process, so the aggregate rate is N × limit; the circuit breaker is per-process too
**Attacks:** §5.5 (line 118), §6 (line 146)

"per-provider token-bucket rate limiting" lives in the provider client, which lives
in the worker. Four workers means four buckets and four times the configured rate
against a provider whose quota is per-key, not per-pod. This is the single most
common way a "horizontally scalable" worker fleet DDoSes its own provider on the
first scale-out, and §6 explicitly sells horizontal scaling as the scale path.

Same issue for the circuit breaker: "opens after N consecutive failures and fails
fast" — per worker. Each of N workers independently burns N × threshold failing
calls before it learns. When the provider recovers, each worker independently
half-opens and they arrive as a thundering herd. Jitter on backoff is specified
(good) but jitter does not fix a shared quota.

Also undefined: what "fails fast" *does* to the item (retryable failure? terminal?
does the run abort?), and whether the breaker is per-provider, per-model, or
per-endpoint.

**Need:** either a shared limiter (Postgres-backed token bucket is acceptable at
these rates, or divide the configured rate by replica count with a documented
`EVALFORGE_REPLICAS` contract), or drop the horizontal-scaling claim to "scale
workers only with the rate limit divided accordingly". A single global
`evalforge_circuit_open{provider}` gauge needs to exist (see MAJOR-10).

---

## MAJOR-9 — Observability: the metric set cannot answer "is this run stuck?" and one label violates the doc's own cardinality rule
**Attacks:** §5.7 (line 130), §5.8 (line 140), G5

`evalforge_queue_depth` alone is not a queue health signal. Depth 5 is healthy if
items are 40ms old and catastrophic if they are 4 hours old. There is no
oldest-pending-item-age, no claim latency, no lease-expiry counter — i.e. no metric
that would let an on-call see BLOCKER-1 happening.

Missing entirely:
- retry counts and 429/5xx rates (`evalforge_provider_errors_total{provider,code}`) —
  §5.5 promises retry/backoff/breaker and exports nothing about them.
- circuit-breaker state gauge.
- run-level status: `evalforge_runs_total{status}`, run duration, and a
  "runs currently `running` longer than X" gauge — the thing that catches MAJOR-7.
- budget: reservations outstanding, `skipped_budget` as a *ratio*, not a counter.
- gate decisions: `evalforge_gate_total{verdict}`. G4 is the product; it has no metric.

Two internal contradictions:
- `evalforge_cost_inr_total{run}` uses `run_id` as a label. §5.8 says "labels are
  bounded enums only — prevents cardinality blowup". Run ids are unbounded and grow
  every CI run. This is a direct violation of the doc's own rule and it will blow up
  Prometheus within weeks of real use. Cost belongs in the DB; export it as a gauge
  read from the ledger if you must.
- `evalforge_items_total{status}` has no `run` dimension and no bounded
  `scorer`/`tier` dimension, so it is a fleet-wide aggregate. You cannot debug a
  single bad run from metrics at all — you must go to the DB, which is exactly the
  3am query you do not want to write.

And there is no SLO and no alert. G5's success measure is "Prometheus metrics …
traces", which is a *capability*, not an objective. For a system that gates other
teams' releases, the SLOs that matter are gate availability, gate latency (does it
answer in time for the CI job), and false-block rate. §6 ships Prometheus with no
Alertmanager, so nothing pages anyone — which makes the whole "what breaks at 3am"
premise unanswerable by construction.

---

## MAJOR-10 — Data lifecycle is unaddressed, and the hottest table is the one with no maintenance plan
**Attacks:** §5.6, §6, §5.2

There is no retention policy, no partitioning, no archival, no autovacuum tuning and
no bloat discussion anywhere in the document.

Arithmetic for a single 50k-item run with the 7 shipped scorers: ~50k × 7 × ~1.5
findings ≈ 525k `results` rows, roughly 80–210MB depending on how much `evidence`
text you store. At three CI runs a day, ~250 working days, that is ~750 runs/year ≈
100GB/year and ~400M rows/year — from one team. `results` is insert-only, so it
bloats slowly; the real problem is that the digest and aggregation in §5.6 must read
all of it back per run, and no aggregation strategy is specified (streaming counters
during execution, or a `GROUP BY` over half a million rows at finalisation holding a
long transaction while other workers try to claim work).

Worse: `items` is the queue *and* the results substrate. It is UPDATEd on every
claim, status transition, lease refresh, retry and settle. That is a hot-update table
in steady state — exactly where autovacuum falls behind and `SKIP LOCKED` scans start
fighting dead tuples. No `fillfactor`, no `autovacuum_vacuum_scale_factor` override,
no partitioning by `run_id` (which would also make "drop this run's data" a `DROP
PARTITION` instead of a `DELETE` that leaves bloat).

Also unstated: where dataset JSONL bytes actually live in v1. §9 puts S3 in v0.2, so
v1 presumably stores them in Postgres — a 50k-item dataset with large inputs is
hundreds of MB, read repeatedly by N workers, and it grows forever because datasets
are immutable and append-only (§5.2).

**Need:** retention default (e.g. results older than 90 days aggregated and dropped),
partitioning by run or month, an explicit aggregation strategy, and a stated
`autovacuum` config for `items`.

---

## MAJOR-11 — No admission control or fairness: one runaway run starves every other pipeline
**Attacks:** §4, §5.2, §6

Nothing caps the number of concurrent runs per project, the size of a dataset, or
the total queue depth. `SKIP LOCKED` claims in whatever order the planner returns
rows — effectively FIFO by `id`, with no priority and no per-project fairness. So:

- A 2M-item dataset push (no size limit is stated in §5.2) monopolises every worker
  for hours. Every other team's CI gate queues behind it.
- Twenty pipelines triggered by the same merge train enqueue twenty runs; depth
  explodes; every run's latency degrades; the budget for each is still individually
  respected, so the cost is real.
- There is no way for a release-blocking gate to say "I am urgent". G4 is the product
  and it has no priority lane.

**Need:** per-project concurrency cap, dataset size cap with an explicit error,
weighted/priority claim ordering, and a queue-depth-based load-shedding response
(reject new runs with 429 rather than accepting them into an hour-long wait).

---

## MAJOR-12 — The ~200 items/s threshold is asserted, and the load test that is supposed to prove it cannot
**Attacks:** §6 (line 146), §7 (line 156), G2

§6 states "at ~200 items/s the single Postgres becomes the bottleneck" with no
derivation — no row sizes, no hardware, no worker count, no item mix. §7's load test
is "10k items, 4 workers" with a "documented throughput number". Those are not the
same claim:

- One data point at 4 workers cannot locate a ceiling. You need a ramp (1, 2, 4, 8,
  16 workers) and a knee, otherwise you have measured one point on an unknown curve.
- 200 items/s is only reachable on a tier-0-only, cache-cold workload. The moment
  `judge_when` fires, throughput is bound by provider latency and the rate limiter,
  not Postgres. At 20% sampling of 50k items (10k judge calls) and a 10 rps limit,
  the run takes ~17 minutes of pure provider wall-clock — the DB is idle for all of
  it. So the number that matters operationally is never stated.
- G2's "≥80% of items scored with zero model calls" is a *quality* claim measured
  against the item mix; the tier-0 cache (§5.4, keyed `(scorer, item_hash)`) is
  per-process, so with N workers each starts cold. Whether the cache is in-memory or
  in Postgres is never stated — and if it is in Postgres it is another unbounded
  table (MAJOR-10).

**Need:** state the threshold as a function (items/s at N workers, split tier-0 vs
tier-1), publish the load-test curve not the number, and say where the tier-0 cache
lives.

---

## MAJOR-13 — Migrations on API start with no expand/contract policy, no rollback, and a full-outage DDL window
**Attacks:** §6 (line 144), §9

"Migrations run on api start (idempotent, advisory-locked)". The advisory lock
correctly stops two API replicas racing — credit where due, that is more than most
drafts say. But:

- **Workers do not run migrations and can start against an old schema.** Compose
  `depends_on` with a healthcheck is not mentioned. A worker booting before the API
  finishes migrating either crashes on a missing column or, worse, runs against a
  half-migrated schema.
- **No expand/contract discipline.** For zero-downtime the rule must be stated: ship
  additive migration → deploy code that writes both → backfill → deploy code that
  reads new → drop old. Nothing in §6 or §9 says this, so the first rename will be
  a hard outage.
- **DDL at app start means a blocking `ALTER TABLE` on `results` (500k+ rows/run)
  takes an `ACCESS EXCLUSIVE` lock and the API is simply down for its duration.**
  With `results` at hundreds of millions of rows, some `ALTER`s will exceed any
  reasonable deploy timeout.
- **No down-migrations, no rollback story.** §6 describes deploy; nothing describes
  rollback. "Revert the image" against an already-applied destructive migration
  leaves the old code reading a schema it does not understand. For a system that
  gates other teams' releases, "we cannot roll back" is a serious gap.

**Need:** migrations as a separate one-shot job (not an API-start side effect),
worker gated on migration completion, a documented expand/contract rule, and an
explicit statement of which migrations are reversible.

---

## MINOR-14 — No idempotency key on run creation: a CI retry doubles the spend
**Attacks:** §4, §5.1, G3

CI clients retry. A `POST /runs` that times out client-side but succeeded server-side
leaves an orphan run burning budget and workers, and the retry enqueues a second one.
`spec_hash` (§5.1) is the natural dedupe key — same spec, same dataset, already
running → return the existing `run_id` — but nothing in the document does that. A
one-line `Idempotency-Key` header plus a unique partial index on
`(project, spec_hash) WHERE status IN ('queued','running')` closes it.

---

## MINOR-15 — No per-item deadline, no backup/PITR for the system of record
**Attacks:** §5.5, §6, §3

- §7's contract tests cover a provider "timeout", so timeouts are contemplated in the
  client — but no *item-level* deadline is stated. A client retry loop of
  `retries × timeout` can exceed the lease and the drain window (BLOCKER-2). The
  budget must be stated as an explicit chain: per-request timeout → per-item deadline
  → lease duration → grace period, each strictly less than the next.
- §3 says "not multi-region or HA in v1" — fine and honest. But this Postgres is the
  *system of record* for release decisions and the cost audit trail, and there is no
  backup, no PITR, no restore-drill, and no stated RPO/RTO anywhere. "No HA" is a
  choice; "no backups" is an accident. §5.8's audit-trail claim is only as good as the
  restore you have never tested.

---

## What the document already gets right (not findings)
To be fair, these are covered and I am not attacking them:

- Advisory-locked, idempotent migrations (§6) — correct instinct, just incomplete.
- `SKIP LOCKED` over Redis/RabbitMQ for v1 is the right call at this scale, and
  putting it behind a `JobQueue` protocol is the right hedge. The problem is the
  lease, not the choice.
- Canonical JSON with sorted keys and fixed float repr (§8) is exactly the right
  reproducibility mitigation.
- Tier-0 scorers receive a `ctx` with no provider client (§8) — genuinely good
  structural enforcement of the free-tier boundary.
- Judge cached by exact input, gate using tier-0 metrics (§8) — right instinct,
  insufficient for G1 (BLOCKER-4).
- Bounded-enum metric labels (§5.8) is the correct policy; §5.7 just violates it once
  (MAJOR-9).
- A run degrading rather than failing on budget exhaustion (§5.4) is the right
  product behaviour; it just needs the coverage rule from BLOCKER-5.
- Honest non-goals (§3) — no learned estimator, no UI in v1, no HA claim. Good.
- The determinism and load tests exist in §7 at all, which is more than most designs.

## Blocking summary
I would block a production launch on BLOCKER-1 through BLOCKER-5. BLOCKER-1, 2 and 3
are schema-level and cheapest to fix now. BLOCKER-4 and 5 are contract-level: they
mean the product's headline guarantees (reproducible digest, trustworthy gate) are
false in the presence of the two features that will be used most — budget limits and
the LLM judge.

Estimated rework: roughly one week to close all five, most of it in schema plus the
worker lifecycle. Cheaper than the first incident.
