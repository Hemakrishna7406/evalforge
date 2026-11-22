# Adversarial review — EvalForge design document

Reviewer role: principal/staff engineer. Scope: technical soundness and internal consistency only. Every finding below is grounded in a quoted line from DESIGN.md; section references are to the document.

Verdict: the goals are the right goals and the component decomposition is sound, but **the central claim — G1 reproducibility — is not achievable under the design as written**, and three of the supporting mechanisms (tier-0 isolation, the cost ledger, partial results) each break the claim they are supposed to protect. Six blockers, eight majors, one minor.

---

## 1. BLOCKER — The budget is inside the spec but outside determinism (§5.1, §5.4, G1)

`RunSpec` embeds `policy: EvalPolicy  # tier gates, sampling, budget`, so `max_cost_inr` is part of `spec_hash`. But the quantity that decides which items get scored is not the ceiling — it is the **ledger**, mutable shared state read and written concurrently by N workers.

Two runs with byte-identical `spec_hash` and dataset hash therefore need not produce the same digest. The set of items marked `skipped_budget` depends on how many workers raced, which worker claimed which row (`SELECT … FOR UPDATE SKIP LOCKED`, §4), whether a retry fired, and what the response cache was warm with. §5.1 states "Two runs with the same spec_hash and dataset hash must produce the same digest" and G1 states "Same run spec + dataset version ⇒ identical result digest". Both are false as designed.

There is a second-order contradiction here too: the doc never says whether ledger state is inside or outside the digest. §5.6 says the digest covers "canonicalised items+metrics, excluding timings" — which silently *includes* the `skipped_budget` population (pure ledger artifact) while saying nothing about spend. So the digest both over-covers (ledger noise is hashed) and under-covers (the reason for the noise is not).

The honest fix is not to remove budget from the spec — that would break §5.1's "A run is fully described by" — it is to make the *outcome* a deterministic function of the spec: deterministic item ordering, deterministic budget allocation (e.g. a fixed per-item reservation schedule derived from `spec_hash`), or accept that budget-constrained runs are a distinct spec class with their own digest domain.

## 2. BLOCKER — Tier-0 isolation is aspiration, not enforcement (§5.3, §8, §5.8)

§8's mitigation reads: "Tier is declared and enforced: tier-0 scorers receive a `ctx` with no provider client."

Omitting a field from a context object is not enforcement in a language where the scorer is imported into the worker's own process. `score(item, ctx)` can equally `import httpx`, `from evalforge.provider import ProviderClient`, read `os.environ["OPENAI_API_KEY"]`, or open a socket directly. §5.3's "Scorers are pure given `(item, ctx)`" is a comment, not a constraint — nothing checks purity and nothing can, in-process.

§5.8 does not help: "Container runs as non-root, read-only rootfs, no shell in the runtime image" constrains filesystem and subprocess, while the threat here is an outbound socket and a credential read.

The consequence is worse than "isolation is weak". §5.4 only takes a reservation for tier-1 calls — "Tier 1 … Each call is preceded by a budget reservation." A tier-0 scorer therefore bypasses the ledger **entirely** while its code can make arbitrary paid calls. That is precisely the risk row's own stated impact: "Silent cost, broken determinism."

This needs either real isolation (separate worker with egress denied, or a subprocess with no credentials plus a syscall filter) or an explicit admission that `ctx` is a convenience and the tier declaration is self-reported metadata. Note also that self-reported metadata is *load-bearing*: `tier` is a scorer-declared attribute (§5.3), so the budget gate trusts the party it is budgeting.

## 3. BLOCKER — "Never overspends" is arithmetically false whenever actual > estimate (§5.4, G3)

`reserve(estimated_cost)` before the call, `settle(actual)` after. The ceiling test guarantees `settled + outstanding_reservations ≤ max_cost_inr` **at reserve time only**. The instant a call settles above its reservation, the invariant is broken and nothing re-checks it.

Simulated with correct reservation accounting (i.e. *not* relying on the naive "forgot other in-flight reservations" bug), max ₹100, 8 workers:

| est | actual | settled | outcome |
|---|---|---|---|
| ₹10 | ₹10 | ₹100 | ceiling holds |
| ₹10 | ₹30 | **₹120** | 20% overspend |
| ₹10 | ₹12 | ₹96 | holds by luck |
| ₹5 | ₹40 | **₹120** | 120% of ceiling |

The overshoot is bounded by `max_in_flight × (actual − est)` — exactly the term the design never bounds. For `llm_judge`, actual cost is unknown before the call *by construction* (output length), so `actual > est` is the normal case, not an edge case.

Two further unspecified interactions make this worse:

- **Retries.** §5.5 specifies "exponential backoff with jitter on 429/5xx". The doc never says whether up to three provider calls are billed against one reservation. If they are not, retries escape the ceiling completely; if they are, `settle` settles three calls against a one-call estimate and overshoot becomes systematic.
- **The mirror error: over-reservation.** Reserve precedes the call, but the cache lives *inside* the client (§5.5: "response caching keyed by exact request"). A guaranteed cache hit still reserves, so items get marked `skipped_budget` for calls that would have been free — degrading the run's coverage for no financial reason, and feeding finding 10.

G3's "Hard cost ceiling enforced before each paid call; run degrades, never overspends" should be restated as a soft target with an explicit bounded overshoot, or reserve must be re-validated pre-flight against a 2× estimate and the call abandoned.

## 4. BLOCKER — The ledger has no release path, so reservations leak (§5.4, §5.5, §8)

`reserve` is specified. `settle` is specified. Nothing else is. Three ordinary paths never reach `settle`:

1. circuit-breaker fail-fast (§5.5: "opens after N consecutive failures and fails fast");
2. a non-retryable provider error;
3. a worker crash or redeploy mid-call — and §4 explicitly designs for this: "must survive a deploy".

Each leaves an outstanding reservation that is never released. Because §5.4's check is `reserved + settled ≤ max`, leaked reservations **monotonically shrink the budget for the remainder of the run**. A run that loses 5% of its workers to a rolling deploy silently scores far fewer items, and the cause is invisible in the metrics.

Relatedly, there is no `skipped_provider` status. §5.4 defines exactly one non-scored state, `skipped_budget`, while §8 relies on "Circuit breaker + resumable runs" as the outage mitigation. So a breaker-open item has no defined state in `evalforge_items_total{status}` (§5.7) and no defined ledger interaction.

Needs: reservation rows carrying owner identity and a TTL, an explicit `release()`, and a reconciliation sweep on worker start. This is a small amount of design the document currently skips.

## 5. BLOCKER — The scoring model cannot represent "not scored", so partial runs collide with complete runs (§5.3, §5.4, §5.6)

`score()` returns `list[Finding]`. The empty list means both "scored, nothing wrong" and — via §5.4's `skipped_budget` — "never looked at". §5.6's digest is "SHA-256 over canonicalised items+metrics".

Unless the canonicalisation carries an explicit per-item scored/skipped marker — never stated — a run that scored 100% of items and a run that skipped 30% of them can hash identically, and §5.6's "Equal digest ⇒ identical quality outcome" is false.

This is not a hypothetical state. §5.4 *mandates* it: "the item is marked `skipped_budget` and the run continues to completion with partial results." And the ambiguity is silent — a skipped item is indistinguishable from a clean item to every downstream consumer, including the gate in finding 10 and any human reading an exported report.

The protocol needs a third outcome (`Finding` vs `NotScored(reason)`), and the digest needs the outcome discriminator inside the canonical form.

## 6. BLOCKER — The spec does not pin the things that determine the output (§5.1 vs §1, §5.3, §5.5)

§1 indicts existing tooling for "No versioning of dataset, prompt, model, or scorer." `RunSpec` then contains `dataset`, `scorers`, `model_config`, `policy`, `baseline_run_id` — and **no prompt or template reference at all**.

Four concrete gaps, each of which changes the digest with no change to `spec_hash`:

1. **The judge rubric.** §5.3 says `llm_judge` is "rubric-driven". The rubric is a prompt; it appears nowhere in the spec. The `Scorer` protocol has no config field, no factory, no `configure()` — so there is also no stated mechanism by which `ScorerRef`'s "config hash" is ever bound to a scorer instance. The spec names a config hash for a config the protocol cannot receive.
2. **Scorer code.** `ScorerRef` is "name + version + config hash" — a *string* version, not a code hash. A scorer whose behaviour changes without a version bump changes the digest undetected.
3. **Scorer dependencies.** `chrf` is a library call. An upgrade to the underlying metrics implementation changes scores for a fixed `spec_hash`. Dependency versions are absent from the spec.
4. **Provider model drift and price tables.** `model_config` is "provider, model id, params hash" — no provider-side model snapshot, so a silent model update changes tier-1 output for a fixed spec. And §5.5's "declared price tables" feed the `cost` metric that §5.6 puts in the digest, so a price-table update changes the digest.

The reproducibility contract is asserted over a spec that omits most of its own inputs. Either the spec grows (prompt refs, code hashes, lockfile digest, model snapshot, price-table version) or G1 should be scoped to "same spec *and same deployment image*".

## 7. MAJOR — Tier-1 selection is a coin flip with no seed (§5.4, §5.1, G1)

"Tier 1 runs only if `policy.judge_when` is satisfied — currently: tier-0 disagreement, or explicit sample rate."

Disagreement is deterministic. A **sample rate** is a random draw, and there is no seed in `RunSpec`, nor any statement that the draw is derived from `(spec_hash, item_id)`. A 10% sample rate yields a different item subset per run, hence a different digest for the same `spec_hash`. One line in the spec fixes it; as written, G1 and the sampling feature are in direct conflict.

## 8. MAJOR — `Scorer.score` is synchronous while everything it must call is asynchronous (§5.3 vs §5.5, §4)

The protocol is `def score(self, item, ctx) -> list[Finding]`. The worker is an "async executor" (§4) and the provider client provides "exponential backoff with jitter", "per-provider token-bucket rate limiting" and a "circuit breaker" (§5.5) — all inherently async.

A synchronous scorer calling that client must either block the event loop (serialising the worker's concurrency, which defeats the async executor and the horizontal-scaling argument) or be bridged through a thread pool — a bridge the document never mentions. And `ctx` "carries the provider client, budget handle, and cache", so a tier-1 scorer is expected to perform a DB transaction (reserve) and a network call (judge) inside a synchronous method.

This will surface on the first day of `llm_judge`. Either the protocol is `async def score`, or tier-1 is a separate interface from tier-0.

## 9. MAJOR — The tier-0 cache key omits scorer config (§5.4 vs §5.1, §5.3)

Tier-0 is "cached by `(scorer, item_hash)`", but a scorer's identity per §5.1 is "name + version + config hash". Two runs using the same name and version with different configuration hit the same cache entry, so the second run silently reuses the first run's findings.

That is a correctness bug and a digest bug simultaneously: it makes the digest depend on cache contents rather than on the spec, which is a second, independent route to finding 1. The key must be `(name, version, config_hash, item_hash)`.

## 10. MAJOR — The gate has no coverage guard, so partial runs pass (§5.6 vs §5.4, G4)

§5.4 makes budget exhaustion a normal, designed outcome. §5.6 then compares metrics against a baseline and exits 1 only when "any category's recall drops beyond tolerance, false alarms rise beyond tolerance, or cost exceeds budget". Nothing checks how many items were actually scored, or whether the scored subset is representative.

Worse, the skipped subset is **systematically biased**: tier-1 fires on "tier-0 disagreement", so under budget pressure the items dropped are exactly the contentious ones — the ones that move recall. A run that scored 12% of items can therefore report *better* recall than baseline and exit 0.

G4 says the gate "exits non-zero on regression vs baseline". It must equally refuse to certify a run whose coverage fell below a threshold (and refuse to compare across mismatched coverage). As written the gate is worse than no gate, because it is confidently wrong.

## 11. MAJOR — "Cost exceeds budget" as a gate failure is either dead code or a confession (§5.6 vs §5.4, G3)

§5.4: "A run never fails because it ran out of money; it reports what it couldn't do." §5.6 lists "cost exceeds budget" among the conditions where `gate` exits 1.

If the ledger works, this condition is unreachable — spend is capped by construction, so the branch can never fire. If it fires, the cap leaked (finding 3) and G3 is false. One of the two must go.

There is also an unresolved ambiguity about *which* budget: the spec's `policy.max_cost_inr`, or the gate's own tolerance, since §5.6 says "Tolerances are explicit CLI/config values, not magic numbers." A gate that re-derives its own ceiling can disagree with the ceiling the run was executed under — and then the comparison in §5.6 ("cost exceeds budget") is comparing two different budgets.

## 12. MAJOR — The gate is structurally blind to the regressions the product exists to catch (§8 vs §5.6, §1, G4)

§8: "gate uses tier-0 metrics for pass/fail, judge reported separately."

§5.3's tier-0 scorers are `exact_match`, `chrf`, `numeric_preservation`, `script_conformance`, `structure_integrity`, `repetition` — all surface-form checks. The only semantic-quality scorer, `llm_judge`, is tier 1 and is explicitly excluded from the verdict.

So the gate can pass a release whose *meaning* regressed while its character n-grams held — the central failure mode of LLM and translation systems, and the reason §1 says regressions ship. Excluding a noisy judge from a hard gate is a defensible engineering trade, but the document presents G4 as "exits non-zero on regression" without qualification. It should say plainly: v1 gates surface-form quality only.

Separately, §5.6 reports precision, localization rate and an "MQM-style severity score", but gates only on recall, false alarms and cost. A severity or precision regression passes the gate. Either gate on the metrics you report or say which are advisory.

## 13. MAJOR — The cross-machine mitigations do not cover the named failure modes, and the CI test cannot test them (§8 vs §7)

§8 offers "Canonical JSON with sorted keys and fixed float repr; asserted in CI." That covers dict ordering and float formatting, and nothing else on the list:

- **Unicode normalisation** — NFC vs NFD in `input`/`reference` and in `chrf` inputs. Unaddressed, and trivially reachable from real JSONL.
- **Locale/collation** — case folding and sort order are locale-sensitive; "sorted keys" says nothing about the sort of *values* or of collation-dependent ordering.
- **`-0.0`, `NaN`, `Inf`** — no repr specified. `NaN != NaN` also breaks naive equality assertions, so the CI test can pass while the digest is unstable.
- **List ordering** — findings emitted from set or dict iteration are unordered. Sorting dict *keys* does not sort lists, and `list[Finding]` is a list.
- **Library and model drift** — findings 6.3 and 6.4. Neither canonical JSON nor float repr touches these.

And the mitigation's own verification is §7's Determinism row: "same spec twice ⇒ same digest … asserted in CI" — same machine, same image, same warm cache. It is structurally incapable of detecting any of the cross-machine risks it is cited against. A real test needs a second pinned image, a normalisation fixture corpus, a cold cache, and tier-1 enabled.

## 14. MAJOR — "Byte-identical item records" contradicts the specified record contents (§5.6 vs G1, §5.5)

G1 promises "byte-identical item records" for two runs of the same spec. §5.5 requires that "Every call is logged with `run_id`, `item_id`, latency, tokens, cost".

So the per-item record carries a per-run identifier and a wall-clock latency — both differ on every execution by definition. §5.6 patches the *digest* by "excluding timings", but the digest is not the item record; G1's byte-identity claim is made about the record.

Either the item record excludes `run_id` and latency (making §5.5's correlation requirement false for the item-scoped record) or G1's byte-identity is unachievable. Define the field set of "item record" explicitly and say which claims apply to it versus to the digest.

## 15. MINOR — Metric labels contradict the bounded-enum rule (§5.7 vs §5.8)

§5.8: "No dataset content in logs or metrics labels (labels are bounded enums only — prevents cardinality blowup and PII leakage)."

§5.7 then declares `evalforge_cost_inr_total{run}` and `evalforge_tokens_total{model}`. `run` is a per-run identifier: one permanent time series per run, monotonically unbounded — the textbook Prometheus cardinality blowup the same document warns against. `model` is bounded only if model ids are enum-checked, which is not stated, and §5.5's client is "provider-agnostic, OpenAI-compatible by default", i.e. ids come from config.

Either aggregate cost by project and expose per-run cost through the API, or state an explicit retention/relabel limit.

---

## Smaller items, below the reporting threshold

- §6 asserts "at ~200 items/s the single Postgres becomes the bottleneck" while §7 promises the load test will produce a "documented throughput number, not a guess". The doc guesses first.
- §4 claims swapping the queue "touches one file", but §5.4's reserve/settle correctness depends on the claim and the reservation sharing a transaction. Moving to SQS/Redis (§6 scale path) changes the ledger's correctness argument, not just the queue implementation.
- §5.8's multi-tenancy claims ("every query carries the project predicate", "scoped per project") cannot be exercised in v0.1, which §9 defines as "single project". Fine as forward-looking, but the isolation is untested by construction.
- §5.2 stores "a content hash" of the JSONL as ingested. Byte-level hashing means a whitespace or key-order change creates a new dataset version with identical semantics; ingest-side canonicalisation is unstated.

## What I would fix first

Findings 1, 3 and 5 are one cluster: the reproducibility contract needs a written definition of *what the digest is a function of*, and that definition must exclude ledger and cache state while including an explicit not-scored marker and a seed. Findings 2 and 6 are the second cluster: the spec is the contract, and it currently omits the prompt, the scorer code and the dependency set — the three inputs most likely to change under a fixed hash. Everything else follows from those two.
