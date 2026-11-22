"""Tests for the five Goldfish blockers.

Each test corresponds to a defect a stateless reviewer found in design v1. If a fix is
removed, the matching test fails. That is the whole point of this file.
"""

from __future__ import annotations

import sqlite3

import pytest

from evalforge import egress, scorers
from evalforge.digest import ItemResult, compute_digest, coverage
from evalforge.gate import GatePolicy, RunSummary, Verdict, evaluate
from evalforge.ledger import BudgetExceeded, Ledger
from evalforge.queue import DEAD, PENDING, ItemQueue


# ---------------------------------------------------------------- BLOCKER 1: digest
def test_truncation_changes_the_digest():
    """A budget-truncated run must not share a digest with a complete one.

    Design v1 had `score() -> []` meaning both "clean" and "never looked at", so a 70%
    coverage run could hash identically to a 100% one.
    """
    complete = [ItemResult(f"i{n}", "scored", ()) for n in range(10)]
    truncated = [ItemResult(f"i{n}", "scored" if n < 7 else "skipped_budget", ()) for n in range(10)]

    d_complete = compute_digest(complete, {"recall": 1.0})
    d_truncated = compute_digest(truncated, {"recall": 1.0})
    assert d_complete != d_truncated
    assert coverage(complete)[2] == 1.0
    assert coverage(truncated)[2] == pytest.approx(0.7)


def test_digest_is_order_independent():
    a = [ItemResult("x", "scored", ()), ItemResult("y", "scored", ())]
    b = list(reversed(a))
    assert compute_digest(a, {}) == compute_digest(b, {})


# ------------------------------------------------------------ BLOCKER 2: tier-0 egress
def test_tier0_scorer_cannot_open_a_socket():
    class Snooper:
        name, version, tier = "snooper", "1.0.0", 0

        def score(self, item, config):
            import socket
            socket.create_connection(("example.com", 80), timeout=1)
            return []

    with pytest.raises(egress.EgressViolation):
        with egress.blocked():
            Snooper().score(None, {})


def test_egress_guard_restores_the_socket():
    import socket
    real = socket.create_connection
    with egress.blocked():
        pass
    assert socket.create_connection is real


# ---------------------------------------------------------------- BLOCKER 3: ledger
def test_reservations_cannot_exceed_the_ceiling():
    conn = sqlite3.connect(":memory:")
    led = Ledger(conn, "run1", ceiling=100.0)
    ids = []
    for _ in range(10):
        try:
            ids.append(led.reserve(15.0))
        except BudgetExceeded:
            break
    snap = led.snapshot()
    assert snap.reserved <= 100.0
    assert len(ids) == 6            # 6 × 15 = 90; the 7th would breach
    with pytest.raises(BudgetExceeded):
        led.reserve(15.0)


def test_settle_records_overrun_instead_of_hiding_it():
    conn = sqlite3.connect(":memory:")
    led = Ledger(conn, "run1", ceiling=100.0)
    res = led.reserve(10.0)
    overrun = led.settle(res, 30.0)
    snap = led.snapshot()
    assert overrun == 20.0
    assert snap.spent == 10.0        # charged at most the reservation
    assert snap.overrun == 20.0      # the excess is visible, not absorbed


def test_release_and_reap_return_budget():
    conn = sqlite3.connect(":memory:")
    led = Ledger(conn, "run1", ceiling=100.0)
    r1 = led.reserve(40.0, ttl_seconds=0.01)
    r2 = led.reserve(40.0)
    led.release(r1)
    assert led.snapshot().reserved == 40.0
    led.reap(now=10**12)             # everything expired
    assert led.snapshot().reserved == 0.0
    led.release(r2)


# ------------------------------------------------------------------ BLOCKER 4: gate
from evalforge.gate import CategoryRecall  # noqa: E402


def _run(coverage_: float, recall: dict[str, tuple[int, int]], overrun: float = 0.0):
    return RunSummary(
        "r", coverage_, {k: CategoryRecall(v[0], v[1]) for k, v in recall.items()},
        overrun_inr=overrun,
    )


def test_truncated_run_is_inconclusive_never_pass():
    """The single most important behaviour in the product."""
    result = evaluate(_run(0.70, {"number": (99, 100)}), _run(0.99, {"number": (80, 100)}))
    assert result.verdict is Verdict.INCONCLUSIVE
    assert result.exit_code == 2


def test_budget_overrun_blocks_a_pass():
    result = evaluate(_run(1.0, {"number": (95, 100)}, overrun=12.0), _run(1.0, {"number": (90, 100)}))
    assert result.verdict is Verdict.INCONCLUSIVE


def test_category_missing_from_candidate_is_incomparable():
    result = evaluate(_run(1.0, {}), _run(1.0, {"number": (90, 100)}))
    assert result.verdict is Verdict.PASS
    assert "number" in result.incomparable


# ----------------------------------------------------------------- BLOCKER 5: queue
def test_expired_lease_is_reclaimed():
    conn = sqlite3.connect(":memory:")
    q = ItemQueue(conn, "run1", lease_ttl_seconds=0, max_attempts=3)
    q.enqueue(["a"])
    claimed = q.claim("w1")
    assert claimed is not None
    assert q.claim("w2") is None            # nothing else pending
    requeued, dead = q.reap(now=10**12)
    assert (requeued, dead) == (1, 0)
    assert q.stats().get(PENDING) == 1


def test_max_attempts_dead_letters():
    conn = sqlite3.connect(":memory:")
    q = ItemQueue(conn, "run1", lease_ttl_seconds=0, max_attempts=2)
    q.enqueue(["a"])
    for _ in range(2):
        assert q.claim("w") is not None
        q.reap(now=10**12)
    assert q.stats().get(DEAD) == 1


# ------------------------------------------------------- scorer sanity (no regressions)
def test_empty_output_is_critical():
    item = scorers.Item("i", "some source text here", "   ")
    findings = scorers.build("empty_output").score(item, {})
    assert findings and findings[0].severity == "critical"


def test_numeric_preservation_catches_a_dropped_number():
    item = scorers.Item("i", "Water boils at 100 C", "पानी उबलता है")
    findings = scorers.build("numeric_preservation").score(item, {})
    assert any("100" in f.evidence for f in findings)
