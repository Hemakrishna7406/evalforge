"""Run identity: what makes two runs the same run.

The Goldfish found the original design's central contradiction — budget lived inside the
spec hash while the ledger decided which items got scored, so "same spec ⇒ same digest" was
false. The fix is a separation of concerns:

* ``RunSpec`` is *identity*: dataset, scorers, model, sampling policy. Immutable, hashed.
* ``ExecutionPolicy`` is *budget and tolerance*: what we were willing to spend. Recorded on
  the run row, deliberately **outside** the hash, because two runs with different budgets
  are legitimately different executions of the same measurement.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, fixed float repr.

    ``sort_keys`` alone is not enough — ``repr`` of floats varies by platform in edge cases,
    so floats are formatted explicitly.
    """
    return json.dumps(_normalise(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _normalise(obj: Any) -> Any:
    if isinstance(obj, float):
        # Fixed precision, and -0.0 collapses to 0.0 so a sign bit cannot fork the hash.
        if obj == 0.0:
            return 0.0
        return float(f"{obj:.10g}")
    if isinstance(obj, dict):
        return {str(k): _normalise(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalise(v) for v in obj]
    return obj


def sha256_of(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ScorerRef:
    """A scorer is identified by name *and* version *and* config.

    The Goldfish caught that a bare name lets you silently reuse a cached finding produced
    by a different scorer implementation. Version and config are part of the cache key.
    """

    name: str
    version: str
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def config_hash(self) -> str:
        return sha256_of(self.config)[:16]

    def cache_key(self, item_hash: str) -> str:
        return f"{self.name}:{self.version}:{self.config_hash}:{item_hash}"


@dataclass(frozen=True)
class RunSpec:
    """Identity of a measurement. No budget, no tolerances — those are execution policy."""

    dataset_hash: str
    scorers: tuple[ScorerRef, ...]
    model_id: str
    model_params_hash: str
    judge_sample_rate: float = 0.0

    @property
    def spec_hash(self) -> str:
        return sha256_of(
            {
                "dataset": self.dataset_hash,
                "scorers": [
                    {"n": s.name, "v": s.version, "c": s.config_hash} for s in self.scorers
                ],
                "model": self.model_id,
                "model_params": self.model_params_hash,
                "judge_sample_rate": self.judge_sample_rate,
            }
        )


@dataclass(frozen=True)
class ExecutionPolicy:
    """How much we were willing to spend, and how strictly we compare. Not identity."""

    max_cost_inr: float = 0.0
    min_coverage: float = 0.98
    coverage_tolerance: float = 0.01
    max_attempts: int = 3
    lease_ttl_seconds: int = 120

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_cost_inr": self.max_cost_inr,
            "min_coverage": self.min_coverage,
            "coverage_tolerance": self.coverage_tolerance,
            "max_attempts": self.max_attempts,
            "lease_ttl_seconds": self.lease_ttl_seconds,
        }
