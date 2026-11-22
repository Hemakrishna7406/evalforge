# EvalForge — production evaluation harness
### Design document (Elephant phase, EGM)

**Status:** draft for Goldfish review · **Author:** Hema Krishna · **Date:** 2026-11-22

---

## 1. Problem

Teams shipping LLM and translation features have no trustworthy way to answer "did this release make quality worse?" Existing options fail in specific, predictable ways:

- **Notebooks and one-off scripts** produce a number nobody can reproduce six weeks later. No versioning of dataset, prompt, model, or scorer.
- **Managed eval SaaS** can't score proprietary metrics, can't run inside a VPC, and prices per-sample in a way that punishes regression testing.
- **Offline harnesses** (including my own take-home) measure quality well but ignore production concerns: no concurrency control, no rate-limit handling, no cost ceiling, no metrics export, no CI gate, no audit trail.

The result: evaluation runs are an occasional ritual, not a gate. Regressions ship.

**EvalForge is a self-hostable evaluation service that makes evaluation a CI gate**: versioned runs, pluggable scorers, bounded cost, exported metrics, and a pass/fail verdict a pipeline can act on.

## 2. Goals

| # | Goal | Measure of success |
|---|---|---|
| G1 | Reproducible runs | Same run spec + dataset version ⇒ identical result digest, byte-identical item records |
| G2 | Cheap by default, expensive on purpose | ≥80% of items scored with zero model calls; paid scorers gated by policy |
| G3 | Bounded spend | Hard cost ceiling enforced before each paid call; run degrades, never overspends |
| G4 | Acts as a gate | `evalforge gate` exits non-zero on regression vs baseline; usable in CI in one line |
| G5 | Observable | Prometheus metrics, structured logs with run_id correlation, per-item traces |
| G6 | Extensible without forking | New scorer = one file + one registry line, no core changes |
| G7 | Self-hostable | `docker compose up` gives API + worker + Postgres + Prometheus in under 2 minutes |

## 3. Non-goals

- Not a training/eval-data labelling tool. Human review is supported but minimal (a queue + export).
- Not a model gateway or proxy. It calls providers; it does not serve them.
- No learned quality estimator. Scoring is explicit and inspectable by design; an opaque scorer cannot be debugged when it disagrees with a human.
- Not multi-region or HA in v1. Single-region, single-writer, with documented scaling path.

## 4. Architecture

```
                    ┌──────────────┐
   CI / CLI ───────▶│   REST API   │  FastAPI, OpenAPI, auth via API key
   Dashboard ──────▶│  (uvicorn)   │
                    └──────┬───────┘
                           │ enqueue run
                    ┌──────▼───────┐
                    │   Postgres   │  runs, items, results, baselines,
                    │              │  dataset versions, cost ledger
                    └──────┬───────┘
                           │ claim work (SELECT … FOR UPDATE SKIP LOCKED)
                    ┌──────▼───────┐
                    │   Worker(s)  │  async executor, N replicas safe
                    │              │
                    │ ┌──────────┐ │
                    │ │ Scorers  │ │  tier 0: deterministic (free)
                    │ │ registry │ │  tier 1: model-backed (budgeted)
                    │ └──────────┘ │
                    │ ┌──────────┐ │
                    │ │ Provider │ │  retry, backoff, rate limit,
                    │ │  client  │ │  circuit breaker, cost meter
                    │ └──────────┘ │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │  /metrics    │  Prometheus
                    └──────────────┘
```

**Why a worker queue and not in-process execution.** A run over 50k items outlives an HTTP request, must survive a deploy, and must scale horizontally. Postgres as the queue (`SKIP LOCKED`) avoids adding Redis/RabbitMQ for v1 while remaining correct with N workers; the interface is behind a `JobQueue` protocol so swapping in SQS or Redis later touches one file.

## 5. Core components

### 5.1 Run specification (immutable, versioned)

A run is fully described by a content-addressed spec:

```python
@dataclass(frozen=True)
class RunSpec:
    dataset: DatasetRef          # name + content hash
    scorers: tuple[ScorerRef, ...]   # name + version + config hash
    model_config: ModelConfigRef     # provider, model id, params hash
    policy: EvalPolicy           # tier gates, sampling, budget
    baseline_run_id: str | None
```

`spec_hash` = SHA-256 over the canonical JSON. Two runs with the same spec_hash and dataset hash must produce the same digest. This is the reproducibility contract (G1) and is asserted in CI.

### 5.2 Datasets

JSONL with a declared schema: `id`, `input`, `reference?`, `metadata?`. Ingested via `evalforge dataset push` — validates, rejects duplicate ids and schema violations with line numbers, stores a content hash, and never mutates (append-only versions). Datasets are immutable; a change is a new version, so a run always points at exactly one.

### 5.3 Scorers

```python
class Scorer(Protocol):
    name: str
    version: str
    tier: int                      # 0 = free/deterministic, 1 = paid
    def score(self, item: EvalItem, ctx: ScoreContext) -> list[Finding]: ...
```

`Finding` = `{category, severity, span, evidence, confidence}`. Scorers are pure given `(item, ctx)`; `ctx` carries the provider client, budget handle, and cache. Discovery via entry points plus an explicit registry; a scorer is loaded only if the run spec names it, so a broken third-party scorer cannot break unrelated runs.

Shipped scorers v1: `exact_match`, `chrf`, `numeric_preservation`, `script_conformance`, `structure_integrity`, `repetition`, and `llm_judge` (tier 1, rubric-driven, cached, consistency-sampled).

### 5.4 Execution and cost

The executor walks tiers in order, per item, and stops when the policy says stop:

1. **Tier 0** runs on every item. Free, deterministic, cached by `(scorer, item_hash)`.
2. **Tier 1** runs only if `policy.judge_when` is satisfied — currently: tier-0 disagreement, or explicit sample rate. Each call is preceded by a budget reservation.
3. **Budget**: a transactional ledger. `reserve(estimated_cost)` before the call; `settle(actual)` after. If reservation would breach `policy.max_cost_inr`, the item is marked `skipped_budget` and the run continues to completion with partial results. **A run never fails because it ran out of money; it reports what it couldn't do.**

### 5.5 Provider client

One client, provider-agnostic, OpenAI-compatible by default. Responsibilities: exponential backoff with jitter on 429/5xx, per-provider token-bucket rate limiting, a circuit breaker that opens after N consecutive failures and fails fast, response caching keyed by exact request, and cost metering from declared price tables. Every call is logged with `run_id`, `item_id`, latency, tokens, cost.

### 5.6 Results, baselines, and the gate

`results` rows are per item, per scorer, per finding. Aggregation produces:
- **Metrics**: recall/precision per category, false-alarm rate, localization rate, MQM-style severity score, cost, latency p50/p95.
- **Digest**: SHA-256 over canonicalised items+metrics, excluding timings. Equal digest ⇒ identical quality outcome.

`evalforge gate --baseline <run> --fail-on regression` compares against a baseline and exits `1` when: any category's recall drops beyond tolerance, false alarms rise beyond tolerance, or cost exceeds budget. Tolerances are explicit CLI/config values, not magic numbers. This is what makes G4 real.

### 5.7 Observability

- **Metrics** (`/metrics`): `evalforge_items_total{status}`, `evalforge_scorer_duration_seconds{scorer}`, `evalforge_tokens_total{model}`, `evalforge_cost_inr_total{run}`, `evalforge_budget_skipped_total`, `evalforge_queue_depth`.
- **Logs**: JSON, one line per event, always carrying `run_id`/`item_id` when applicable.
- **Traces**: OpenTelemetry spans per item and per provider call; exporter configurable, no-op by default.

### 5.8 Security

- API keys hashed at rest (argon2), scoped per project, revocable; never logged.
- Provider credentials from env/secret manager, never persisted in the DB.
- Datasets and results are project-scoped; every query carries the project predicate (no cross-tenant reads).
- Container runs as non-root, read-only rootfs, no shell in the runtime image.
- No dataset content in logs or metrics labels (labels are bounded enums only — prevents cardinality blowup and PII leakage).

## 6. Deployment

`docker compose up`: `api`, `worker`, `postgres`, `prometheus`. Migrations run on api start (idempotent, advisory-locked). Helm chart is v2 scope; documented but not shipped in v1.

Scale path: workers are stateless and horizontally scalable; the DB is the only shared state; queue contention handled by `SKIP LOCKED` + short transactions. Documented threshold: at ~200 items/s the single Postgres becomes the bottleneck; the documented next step is a Redis queue behind the existing `JobQueue` protocol.

## 7. Testing strategy

| Layer | What | How |
|---|---|---|
| Unit | scorers, digest, cost ledger, spec hashing | pytest, no network |
| Contract | provider client against a fake HTTP server | httpx mock transport: 429, 500, timeout, malformed body |
| Integration | API + worker + Postgres | testcontainers, real migrations |
| Determinism | same spec twice ⇒ same digest | asserted in CI |
| Load | 10k items, 4 workers | documented throughput number, not a guess |

CI runs lint (ruff), type check (mypy strict on core), unit+integration tests, and a self-evaluation: EvalForge evaluates its own sample dataset and the gate must pass.

## 8. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Scorer author writes a network call in tier 0 | Silent cost, broken determinism | Tier is declared and enforced: tier-0 scorers receive a `ctx` with no provider client |
| Digest differs across machines (dict ordering, float formatting) | Reproducibility claim breaks | Canonical JSON with sorted keys and fixed float repr; asserted in CI |
| Postgres queue contention under many workers | Throughput ceiling | Short transactions, `SKIP LOCKED`, documented Redis path |
| LLM judge non-determinism | Unstable gate | Judge cached by exact input; gate uses tier-0 metrics for pass/fail, judge reported separately |
| Provider outage mid-run | Partial results | Circuit breaker + resumable runs (items are independent rows) |

## 9. Rollout

1. **v0.1** (this repo): the above, single project, compose deploy, CLI + API, 7 scorers, gate mode.
2. v0.2: Helm chart, Redis queue, S3 dataset storage.
3. v0.3: human review queue, scheduled runs, Slack/webhook notifications.

## 10. What I am deliberately not doing

Building a UI dashboard in v1. The API returns JSON and `/metrics` feeds Grafana; a bespoke dashboard is a week of work that teaches nothing about whether the harness is correct. If the harness is right, the UI is a weekend.
