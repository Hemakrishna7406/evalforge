# Contributing to EvalForge

The most useful contribution to this project is **a statistic that proves one of our claims wrong.**

## What we want most

1. **Falsify a claim.** The README asserts that most evals are underpowered and that our gate
   avoids passing regressions. If you can construct a case where the gate returns `PASS` on a
   real regression, that is a bug and we want the test that demonstrates it.
2. **A missing statistical assumption.** We use frequentist two-proportion inference with no
   sequential testing. If you know a better-suited method for eval gating, open an issue with
   the derivation, not a feature request.
3. **A scorer that finds something our four free scorers miss** — with a false-alarm
   measurement on real data, not a synthetic example.

## What we will decline

- New integrations (provider SDKs, dashboards, SaaS connectors) before the statistics are sound.
- Features that make the harness *report* more without making it *know* more.

## Development

```bash
git clone https://github.com/Hemakrishna7406/evalforge && cd evalforge
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q          # must be green before you open a PR
```

No runtime dependencies. That is deliberate: a statistical library you cannot audit is worse
than none, so `stats.py` is dependency-free and every formula is readable in place.

## Pull requests

- One idea per PR.
- If you change a verdict, **add or update the test that pins it**. `tests/test_stats.py` and
  `tests/test_blockers.py` are the contract; changing behaviour without changing them is a red flag.
- If you change a number in the README (MDE, test count), say how you got it.

## Reporting a bug

Include: the command, the dataset shape, and the digest from `runs/*/run.json`. The digest is
how we tell "the code changed" from "the data changed".
