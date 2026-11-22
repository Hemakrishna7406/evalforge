# Changelog

## v0.2.0 — statistical honesty

- **Breaking:** the gate decides on confidence intervals, not point thresholds.
  `PASS` now means "the interval rules out a regression beyond tolerance"; a new
  `INCONCLUSIVE` verdict (exit code 2) covers samples too small to tell.
- `evalforge power --n N --baseline P` reports the minimum detectable effect before you run.
- `stats.py`: Wilson intervals, difference intervals, MDE, required-n, seeded bootstrap.
  Dependency-free and deterministic.
- Fixed: at p = 1.0 the MDE collapsed to zero, letting a perfect-scoring eval claim
  unlimited sensitivity. Boundary now clamped.
- `RunSummary` carries raw counts per category, not percentages — a percentage discards
  the information the interval needs.

## v0.1.0 — first build

- Design reviewed by three stateless adversarial reviewers (Elephant-Goldfish Model);
  five blockers found and answered in `DESIGN_v2_RESOLUTIONS.md`.
- Coverage-aware digest, atomic budget ledger with overrun accounting, lease-based queue,
  tier-0 egress guard, three-outcome gate.
