# Goldfish Review — EvalForge design document (EGM)

Reviewed document: `DESIGN.md` (draft, 2026-11-22, Hema Krishna), 178 lines.
Reviewer position: no prior context, document-only.

Verdict: **NOT READY** for implementation from this document alone. See JOB 2.

---

## JOB 1 — Security and production-management review

### F1 — BLOCKER — Multi-tenancy is a convention, not a control, and the document contradicts itself on whether it exists
**Sections: §5.8 vs §9, §5.6, §5.4, §7**

§5.8 states "Datasets and results are project-scoped; every query carries the project predicate (no cross-tenant reads)." That is a coding discipline, not an enforcement mechanism. There is no row-level security, no per-project DB role, no repository layer named as the single choke point, and no isolation test anywhere in the §7 test matrix — the integration layer (§7) asserts API+worker+Postgres wiring, not that tenant A cannot read tenant B.

Worse, §9 says v0.1 ships **"single project"**. So the control is being claimed for a configuration that does not exercise it, which means it will never be tested before it matters. "Every query carries the project predicate" fails silently the first time someone writes `session.execute(select(Run))` without the filter — and there is no mechanism described that would catch it.

Unscoped paths that are not "every query" and are not addressed:
- **Aggregation** (§5.6): metrics and the digest are computed by "aggregation" over results rows. Nothing says the aggregator is project-scoped; a global aggregate is the natural implementation and produces a cross-tenant read (and a cross-tenant information leak via `/metrics` and gate output).
- **Cost ledger** (§5.4): the transactional ledger has no stated project dimension, so per-project spend — the thing a shared platform is billed on — is not derivable from the design.
- **Export / human-review queue** (§3): "a queue + export" is named as in-scope and given no scoping rule at all.

### F2 — BLOCKER — Third-party scorer code runs in-process with the worker's provider credentials
**Sections: §5.3, §8, §5.8**

§5.3 loads scorers by entry point + registry and calls `score()` in the worker process. §8's stated mitigation for the "scorer author writes a network call in tier 0" risk is that "tier-0 scorers receive a `ctx` with no provider client." That is capability *hiding*, not a boundary: a scorer can `import httpx`, `import requests`, `open()` any readable path, or `os.system()` whatever it likes. The `ctx` argument constrains only code that chooses to be constrained.

The blast radius is set by §5.8, which puts provider credentials in the worker's **env**. A malicious or merely compromised scorer therefore gets: every provider API key in the process environment, every item of every tenant flowing through that worker in memory, and the DB connection pool. Container hardening in §5.8 (non-root, read-only rootfs, no shell) protects the *host* from the container; it does nothing about code running *inside* the container, which is where scorers run.

Not addressed anywhere: no subprocess/worker isolation for scorers, no seccomp or network namespace, no egress allowlist, no package signing or hash pinning in the registry, no allowlist of approved scorer distributions, no per-scorer CPU/memory/time limit. "A scorer is loaded only if the run spec names it" (§5.3) limits *when* code runs, not *what* it can do. Note the same gap defeats the determinism goal in §8: a scorer that reads the clock or the network breaks G1 and nothing detects it.

### F3 — BLOCKER — Budget exhaustion silently turns the gate green; nothing halts a runaway run on a shared platform
**Sections: §5.4, §5.6, §5.5, §2 G3**

§5.4: when a reservation would breach `policy.max_cost_inr`, "the item is marked `skipped_budget` and the run continues to completion with partial results. **A run never fails because it ran out of money.**" §5.6: the gate compares "any category's recall drops beyond tolerance." Combine the two: recall is a ratio over scored items. A run that skipped 40% of items computes recall over the remaining 60% — a denominator the baseline did not have. The gate then compares two numbers computed over different populations and can pass a genuine regression, or fail a clean run, with no signal in the exit code that the comparison was unsound. The doc's own risk table (§8) does not list this. This is the single most damaging finding for the product's stated purpose (G4, §1).

Related holes in the same mechanism:
- **Per-run ceiling only.** `policy.max_cost_inr` is in the run spec (§5.1). Nothing caps a *project*, a *day*, or the *platform*. A tenant submitting 500 concurrent runs each under its own ceiling has no bound. There is no org-level budget, no quota, and no kill switch — by design, since the stated behaviour is to degrade rather than stop.
- **Estimate basis unspecified.** `reserve(estimated_cost)` before the call (§5.4) depends on a cost estimate; how tokens are estimated, and what happens when `settle(actual) > reserve`, is not stated. If the check-and-reserve is not one atomic statement (or `SELECT … FOR UPDATE`) per §4, N workers each pass the same check and collectively overshoot the ceiling.
- **Price tables are unversioned.** §5.5 meters cost "from declared price tables." Price tables are not in `RunSpec` (§5.1) and therefore not in `spec_hash`. A price change silently changes every run's cost, breaks the §5.6 "cost exceeds budget" gate check against an older baseline, and is not reproducible.

### F4 — BLOCKER — "Who ran what" is unanswerable, and the audit trail is not tenant-scoped or tamper-evident
**Sections: §5.1, §5.5, §5.7, §5.8**

§1 sells the audit trail as the thing offline harnesses lack. Four of the five questions are answerable, one is not:

| Question | Answerable? | Where |
|---|---|---|
| Which dataset | Yes — content hash in `DatasetRef` | §5.1, §5.2 |
| Which plugin version | Partly — `ScorerRef` carries name + version + config hash | §5.1, §5.3 |
| At what cost | Partly — ledger and per-call metering | §5.4, §5.5 |
| What ran | Yes — per-item rows | §5.6 |
| **Who ran it** | **No** | — |

`RunSpec` (§5.1) has no principal, no `created_by`, no API-key id. §5.5 logs provider calls with `run_id`/`item_id` — that is metering, not attribution. §5.8 says API keys are "scoped per project," so at best you learn *which project*, never *which key or which human*. For a CI gate that can fail a release, "who kicked off the run that failed the build" is a first-class question and the design cannot answer it.

The trail is also not tenant-scoped: §5.7 logs carry `run_id`/`item_id` only, never `project_id`. Whoever reads the log store reads all tenants. And nothing states that logs or the cost ledger are append-only, retained for a period, or tamper-evident — so the audit trail is as editable as any other row.

### F5 — MAJOR — No secret rotation story; a rotated key leaves stale workers serving
**Sections: §5.8, §6**

§5.8: "Provider credentials from env/secret manager, never persisted in the DB." §6: workers are "stateless and horizontally scalable," N replicas. Env is read at process start. Rotating a provider key therefore requires restarting every worker replica, and until the last one restarts, the fleet is split — some workers on the old (possibly revoked) key, some on the new. The doc describes no key version, no reload signal, no overlap window, no graceful drain. Because §5.5 has a circuit breaker that "opens after N consecutive failures and fails fast," a revoked-key window degrades a run into `circuit open` rather than an obvious auth error, which is a confusing failure mode for an operator at 2am.

API-key side (§5.8): keys are argon2-hashed and "revocable," but revocation latency is not defined. Verifying an argon2 hash on every request is expensive enough that any real implementation caches it — and the moment it caches, revocation has a stale window that the document does not bound. Also absent: key expiry, key prefixes/ids for lookup, and any read-only vs read-write distinction — so the key handed to a CI pipeline (§2 G4) can also push datasets and revoke keys.

### F6 — MAJOR — PII and data residency in eval datasets; provider egress is unbounded
**Sections: §5.2, §5.3, §5.5, §5.8**

Eval datasets are JSONL of `input` / `reference` (§5.2) — in this product's stated use case that is *real production text* from LLM and translation features. That is the highest-PII-density artifact in the system. The design has:
- No PII classification, redaction, or field-level encryption at ingest (§5.2 validates schema and duplicate ids only).
- No retention period and no deletion path. §5.2: datasets are "immutable… append-only versions." Immutability is right for reproducibility and directly conflicts with erasure obligations: you cannot remove one data subject's record. A "new version" does not delete the old bytes, and every historical run still points at them (§5.1). Under India's DPDP Act or GDPR this is a live compliance defect, not a nit.
- No data-residency statement at all. §3 says "single-region," but not *which* region, and not whether datasets may leave it.
- No provider egress control. §5.5 sends item content to an OpenAI-compatible endpoint; §5.8 forbids dataset content in *logs and metrics labels* while the far larger egress — the actual item text to a third-party model — is unconstrained. No provider allowlist, no per-project provider restriction, no egress DLP, no record of which provider saw which item beyond a per-call log line.
- Unstated and material: whether `llm_judge` (§5.3) sends `reference` to the model. If it does, labels leak to a third party and the judge is no longer an independent measurement.

### F7 — MAJOR — Tier-0 cache key omits scorer version and project
**Sections: §5.4, §5.5, §5.1, §2 G1**

§5.4: tier-0 results are "cached by `(scorer, item_hash)`." The scorer's **version** and **config hash** — both of which §5.1 puts in `ScorerRef` precisely because they change behaviour — are not in the key. Ship a fixed `chrf` as v1.0.1 and every run keeps serving v1.0.0 findings from cache: the fix appears to do nothing, and G1's "same spec ⇒ identical digest" contract is satisfied by two different scorer versions producing the same digest, which is exactly the wrong kind of stable. §5.5's response cache has the analogous problem ("keyed by exact request" — presumably includes model id and params; not stated).

The cache also has no stated project dimension. If it is shared, it is a cross-tenant surface: `item_hash` is content-derived so accidental collisions are not the risk — cross-tenant cache *poisoning* and hit/miss timing as an oracle for whether another tenant has evaluated identical text are.

Separately: no cache backend is named, and §5.8 specifies a **read-only rootfs** — so the obvious filesystem cache is unavailable and the design has not said what replaces it.

### F8 — MINOR — No API rate limiting or per-tenant fairness; queue depth is a global number
**Sections: §4, §5.7, §5.2**

§5.2's `dataset push` and §4's enqueue path have no request size cap, no rate limit, and no concurrency cap per project. `evalforge_queue_depth` (§5.7) is a single global gauge with no per-project label, and the queue (§4) has no priority or fair-share — so one tenant's 50k-item run occupies the `SKIP LOCKED` claim order and starves everyone else's 200-item CI gate. For a product whose pitch is "usable in CI in one line" (G4), a gate that queues behind an unrelated batch job for twenty minutes is a functional failure, not just an unfairness. The bounded-label rule in §5.8 is the right instinct; it should not be the reason per-tenant depth is unobservable.

---

## JOB 2 — Implementation readiness check

**Could a competent engineer implement this from the document alone, without asking questions? No.** The document is a good architecture memo and a poor build spec: it names components, contracts, and goals, and supplies essentially no schemas, signatures, or file layout. Below is what is specifically missing.

### Undefined data structures
- `DatasetRef`, `ScorerRef`, `ModelConfigRef`, `EvalPolicy` (§5.1) are named and never defined. `EvalPolicy` is the worst: it is said to carry "tier gates, sampling, budget" and later `judge_when` and `max_cost_inr` (§5.4) — the full field set, types, defaults, and units (₹? paise? per-run or per-item?) are absent.
- `EvalItem`, `ScoreContext` (§5.3) — no fields. `ScoreContext` is said to carry "provider client, budget handle, and cache"; the budget handle's API is not given.
- `Finding` = `{category, severity, span, evidence, confidence}` (§5.3) — no types. Is `span` char offsets, a list, source-side or target-side (material for a translation product)? What is the `severity` enum? What is the `category` vocabulary? This is blocking, not cosmetic: §5.6 metrics are "per category" and the gate compares per-category recall, so the entire gate contract rests on an enum that does not exist.
- No result row schema, no cost-ledger schema, no dataset manifest schema, no `status` enum — yet `{status}` is a metric label (§5.7) and `skipped_budget` (§5.4) is one value of an otherwise unstated set.
- `spec_hash` inputs are not enumerated (§5.1). Is `baseline_run_id` inside the hash? Is `policy`? (If policy is, a budget tweak invalidates every cached digest — a design decision, and it is not made.)

### Absent function signatures / interfaces
- `JobQueue` is named as *the* swap point for Redis/SQS (§4, §6) and never specified — no `enqueue`/`claim`/`ack`/`nack`/`heartbeat` signatures, no lease or visibility-timeout semantics. This is a correctness gap, not just a documentation one: `SELECT … FOR UPDATE SKIP LOCKED` (§4) releases the lock when the transaction ends, so a worker that dies mid-item leaves the row claimed-but-unfinished with no recovery rule. "Resumable runs (items are independent rows)" (§8) requires lease expiry or a heartbeat, and neither is specified.
- Provider client interface (§5.5): no signature, no return type, no error taxonomy beyond "429/5xx".
- Budget ledger (§5.4): only `reserve`/`settle` named. No signature, no isolation level, no rule for `settle > reserve`, no rule for what happens to a reservation when a worker crashes between reserve and settle (leaked reservation permanently consumes budget).
- Scorer registration: "entry points plus an explicit registry" (§5.3) — no entry-point group name, no registry file, no packaging metadata, no tier-declaration mechanism beyond an attribute.
- CLI (§2 G4, §5.2, §5.6): `dataset push` and `gate` are named; no argument grammar, no config file format, no auth handling, no exit-code table (only "exits 1").
- No API surface beyond "REST API, OpenAPI" (§4): no endpoint list, no auth header, no error envelope, no pagination.

### Unspecified file layout
There is no repository tree anywhere in the document. G6 promises "new scorer = one file + one registry line" (§2) without saying which directory, which registry, or how a scorer is packaged and distributed. Migration tooling is referenced (§6, "migrations run on api start") with no tool named and no migration list.

### Unclear semantics
- **Digest inputs (§5.6):** "canonicalised items+metrics, excluding timings." Does that include findings, spans, `confidence` floats, cost, cache-hit flags? Is judge output in the digest? §8 says the gate uses tier-0 metrics and the judge is "reported separately," which implies judge output may be excluded — but then G1's "byte-identical item records" (§2) is contradicted by the same document, since the item records contain judge findings.
- **Item ordering:** G1 requires identical digests, but with N workers (§4) item completion order varies. The digest must canonicalise by item id — no ordering rule is stated.
- **Gate comparison across unequal populations:** what happens when a category exists in the candidate but not the baseline, or when the candidate skipped items (F3)? Undefined. Default tolerances are said to be "explicit… not magic numbers" (§5.6) but no values or units are given.
- **Cache invalidation** (F7) and **cache backend** given the read-only rootfs (§5.8).
- **Float canonicalisation:** §8 says "fixed float repr" — which repr, which rounding, at which point in the pipeline?

### Idempotency of re-runs
Entirely undefined. Is a run immutable once created? Does re-submitting the same `spec_hash` return the existing run or create a new one? Is there a run-level retry distinct from item-level resume? What is the state machine for a run (queued → running → partial → complete → failed) and who advances it? Can a completed run be re-opened? For a CI gate that will be re-invoked on every commit, "run the same spec twice" is the most common operation in the system and its semantics are absent.

### Versioning rules
- `Scorer.version` (§5.3) is a free string with no format (semver?), no owner, no bump discipline. `ScorerRef` carries both `version` and a `config hash` (§5.1) — it is not stated whether a config change requires a version bump, or whether two runs differing only in config hash are "the same scorer" for gate comparison.
- No schema version on stored `Finding` rows, so a scorer that adds a field silently produces mixed-schema history that the aggregator (§5.6) must read.
- Dataset "version" is only a content hash (§5.2) — no human-readable label, no lineage, no deprecation policy.
- Price tables (§5.5) are unversioned and outside `spec_hash` (F3), so cost is not reproducible.

### Minimum additions to reach READY
1. **Schemas and signatures.** Concrete definitions for `RunSpec`/`EvalPolicy`/`EvalItem`/`ScoreContext`/`Finding`/result row/cost ledger/dataset manifest, with the `severity`, `category`, and item `status` enums closed and written down; `JobQueue` and provider-client protocols with signatures and a lease/heartbeat model; the CLI grammar and exit-code table.
2. **Data model.** DDL or an equivalent table list with keys and the indexes the queue and aggregation depend on, plus the migration tool and the v1 migration list.
3. **Repository layout and scorer packaging.** A tree, the registry file, the entry-point group name, and the distribution mechanism.
4. **Tenancy decision + enforcement.** State whether v1 is single- or multi-tenant (§5.8 vs §9 must agree). If multi-tenant: row-level security or a mandatory repository layer, project on the cost ledger, project on every aggregate and export, and a cross-tenant isolation test in the §7 matrix.
5. **Digest specification.** Exact input set, canonicalisation rules, item ordering, float repr, and an explicit statement of whether judge findings are in or out.
6. **Gate specification.** Metric definitions with denominators, behaviour under `skipped_budget` and mismatched category sets, and default tolerances with units.
7. **Budget model.** Reservation arithmetic, isolation level, crash-between-reserve-and-settle handling, a per-project ceiling in addition to the per-run one, versioned price tables inside the reproducibility contract, and an explicit statement that a run with material `skipped_budget` cannot produce a passing gate.
8. **Scorer isolation.** Subprocess or otherwise bounded execution, no provider credentials in the worker env visible to scorers, CPU/memory/wall-clock limits, and either signed scorer packages or an explicit allowlist.
9. **Secrets and identity.** Rotation procedure with the worker restart/drain story, API-key revocation latency bound, key scopes and expiry, and a recorded principal on every run.
10. **Data handling.** Retention period, deletion/erasure path for datasets, PII classification at ingest, provider egress allowlist, and the residency statement.

---

## Summary

The architecture is sound and the instincts are right — content-addressed specs, a Postgres queue behind a protocol, tier-gated spend, deterministic digests, honest non-goals. The gap is between the security posture the document *claims* (§5.8) and what it *specifies*: multi-tenancy is a coding convention asserted for a v1 that has one tenant, the audit trail cannot name a principal, the cost ceiling degrades instead of stopping and quietly corrupts the very gate it exists to protect, and third-party scorer code runs in-process holding every provider key. None of those are hard to fix; all of them are invisible in the current text. The document is not implementable as written and is not yet safe to implement as written.

**Verdict: NOT READY.** The minimum additions in JOB 2 items 1–7 would be enough for an engineer to start; items 8–10 are required before the system handles more than one tenant's data or any third-party scorer.
