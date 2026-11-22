# EvalForge

**Most LLM evals are statistically underpowered, and their gates pass regressions they were built to catch.**

This repository is an evaluation harness that computes the error bar before it computes the verdict — and says "I can't tell" instead of "pass".

```
$ evalforge power --n 108 --baseline 0.9
with 108 items this eval can only detect a regression of 11.4% or larger

$ # a real 6-point regression: 90% → 84%
n=50       verdict=inconclusive   (this eval can only see a 16.8% drop)
n=500      verdict=inconclusive   (5.3%)
n=10000    verdict=fail           (1.2%)
```

The same regression, the same code, three different answers — because the sample size decides what is *knowable*, not the threshold you picked. A gate that returns `PASS` at n=50 is not measuring quality. It is measuring noise and calling it a release.

---

## The claim, stated so it can be attacked

An eval harness reporting "96.1% recall" without an interval is doing numerology. With ~100 items, the 95% interval on a recall **difference** is roughly ±9 points. That means:

- Most "improvements" teams celebrate are inside the noise.
- Most "regressions" they block are too.
- And critically — a run too small to detect a regression will report the *absence of evidence* as *evidence of absence*.

EvalForge makes that impossible. The gate decides on confidence intervals, and it has a third outcome:

| Verdict | Exit | Meaning |
|---|---|---|
| `PASS` | 0 | the interval **rules out** a regression larger than tolerance |
| `FAIL` | 1 | the interval puts the regression **confidently above** tolerance |
| `INCONCLUSIVE` | 2 | the sample is too small to rule it out either way |

`INCONCLUSIVE` is the product. A two-state gate forces a lie: it either blocks a release over noise or passes a run it could not have measured.

## Quickstart

```bash
git clone https://github.com/Hemakrishna7406/evalforge && cd evalforge
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

evalforge power --n 108 --baseline 0.9      # what can this eval see?
pytest -q                                    # 23 tests
evalforge run --dataset data/sample.jsonl --out ./runs/a
evalforge gate --candidate ./runs/a/run.json --baseline ./runs/a/run.json
```

## What's inside

| Module | Responsibility |
|---|---|
| `stats.py` | Wilson intervals, difference intervals, MDE, required-n, bootstrap. Dependency-free, seeded, deterministic. |
| `gate.py` | Interval-based three-way verdict, per-category CIs, power report. |
| `digest.py` | Run fingerprint that **includes coverage**, so a truncated run cannot masquerade as a complete one. |
| `ledger.py` | Atomic budget reservations; overrun is measured and blocks a pass. |
| `queue.py` | Lease-based item claims with heartbeat, reaper, dead-lettering. |
| `egress.py` | Denies network to tier-0 scorers at the socket, not the API. |
| `spec.py` | Run identity: what makes two runs the same run. |

Two numbers this repository refuses to hide: every report carries its **minimum detectable effect**, and every gate verdict carries the **interval it was decided on**.

---

## How this was designed

Built with Google's [Elephant-Goldfish Model](https://github.com/google/elephant-goldfish-model): an "Elephant" session holds context and writes a design document with no code; "Goldfish" sessions are stateless agents that review it adversarially, sharing none of the author's assumptions.

Three Goldfish — an SRE, a staff engineer, and a security/readiness reviewer — were given only [`DESIGN.md`](DESIGN.md). All three independently found the same five defects, and their verdict was **NOT READY**:

| Blocker | Why v1 was wrong |
|---|---|
| Budget vs digest | Budget decided which items got scored, so the same spec produced different digests. The reproducibility guarantee was false. |
| Tier-0 isolation | "A context object with no client" is capability *hiding*. A scorer can `import httpx`. |
| Reserve/settle | One reviewer **simulated it**: ₹100 ceiling, 8 workers, est ₹10 / actual ₹30 → settled **₹120**. |
| The gate | A run skipping 40% of items measured a different population and could pass a real regression. |
| The queue | No lease, no reaper: a `kill -9` stranded the item as `running` forever. |

Every finding is answered in [`DESIGN_v2_RESOLUTIONS.md`](DESIGN_v2_RESOLUTIONS.md) — with a decision, not a defence. Where a claim could not be defended it was **removed**: multi-tenancy was dropped entirely, because claiming an isolation control you never test is worse than not having one.

The reviews are checked in unedited in [`docs/reviews/`](docs/reviews/). Publishing the criticism next to the code is the evidence that the design survived review rather than avoided it.

## What is built, and what is not

**Working and tested (23 tests):** statistics and power analysis; interval-based gate with per-category CIs; coverage-aware digest; budget ledger with overrun accounting and TTL reaper; lease-based queue with dead-lettering; tier-0 egress guard; four free scorers; CLI (`run`, `gate`, `power`).

**Designed, not built — stated rather than implied:**
- **Postgres backend.** SQL is written for both dialects and `claim()` emits `FOR UPDATE SKIP LOCKED` under Postgres, but only SQLite is exercised by tests. Largest gap.
- REST API, worker process, Docker Compose, Prometheus export (`DESIGN.md` §4–6).
- LLM judge (tier 1): budget plumbing and failure path exist; the provider client does not.
- Multi-tenancy: v0.1 is single-tenant, and says so.
- Plugin sandbox: `egress.blocked()` stops accidents, not hostile code.

## Known limits

- **The statistics are frequentist and two-proportion.** No sequential testing yet, so peeking at a run mid-flight inflates error rates — a real limitation, documented rather than hidden.
- **Cross-machine determinism is not claimed.** Canonical JSON with fixed float formatting handles dict ordering; it does not handle BLAS dispatch or Unicode normalisation.
- **The egress guard is not a sandbox.** It patches stdlib entry points.
- **The gate compares recall by category.** False-alarm rate is reported but not yet interval-tested.

## Roadmap

1. **v0.2** — sequential/always-valid inference so peeking is safe; interval-tested false alarms; Postgres backend with integration tests.
2. **v0.3** — LLM judge with measured TPR/TNR propagated into the final interval, so judge noise is part of the error bar rather than invisible.
3. **v0.4** — plugin sandbox (subprocess + seccomp); multi-tenant RLS with an isolation test that attempts a cross-tenant read.

## AI assistance

Design and adversarial review were AI-assisted under the Elephant-Goldfish pattern; the reviews in `docs/reviews/` were produced by three stateless reviewers given only the design document. Human judgement chose which blockers to fix, which to bound, and which to defer — and the deferrals are listed above rather than omitted.

## License

MIT
