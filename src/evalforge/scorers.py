"""Scorers.

A scorer is a pure function of (item, config) plus, for tier 1, a provider client. The tier
is declared, and the runner enforces it: tier 0 executes inside ``egress.blocked()`` so a
network call raises instead of silently costing money.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

TIER_FREE = 0
TIER_PAID = 1


@dataclass(frozen=True)
class Finding:
    category: str
    severity: str
    evidence: str
    span: tuple[int, int] | None = None
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "severity": self.severity,
            "evidence": self.evidence,
            "span": list(self.span) if self.span else None,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class Item:
    item_id: str
    source: str
    output: str
    lang: str = "en"


class Scorer(Protocol):
    name: str
    version: str
    tier: int

    def score(self, item: Item, config: dict) -> list[Finding]: ...


# --------------------------------------------------------------------------- tier 0

NUM = re.compile(r"\d+(?:[.,]\d+)*")


def _numbers(text: str) -> Counter:
    return Counter(m.group(0).replace(",", "") for m in NUM.finditer(text))


class NumericPreservation:
    """A number in the source that is absent from the output."""

    name, version, tier = "numeric_preservation", "1.0.0", TIER_FREE

    def score(self, item: Item, config: dict) -> list[Finding]:
        src, out = _numbers(item.source), _numbers(item.output)
        missing = src - out
        return [
            Finding("number", "major", f"source number {n!r} missing from output (×{c})")
            for n, c in missing.items()
        ]


class Repetition:
    """An adjacent verbatim repeat — the classic looping failure."""

    name, version, tier = "repetition", "1.0.0", TIER_FREE
    SHINGLE = 3

    def score(self, item: Item, config: dict) -> list[Finding]:
        words = item.output.split()
        n = int(config.get("shingle", self.SHINGLE))
        findings: list[Finding] = []
        seen: dict[tuple, int] = {}
        for i in range(len(words) - n + 1):
            key = tuple(w.strip(".,!?।") for w in words[i : i + n])
            if key in seen and all(key):
                findings.append(
                    Finding("addition", "major", f"repeated {n}-word sequence {key!r}")
                )
                break
            seen[key] = i
        return findings


class LengthRatio:
    """Output wildly shorter or longer than the source."""

    name, version, tier = "length_ratio", "1.0.0", TIER_FREE

    def score(self, item: Item, config: dict) -> list[Finding]:
        lo = float(config.get("lo", 0.4))
        hi = float(config.get("hi", 3.0))
        sw, ow = len(item.source.split()), len(item.output.split())
        if sw < 6 or ow == 0:
            return []
        ratio = ow / sw
        if ratio < lo:
            return [Finding("omission", "major", f"output is {ratio:.2f}× the source length")]
        if ratio > hi:
            return [Finding("addition", "minor", f"output is {ratio:.2f}× the source length")]
        return []


class EmptyOutput:
    """Nothing came back. Cheap, and it catches the most damaging failure of all."""

    name, version, tier = "empty_output", "1.0.0", TIER_FREE

    def score(self, item: Item, config: dict) -> list[Finding]:
        if item.output.strip():
            return []
        return [Finding("omission", "critical", "output is empty", span=(0, 0))]


TIER0: dict[str, type] = {
    s.name: s for s in (NumericPreservation, Repetition, LengthRatio, EmptyOutput)
}


def build(name: str) -> Scorer:
    if name not in TIER0:
        raise KeyError(f"unknown scorer {name!r}; available: {sorted(TIER0)}")
    return TIER0[name]()
