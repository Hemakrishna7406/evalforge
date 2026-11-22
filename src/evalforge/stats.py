"""Statistics for evals that don't lie to themselves.

An eval harness that reports "96.1% recall" without an interval is doing numerology. With
108 items the 95% interval on a recall difference is roughly ±9 percentage points, which
means most "improvements" teams celebrate and most "regressions" they block are noise.

Everything here is dependency-free, seeded, and deterministic — a statistical result you
cannot reproduce is not a result.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

# Two-sided 95% and one-sided 80% power, the conventional defaults.
Z_975 = 1.959963984540054
Z_80 = 0.8416212335729143


def wilson_interval(successes: int, n: int, z: float = Z_975) -> tuple[float, float]:
    """Wilson score interval for a proportion.

    Preferred over the normal approximation because recall near 1.0 is exactly where the
    naive interval collapses to a point and reports false certainty.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def two_proportion_se(p1: float, n1: int, p2: float, n2: int) -> float:
    if n1 <= 0 or n2 <= 0:
        return float("inf")
    return math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)


def diff_interval(
    k1: int, n1: int, k2: int, n2: int, z: float = Z_975
) -> tuple[float, float, float]:
    """Interval on (p1 − p2). Returns (diff, lo, hi)."""
    p1 = k1 / n1 if n1 else 0.0
    p2 = k2 / n2 if n2 else 0.0
    diff = p1 - p2
    se = two_proportion_se(p1, n1, p2, n2)
    if not math.isfinite(se):
        return (diff, -1.0, 1.0)
    return (diff, diff - z * se, diff + z * se)


def minimum_detectable_effect(
    p: float, n: int, alpha_z: float = Z_975, power_z: float = Z_80
) -> float:
    """The smallest true difference this sample size could detect.

    Two independent proportions of equal size. This is the number an eval should publish
    *before* anyone reads its verdict.
    """
    if n <= 0:
        return 1.0
    # At p = 1.0 (or 0.0) the binomial variance is zero and the formula collapses to an MDE
    # of 0 — an eval that claims it can detect any difference at all. Clamp to the
    # Agresti-Coull style boundary so a perfect score still admits uncertainty.
    p_adj = min(max(p, 1.0 / (2 * n)), 1.0 - 1.0 / (2 * n))
    return (alpha_z + power_z) * math.sqrt(2 * p_adj * (1 - p_adj) / n)


def required_sample_size(
    p: float, mde: float, alpha_z: float = Z_975, power_z: float = Z_80
) -> int:
    """Items per arm needed to detect ``mde`` at the given error rates."""
    if mde <= 0:
        return 0
    n = ((alpha_z + power_z) ** 2) * 2 * p * (1 - p) / (mde**2)
    return int(math.ceil(n))


def bootstrap_mean_ci(
    values: list[float], n_resamples: int = 2000, seed: int = 0, alpha: float = 0.05
) -> tuple[float, float, float]:
    """Percentile bootstrap for a mean. Deterministic given the seed."""
    if not values:
        return (0.0, 0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(n_resamples):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[int((alpha / 2) * n_resamples)]
    hi = means[min(n_resamples - 1, int((1 - alpha / 2) * n_resamples))]
    return (sum(values) / n, lo, hi)


@dataclass(frozen=True)
class StatisticalVerdict:
    verdict: str            # "pass" | "fail" | "inconclusive"
    reason: str
    diff: float
    ci_low: float
    ci_high: float
    mde: float
    tolerance: float


def decide(
    candidate_successes: int,
    candidate_n: int,
    baseline_successes: int,
    baseline_n: int,
    tolerance: float,
    z: float = Z_975,
) -> StatisticalVerdict:
    """Three-way decision on interval arithmetic instead of a point comparison.

    * FAIL          the interval puts the regression above tolerance — confidently worse
    * PASS          the interval rules out a regression larger than tolerance
    * INCONCLUSIVE  the sample is too small to rule it out either way

    The third outcome is the whole point: a two-state gate forces a lie. It either blocks a
    release over noise or passes a run it could not have measured.
    """
    diff, lo, hi = diff_interval(candidate_successes, candidate_n, baseline_successes, baseline_n, z)
    # Negative diff means the candidate is worse (fewer successes).
    regression_lo = -hi          # best case for the candidate
    regression_hi = -lo          # worst case for the candidate
    p = baseline_successes / baseline_n if baseline_n else 0.5
    mde = minimum_detectable_effect(p, min(candidate_n, baseline_n))

    if regression_lo > tolerance:
        return StatisticalVerdict(
            "fail",
            f"confidently worse: regression interval [{regression_lo:.1%}, "
            f"{regression_hi:.1%}] lies entirely above the {tolerance:.1%} tolerance",
            diff, lo, hi, mde, tolerance,
        )
    if regression_hi <= tolerance:
        return StatisticalVerdict(
            "pass",
            f"regression interval [{regression_lo:.1%}, {regression_hi:.1%}] rules out a "
            f"drop larger than {tolerance:.1%}",
            diff, lo, hi, mde, tolerance,
        )
    return StatisticalVerdict(
        "inconclusive",
        f"underpowered: regression interval [{regression_lo:.1%}, {regression_hi:.1%}] "
        f"straddles the {tolerance:.1%} tolerance. With n={min(candidate_n, baseline_n)} "
        f"this eval can only detect a drop of {mde:.1%} or larger",
        diff, lo, hi, mde, tolerance,
    )
