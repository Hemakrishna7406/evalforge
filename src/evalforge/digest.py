"""The digest: proof that two runs measured the same thing.

The Goldfish's sharpest finding was that a budget-truncated run and a complete run could
share a digest, because ``score() -> []`` meant both "clean" and "never looked at". The fix
is that **coverage is digested**. A truncated run has a different digest by construction, so
it cannot masquerade as a complete one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .spec import sha256_of

# Statuses that mean "this item contributed to the measurement".
SCORED = "scored"
# Statuses that mean "this item did not".
UNSCORED = frozenset({"skipped_budget", "skipped_policy", "failed", "dead_letter", "pending"})


@dataclass(frozen=True)
class ItemResult:
    item_id: str
    status: str
    findings: tuple[dict, ...] = ()

    def to_digest_payload(self) -> dict:
        # Only the digested fields. Timing, cost and worker identity are excluded on purpose:
        # they differ between two correct runs of the same measurement.
        return {
            "id": self.item_id,
            "status": self.status,
            "findings": sorted(
                ({"c": f.get("category"), "s": f.get("severity"), "sp": f.get("span")}
                 for f in self.findings),
                key=lambda d: (d["c"] or "", d["s"] or "", str(d["sp"])),
            ),
        }


def coverage(results: Iterable[ItemResult]) -> tuple[int, int, float]:
    """(scored, total, fraction). The denominator the gate needs and the design forgot."""
    total = 0
    scored = 0
    for r in results:
        total += 1
        if r.status == SCORED:
            scored += 1
    return scored, total, (scored / total if total else 0.0)


def compute_digest(results: Iterable[ItemResult], metrics: Mapping[str, object]) -> str:
    """Digest over item outcomes, aggregate metrics, and coverage.

    Sorted by item id so ordering of execution cannot fork the hash.
    """
    rows = sorted((r.to_digest_payload() for r in results), key=lambda d: d["id"])
    scored, total, frac = coverage(
        ItemResult(r["id"], r["status"], tuple(r["findings"])) for r in rows
    )
    return sha256_of(
        {
            "items": rows,
            "metrics": dict(metrics),
            # Rounded so float formatting noise cannot fork the digest; the coverage guard in
            # the gate is what actually enforces comparability.
            "coverage": round(frac, 6),
            "scored": scored,
            "total": total,
        }
    )
