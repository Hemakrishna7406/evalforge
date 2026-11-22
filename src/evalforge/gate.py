"""The gate: the product.

The Goldfish found the original gate unsound in a way that matters more than any other
finding — recall is a ratio, and a run that skipped 40% of its items computes recall over a
different population than its baseline. It could **pass a real regression**.

So the gate has three outcomes and a coverage precondition:

    PASS          exit 0   quality did not regress, on comparable populations
    FAIL          exit 1   a measured regression
    INCONCLUSIVE  exit 2   populations not comparable, or budget overrun — never PASS

INCONCLUSIVE exists because a two-state gate forces a lie: either you block a release for
running out of money, or you pass a run you couldn't measure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


EXIT_CODES = {Verdict.PASS: 0, Verdict.FAIL: 1, Verdict.INCONCLUSIVE: 2}


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    coverage: float
    recall_by_category: dict[str, float]
    false_alarm_rate: float
    overrun_inr: float = 0.0


@dataclass
class GateResult:
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)
    incomparable: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.verdict]

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "exit_code": self.exit_code,
            "reasons": self.reasons,
            "incomparable_categories": self.incomparable,
        }


@dataclass(frozen=True)
class GatePolicy:
    recall_drop_tolerance: float = 0.02
    false_alarm_rise_tolerance: float = 0.01
    min_coverage: float = 0.98
    coverage_tolerance: float = 0.01


def evaluate(
    candidate: RunSummary, baseline: RunSummary, policy: GatePolicy | None = None
) -> GateResult:
    policy = policy or GatePolicy()
    reasons: list[str] = []

    # --- precondition: are these two runs even comparable? -------------------
    if candidate.overrun_inr > 0:
        reasons.append(
            f"candidate overran its budget by ₹{candidate.overrun_inr:.2f}; spend was not bounded"
        )
    if candidate.coverage < policy.min_coverage:
        reasons.append(
            f"candidate coverage {candidate.coverage:.1%} is below the required "
            f"{policy.min_coverage:.0%} — the measurement is incomplete"
        )
    if abs(candidate.coverage - baseline.coverage) > policy.coverage_tolerance:
        reasons.append(
            f"coverage differs from baseline ({candidate.coverage:.1%} vs "
            f"{baseline.coverage:.1%}); populations are not comparable"
        )
    if reasons:
        return GateResult(Verdict.INCONCLUSIVE, reasons)

    # --- comparison ---------------------------------------------------------
    incomparable: list[str] = []
    regressions: list[str] = []

    for cat, base_recall in sorted(baseline.recall_by_category.items()):
        if cat not in candidate.recall_by_category:
            incomparable.append(cat)
            continue
        drop = base_recall - candidate.recall_by_category[cat]
        if drop > policy.recall_drop_tolerance:
            regressions.append(
                f"{cat}: recall {base_recall:.1%} → "
                f"{candidate.recall_by_category[cat]:.1%} (drop {drop:.1%})"
            )

    fa_rise = candidate.false_alarm_rate - baseline.false_alarm_rate
    if fa_rise > policy.false_alarm_rise_tolerance:
        regressions.append(
            f"false alarms {baseline.false_alarm_rate:.1%} → "
            f"{candidate.false_alarm_rate:.1%} (rise {fa_rise:.1%})"
        )

    if regressions:
        return GateResult(Verdict.FAIL, regressions, incomparable)
    return GateResult(Verdict.PASS, [], incomparable)
