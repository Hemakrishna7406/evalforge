"""The statistical claims, tested as claims.

If the README says "this eval can only detect a 12% regression", that number had better be
computable, monotone, and reproducible. These tests are what make the thesis falsifiable.
"""

from __future__ import annotations

import pytest

from evalforge.gate import CategoryRecall, GatePolicy, RunSummary, evaluate, power_report
from evalforge.stats import (
    decide, diff_interval, minimum_detectable_effect, required_sample_size, wilson_interval,
)


def test_wilson_interval_never_collapses_at_the_boundary():
    """The naive interval reports zero uncertainty at p=1.0. That is the bug being fixed."""
    lo, hi = wilson_interval(100, 100)
    assert lo < 1.0 and hi == pytest.approx(1.0)
    assert lo > 0.95


def test_mde_shrinks_with_more_items():
    small = minimum_detectable_effect(0.9, 100)
    large = minimum_detectable_effect(0.9, 10000)
    assert large < small
    # 100× the items buys 10× the precision (MDE scales with 1/sqrt(n))
    assert small / large == pytest.approx(10.0, rel=0.01)


def test_required_sample_size_round_trips_the_mde():
    mde = minimum_detectable_effect(0.9, 500)
    n = required_sample_size(0.9, mde)
    assert n == pytest.approx(500, rel=0.05)


def test_small_sample_cannot_pass_a_regression():
    """The thesis, as an assertion: 50 items cannot rule out a 10-point drop."""
    v = decide(candidate_successes=45, candidate_n=50, baseline_successes=50, baseline_n=50,
               tolerance=0.02)
    assert v.verdict == "inconclusive"
    assert v.mde > 0.02


def test_large_sample_passes_a_real_no_regression():
    v = decide(9000, 10000, 9000, 10000, tolerance=0.02)
    assert v.verdict == "pass"


def test_large_sample_fails_a_real_regression():
    v = decide(7000, 10000, 9000, 10000, tolerance=0.02)
    assert v.verdict == "fail"


def test_diff_interval_is_symmetric_around_the_point_estimate():
    diff, lo, hi = diff_interval(80, 100, 90, 100)
    assert diff == pytest.approx(-0.10)
    assert (hi - diff) == pytest.approx(diff - lo, rel=0.01)


def test_power_report_flags_an_underpowered_run():
    rep = power_report(0.9, n=108)
    assert rep["minimum_detectable_effect"] > 0.05
    assert rep["verdict_is_trustworthy"] is False
    assert "108 items" in rep["advice"]


def test_gate_reports_per_category_intervals():
    cand = RunSummary("c", 1.0, {"number": CategoryRecall(45, 50)})
    base = RunSummary("b", 1.0, {"number": CategoryRecall(50, 50)})
    res = evaluate(cand, base, GatePolicy(tolerance=0.02, min_coverage=0.0))
    assert res.verdict.value == "inconclusive"
    assert "ci" in res.per_category["number"]
    assert res.detectable_effect > 0.02
