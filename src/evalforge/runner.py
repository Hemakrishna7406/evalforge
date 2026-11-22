"""The runner: claim, score, settle, finish.

Deliberately small. Everything interesting lives in the modules it composes, and each of the
five Goldfish blockers is enforced here at exactly one point:

* tier-0 isolation → ``egress.blocked()`` around the free scorers
* budget          → ``Ledger.reserve/settle/release`` with a TTL
* queue liveness  → ``ItemQueue.claim/heartbeat/reap``
* coverage        → every item ends with a status, including ``skipped_budget``
* digest          → ``compute_digest`` over outcomes *and* coverage
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import egress, scorers
from .digest import ItemResult, compute_digest, coverage
from .ledger import BudgetExceeded, Ledger
from .queue import ItemQueue
from .spec import ExecutionPolicy, RunSpec, canonical_json, sha256_of


@dataclass
class RunOutcome:
    run_id: str
    spec_hash: str
    digest: str
    results: list[ItemResult]
    metrics: dict
    budget: dict
    queue: dict
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "spec_hash": self.spec_hash,
            "digest": self.digest,
            "metrics": self.metrics,
            "budget": self.budget,
            "queue": self.queue,
            "errors": self.errors,
            "items": [
                {"id": r.item_id, "status": r.status, "findings": list(r.findings)}
                for r in self.results
            ],
        }


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSON — {exc.msg}") from exc
        for key in ("id", "source", "output"):
            if key not in row:
                raise ValueError(f"{path}:{lineno}: missing required field {key!r}")
        rows.append(row)
    ids = [r["id"] for r in rows]
    if len(ids) != len(set(ids)):
        dupes = {i for i in ids if ids.count(i) > 1}
        raise ValueError(f"{path}: duplicate item ids: {sorted(dupes)[:5]}")
    return rows


def dataset_hash(rows: list[dict]) -> str:
    return sha256_of(sorted(rows, key=lambda r: r["id"]))


def run(
    rows: list[dict],
    spec: RunSpec,
    policy: ExecutionPolicy,
    *,
    conn: sqlite3.Connection | None = None,
    worker_id: str | None = None,
    now: float | None = None,
) -> RunOutcome:
    conn = conn or sqlite3.connect(":memory:")
    run_id = uuid.uuid4().hex[:12]
    worker_id = worker_id or f"w-{uuid.uuid4().hex[:6]}"

    queue = ItemQueue(
        conn, run_id,
        lease_ttl_seconds=policy.lease_ttl_seconds,
        max_attempts=policy.max_attempts,
    )
    queue.enqueue([r["id"] for r in rows])
    by_id = {r["id"]: r for r in rows}

    ledger = Ledger(conn, run_id, policy.max_cost_inr)
    results: list[ItemResult] = []
    errors: list[str] = []

    free = [scorers.build(s.name) for s in spec.scorers if scorers.build(s.name).tier == 0]

    while True:
        claimed = queue.claim(worker_id)
        if claimed is None:
            break
        row = by_id[claimed.item_id]
        item = scorers.Item(
            item_id=row["id"], source=row["source"], output=row["output"],
            lang=row.get("lang", "en"),
        )

        # --- tier 0, with the network denied ------------------------------
        findings: list[dict] = []
        status = "scored"
        try:
            with egress.blocked():
                for scorer in free:
                    for f in scorer.score(item, {}):
                        findings.append(f.to_dict())
        except egress.EgressViolation as exc:
            # A tier-0 scorer tried to reach the network. It did not get to pretend it
            # scored the item.
            status = "failed"
            errors.append(f"{item.item_id}: egress violation — {exc}")

        # --- tier 1, budgeted ---------------------------------------------
        if status == "scored" and spec.judge_sample_rate > 0 and policy.max_cost_inr > 0:
            estimate = 0.10
            try:
                res = ledger.reserve(estimate, ttl_seconds=policy.lease_ttl_seconds)
            except BudgetExceeded:
                status = "skipped_budget"
            else:
                try:
                    # A real judge call would go here; v0.1 keeps the accounting honest
                    # and the call pluggable.
                    ledger.settle(res, estimate)
                except Exception as exc:  # noqa: BLE001 - any failure must release budget
                    ledger.release(res)
                    status = "failed"
                    errors.append(f"{item.item_id}: judge failed — {exc}")

        queue.finish(claimed.item_id, status)
        results.append(ItemResult(claimed.item_id, status, tuple(findings)))

    # A single-worker run can still leave a lease behind if it died; reap for hygiene.
    requeued, dead = queue.reap(now)

    scored, total, frac = coverage(results)
    snap = ledger.snapshot()
    flagged = [r for r in results if r.status == "scored" and r.findings]
    metrics = {
        "items_total": total,
        "items_scored": scored,
        "coverage": round(frac, 6),
        "items_flagged": len(flagged),
        "false_alarm_rate": 0.0,
        "recall_by_category": {},
    }
    digest = compute_digest(results, metrics)

    return RunOutcome(
        run_id=run_id,
        spec_hash=spec.spec_hash,
        digest=digest,
        results=results,
        metrics=metrics,
        budget={
            "ceiling": snap.ceiling, "spent": snap.spent,
            "reserved": snap.reserved, "overrun": snap.overrun,
        },
        queue={"requeued": requeued, "dead_lettered": dead, **queue.stats()},
        errors=errors,
    )


def write_report(outcome: RunOutcome, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run.json").write_text(
        json.dumps(outcome.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    lines = [
        f"# EvalForge run {outcome.run_id}",
        "",
        f"- **digest**: `{outcome.digest}`",
        f"- **spec**: `{outcome.spec_hash}`",
        f"- **coverage**: {outcome.metrics['coverage']:.1%} "
        f"({outcome.metrics['items_scored']}/{outcome.metrics['items_total']})",
        f"- **budget**: spent ₹{outcome.budget['spent']:.2f} "
        f"of ₹{outcome.budget['ceiling']:.2f}, overrun ₹{outcome.budget['overrun']:.2f}",
        "",
    ]
    if outcome.errors:
        lines.append("## Errors\n")
        lines += [f"- {e}" for e in outcome.errors]
        lines.append("")
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    return out_dir
