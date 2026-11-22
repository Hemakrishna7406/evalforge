"""The gate: the product.

Two design generations live here, and the difference between them is the point of this
repository.

**v1** compared point estimates against absolute tolerances. A Goldfish reviewer killed it:
recall is a ratio, so a run that skipped 40% of items measured a different population and
could pass a real regression.

**v2** decides on **confidence intervals**. It answers a question v1 could not: *could this
run have detected the thing it claims not to have found?* A run too small to detect a
regression returns ``INCONCLUSIVE`` — not ``PASS``. That distinction is the difference
between an eval harness and a random number generator with a CI badge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .stats import StatisticalVerdict, decide, minimum_detectable_effect


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


EXIT_CODES = {Verdict.PASS: 0, Verdict.FAIL: 1, Verdict.INCONCLUSIVE: 2}


@dataclass(frozen=True)
class CategoryRecall:
    """Successes and denominator, not just a percentage.

    Keeping the raw counts is what makes the interval possible. A percentage throws away the
    information the gate needs.
    """

    successes: int
    n: int

    @property
    def rate(self) -> float:
        return self.successes / self.n if self.n else 0.0


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    coverage: float
    recall: dict[str, CategoryRecall]
    false_alarm_rate: float = 0.0
    overrun_inr: float = 0.0


@dataclass
class GateResult:
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)
    incomparable: list[str] = field(default_factory=list)
    per_category: dict[str, dict] = field(default_factory=dict)
    detectable_effect: float = 0.0

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.verdict]

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "exit_code": self.exit_code,
            "reasons": self.reasons,
            "incomparable_categories": self.incomparable,
            "detectable_effect": round(self.detectable_effect, 4),
            "per_category": self.per_category,
        }


@dataclass(frozen=True)
class GatePolicy:
    #: A regression smaller than this is not worth blocking a release over.
    tolerance: float = 0.02
    min_coverage: float = 0.98
    coverage_tolerance: float = 0.01


def evaluate(
    candidate: RunSummary, baseline: RunSummary, policy: GatePolicy | None = None
) -> GateResult:
    policy = policy or GatePolicy()
    blockers: list[str] = []

    # --- preconditions: is this comparison meaningful at all? ----------------
    if candidate.overrun_inr > 0:
        blockers.append(
            f"candidate overran its budget by ₹{candidate.overrun_inr:.2f}; spend was unbounded"
        )
    if candidate.coverage < policy.min_coverage:
        blockers.append(
            f"coverage {candidate.coverage:.1%} is below the required {policy.min_coverage:.0%}"
        )
    if abs(candidate.coverage - baseline.coverage) > policy.coverage_tolerance:
        blockers.append(
            f"coverage differs from baseline ({candidate.coverage:.1%} vs "
            f"{baseline.coverage:.1%}); populations are not comparable"
        )
    if blockers:
        return GateResult(Verdict.INCONCLUSIVE, blockers)

    # --- per-category interval comparison -----------------------------------
    incomparable: list[str] = []
    failures: list[str] = []
    inconclusive: list[str] = []
    per_category: dict[str, dict] = {}
    worst_mde = 0.0

    for cat, base in sorted(baseline.recall.items()):
        cand = candidate.recall.get(cat)
        if cand is None:
            incomparable.append(cat)
            continue

        verdict: StatisticalVerdict = decide(
            cand.successes, cand.n, base.successes, base.n, policy.tolerance
        )
        worst_mde = max(worst_mde, verdict.mde)
        per_category[cat] = {
            "candidate": round(cand.rate, 4),
            "baseline": round(base.rate, 4),
            "diff": round(verdict.diff, 4),
            "ci": [round(verdict.ci_low, 4), round(verdict.ci_high, 4)],
            "mde": round(verdict.mde, 4),
            "verdict": verdict.verdict,
            "reason": verdict.reason,
        }
        if verdict.verdict == "fail":
            failures.append(f"{cat}: {verdict.reason}")
        elif verdict.verdict == "inconclusive":
            inconclusive.append(f"{cat}: {verdict.reason}")

    if failures:
        return GateResult(Verdict.FAIL, failures, incomparable, per_category, worst_mde)
    if inconclusive:
        return GateResult(Verdict.INCONCLUSIVE, inconclusive, incomparable, per_category, worst_mde)
    return GateResult(Verdict.PASS, [], incomparable, per_category, worst_mde)


def power_report(
    baseline_rate: float, n: int, tolerances: tuple[float, ...] = (0.01, 0.02, 0.05)
) -> dict:
    """What can this eval actually see? Answer this *before* reading any verdict."""
    mde = minimum_detectable_effect(baseline_rate, n)
    return {
        "n": n,
        "baseline_rate": baseline_rate,
        "minimum_detectable_effect": round(mde, 4),
        "can_detect": {f"{t:.0%}": mde <= t for t in tolerances},
        "verdict_is_trustworthy": mde <= min(tolerances),
        "advice": (
            f"with {n} items this eval can only detect a regression of {mde:.1%} or larger"
            if mde > min(tolerances)
            else f"{n} items is enough to detect a {mde:.1%} regression"
        ),
    }
