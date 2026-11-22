# Adversarial review plan — evaluation-harness design doc

**STATUS: NOT A REVIEW.** The target document (`/scratch/input/DESIGN.md`) was not
present in the sandbox — `/scratch/input/` contained zero files, and no `DESIGN.md`
existed anywhere on the filesystem. Nothing below is a finding about the document.
No section numbers are cited because none were read.

This file is the *instrument*: for each of the seven claims named in the brief, the
concrete failure modes that would break it and the specific check that confirms or
kills it. Each item is a hypothesis to test against the text, not a conclusion.

Severity is deliberately unassigned — it depends on what the doc actually claims and
how load-bearing each invariant is, which can only be judged from the text.

---

## 1. Reproducibility: content-addressed spec + digest (G1)

The claim under test is the implication *same digest => same results*. It fails if a
single input to behaviour sits outside the digest closure, or if behaviour is
nondeterministic given identical inputs. Attack the closure first, determinism second.

**Closure holes (digest under-specifies behaviour)**
- Digest covers the spec but not the **runtime**: base image referenced by a mutable
  tag rather than an image digest; unpinned transitive dependencies; no lock file;
  system libs resolved at build time.
- Digest covers *references* rather than *content*: a spec that names a git branch,
  a `latest` model alias, a container tag, or a URL fetched at run time. Content
  addressing that resolves to "whatever was current" is not content addressing.
- Scorer **code outside the spec** — installed as a plugin, editable install, or
  entry-point resolved at import time. The digest then describes the configuration
  but not the measurement function.
- Tree-hashing that includes `mtime`, `__pycache__`, `.git`, or absolute paths →
  digest differs per checkout and per machine for semantically identical inputs.
- Canonicalisation gaps in the hash preimage: JSON key ordering, float `repr`,
  unicode NFC/NFD normalisation, integer-vs-string type coercion. Two specs that
  render identically can hash differently, or (worse) two different specs collide.
- External mutable state reachable at run time: retrieval corpora, live web tools,
  HuggingFace cache dirs, any dataset fetched by URL instead of by pinned digest.

**Determinism holes (same inputs, different outputs)**
- Provider sampling: undated model alias, server-side seed not honoured,
  `temperature > 0`, `top_p`, or batching-dependent MoE routing on the provider side.
  Provider-side nondeterminism at `temperature=0` is real and must be acknowledged,
  not assumed away.
- Float reduction order: BLAS threading, GPU kernels, atomics, TF32/cuDNN autotune
  (`benchmark=True`), `torch.use_deterministic_algorithms` unset.
- Harness itself: `PYTHONHASHSEED` unset → set/dict iteration order varies per
  process; `os.listdir` order varies by filesystem (ext4/xfs/overlayfs); worker
  count changing RNG stream partitioning; task scheduling order mutating shared state.
- Clock/locale leakage: `datetime.now()` in a prompt or seed; `TZ` / `LC_ALL`
  affecting date parsing, float formatting, or sorting. Hostname/PID/uid in prompts.

**The check that settles it:** take one digest, run it on two machines with
*different CPU generations* (e.g. AVX2-only vs AVX-512) and different core counts,
then diff the per-item outputs and the aggregate. Then ask: is the digest computed
over the *transitive closure* of everything that can affect output, or over the
spec object? If the doc asserts reproducibility without stating the closure and the
tolerance for numeric drift, the claim is aspirational.

**Second-order risk:** if the digest is also the cache key, an under-specified digest
turns a reproducibility bug into *silent cache poisoning* — cross-machine hits serve
wrong results with no error. Worse failure mode than no cache at all. Check whether
the doc treats digest-as-identity and digest-as-cache-key as the same thing.

---

## 2. Scorer protocol: tier-0 gets "a ctx with no provider client"

The question is whether this is an enforced capability boundary or a naming
convention. Absence of an attribute on a passed-in object is not a security or
correctness boundary in Python.

- **Construction from ambient state.** `import openai; openai.OpenAI()` builds its
  own client from environment variables. If credentials are in the process
  environment — and they are, because other tiers need them — a tier-0 scorer can
  reconstruct the client the ctx withheld.
- **The real boundary is egress, not the ctx.** `requests`, `urllib`, `socket`,
  `http.client` are all importable. Unless the network is blocked at the sandbox
  level (netns / seccomp / proxy allowlist), the ctx is decoration.
- **In-process sharing.** If tier-0 runs in the same process as the harness, it can
  read the harness's provider credentials directly, monkeypatch the ledger, mutate
  another scorer's state, or read the full prompt. Isolation must be a process
  boundary, not an object shape.
- **Type-level enforcement is bypassable and also breaks honest use.** `AttributeError`
  on `ctx.provider` is one `getattr(ctx, "provider", None)` or one `__dict__` walk
  from irrelevant. Meanwhile legitimate scorers that need a tokenizer or model
  metadata are blocked, since a tokenizer is a model artifact — the boundary is
  drawn in the wrong place if it forbids all model access rather than all *paid
  generation* access.
- **Tier assignment is trusted input.** What stops a costly or networked scorer from
  being registered as tier-0 to escape cost accounting? If tier is a declaration in
  the registry rather than a property of the sandbox it runs in, the cost ledger's
  completeness depends on plugin honesty.
- **Hangs and cancellation.** An in-process scorer that blocks cannot be cancelled
  from another Python thread. Is there a hard timeout via subprocess kill, or a
  cooperative one that a malicious/buggy scorer ignores?
- **Does tier mean cost or trust?** If the same word covers "cheap" and "sandboxed",
  the taxonomy collapses: a scorer can be cheap and untrusted, or expensive and
  trusted. Check whether the doc conflates the two axes.

**The check:** can a tier-0 scorer, written adversarially in ten lines, reach the
network or the harness's credentials? If yes anywhere, the enforcement is aspirational.

---

## 3. Cost ledger: reserve / settle under concurrency

Standard two-phase budget accounting. The failure modes are well known and each is a
specific question to ask of the design.

- **Check-then-act.** "Read balance, decide affordable, write debit" is not atomic.
  Needs a single conditional update (`UPDATE ... WHERE balance >= :amt`), a
  serialisable transaction, or a single-writer actor. Look for the word "atomic"
  next to the actual mechanism, not just next to the intent.
- **Reserve without settle.** Process crashes between reserve and settle → the
  reservation leaks permanently and the budget silently shrinks run over run. Is
  there a TTL plus a reaper? Who reclaims, and how does the reaper distinguish
  "crashed" from "slow"? TTL expiry under NTP skew.
- **Settle without reserve / double settle.** Retries and at-least-once delivery
  double-charge unless settle is idempotent on a key like `(run_id, attempt_id)`.
  Is the ledger idempotent, or does it rely on the caller to not retry?
- **Reserve is an estimate; actual can exceed it.** Token counts exceed `max_tokens`
  estimates, retries multiply cost, prompt grows after compaction. If settle can push
  the balance negative, the no-overspend invariant is false. If settle is clamped to
  the reserve, the ledger *under-reports* and the invariant is true only because the
  books are wrong. Which one is it? The doc must pick and say so.
- **Sum-of-reservations invariant.** With N concurrent workers, `sum(open
  reservations) + settled <= balance` must hold at every instant. That requires the
  reservation to be a first-class row with state, not an in-memory counter.
- **Stale reads.** If the ledger is read from a replica or a cache, overspend follows
  directly. Is the read path strongly consistent?
- **Unknown cost after a lost response.** Provider charges you, response times out,
  you never learn the usage. Is the charge assumed to be the maximum? If not, the
  ledger drifts from the real bill with no reconciliation path. Is there
  reconciliation against provider usage logs?
- **Provider-level idempotency.** If the ledger dedupes a retry but the provider
  charged for both attempts, the books balance and the invoice doesn't.
- **Unit drift.** Tokens vs money vs "credits"; price table versioning. If prices
  change mid-run, the recorded cost no longer explains the recorded digest.

---

## 4. "Never fails because it ran out of money" + partial results + the gate

This claim quietly conflates two different things: *the process doesn't crash* and
*the measurement is still valid*. The first is achievable; the second is where it
breaks.

- **Truncation biases the metric.** A run that stops early yields a non-random
  subset. If scheduling is FIFO or shard-ordered, the dropped items are systematically
  the expensive/hard/late ones — so the partial score is biased upward or downward
  in a way that looks like a real result. Reproducible *and* wrong.
- **The gate must carry completeness.** If the gate emits partial results without a
  `completed / planned` marker, the metric layer will happily compare a 40% run
  against a 100% run. Does the gate emit a status, and does the comparison layer
  refuse mixed completeness?
- **Contradiction with G1 — the sharp one.** A partial run and a complete run can
  carry the *same digest* unless budget/limits are inside the digest. Two options,
  both consequences: either the digest includes the budget (then the same spec at a
  different budget is a different identity, which is probably correct), or it doesn't
  (then "same digest => same results" is false, and a cache can serve a truncated
  result as if complete). Check which the doc chose.
- **Shared budgets break reproducibility.** If several runs draw on one pool, a
  concurrent run draining it moves *your* truncation point. The number of completed
  items then depends on scheduling noise — a direct conflict between the budget claim
  and the reproducibility claim.
- **Item-level atomicity.** Can the budget run out *mid-item*, leaving an item
  half-scored and its partial score folded into the aggregate? What is the unit of
  all-or-nothing?
- **Retry storm near exhaustion.** As the budget approaches zero, retries burn
  reserve and accelerate truncation, starving expensive-but-important items. Is
  there per-item reserve reservation rather than per-call?

---

## 5. Digest across machines: are the stated mitigations sufficient?

Whatever mitigations are listed, test them against this list of what they typically
miss:

- Image digest pins the image — not the **host kernel**, CPU microarchitecture, GPU
  model/driver/CUDA version mounted from outside, or container runtime.
- Lock file pins Python packages — not **BLAS implementation** (MKL vs OpenBLAS give
  different summation order), not libgomp/libstdc++ versions, not
  source-vs-wheel builds.
- CPU **dispatch** picks different SIMD kernels at import time on different hosts →
  last-bit float differences → argmax flips on ties → different scores. This is the
  classic silent cross-machine divergence.
- `PYTHONHASHSEED`, `TZ`, `LC_ALL`, filesystem ordering, hostname/PID — all
  environmental and all absent from a package lock.
- Aggregation: summing scores across shards in different completion order produces
  last-bit differences; a threshold comparison then flips a pass/fail.
- Tokenizer version — text normalisation changes token counts, which changes both
  cost and output.

**The check:** state the claim operationally. "Same digest ⇒ byte-identical results"
is a much stronger claim than "same digest ⇒ same results within tolerance", and the
doc should say which. Then run the two-machine diff. If the mitigations are a list of
pins without a stated tolerance and a CI job that actually diffs two hosts, they are
insufficient by construction — you cannot verify a determinism claim you never test.

---

## 6. Plugin / registry: is the extensibility claim credible?

Extensibility claims are credible only when protocol versioning, isolation, and
trust are all addressed. Check each:

- **Plugins are arbitrary code in the harness trust domain.** If they load in-process
  via entry points, they can read provider credentials, mutate the ledger, and
  exfiltrate prompts. This makes finding 2 moot: "tier-0 gets no provider client"
  means nothing if any plugin can `import openai`.
- **Plugin version not in the digest** → the digest no longer reproduces. Extensibility
  and reproducibility are in direct tension here; the doc must say how a plugin's
  identity enters the digest.
- **Protocol stability.** Passing a live harness/`ctx` object to plugins couples them
  to internals; without a schema version and capability negotiation, upgrades break
  every plugin or, worse, silently misbehave.
- **Registry trust.** Who can register a scorer under an existing name? Name
  squatting or typo-squatting substitutes an attacker's metric for a real one. Is
  there namespacing, signing, or review?
- **Conformance kit.** Is there a golden-output test suite a third-party scorer must
  pass before it can influence a measurement? Without one, "extensible" means
  "untested code in the measurement path".
- **Cost model coverage.** Scorers consume compute, not just API money. Does the
  ledger account for scorer cost at all, or only provider spend?

---

## 7. Internal contradictions to look for while reading

Not findings — a checklist of pairs that must be reconciled across sections:

- Content-addressed spec vs. plugins/dependencies resolved at run time.
- "Never fails on budget exhaustion" vs. "reproducible results" (finding 4).
- "Tier-0 has no provider client" vs. in-process plugins / ambient credentials.
- "Digest pins everything" vs. budget, limits, or plugin versions outside the digest.
- "Reserve then settle" vs. "actual cost may exceed the reserve".
- Tier = cost vs. tier = trust (the taxonomy collapse in finding 2).
- Any invariant asserted in an overview section and quietly weakened in a later
  "limitations" or "future work" paragraph — check whether the strong claim or the
  weak caveat is the one the rest of the system relies on.

---

## How to use this

Read the doc with these as probes. For each claim, find the sentence that makes it,
then find the mechanism that enforces it. A claim with no mechanism behind it is the
finding; a claim whose mechanism doesn't cover its own wording is the more interesting
finding. Then assign severity by blast radius: does the flaw corrupt a measurement
silently (BLOCKER), or does it degrade an operational property loudly (MAJOR), or is
it a documentation/precision gap (MINOR)?
