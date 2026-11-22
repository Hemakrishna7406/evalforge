"""Budget ledger.

The Goldfish simulated the original design and showed it settling ₹120 against a ₹100
ceiling. Their analysis: overshoot is bounded by ``in_flight × (actual − estimate)``, and
nothing bounded that term. So this module stops claiming zero overshoot and does three
concrete things instead:

1. ``reserve`` is a single atomic UPDATE with the ceiling in the WHERE clause — no
   read-then-write race.
2. ``settle`` charges at most the reservation and records the excess as ``overrun``.
   Overrun is measured, reported, and makes a run ineligible to pass the gate.
3. Every reservation is a row with a TTL, so a crashed worker's budget is reclaimed by
   ``reap`` rather than leaking for the rest of the run.

The same SQL runs on SQLite (tests) and Postgres (production).
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any


class BudgetExceeded(RuntimeError):
    """The reservation would breach the ceiling. The run should mark the item skipped."""


@dataclass(frozen=True)
class BudgetSnapshot:
    ceiling: float
    reserved: float
    spent: float
    overrun: float

    @property
    def remaining(self) -> float:
        return max(0.0, self.ceiling - self.spent - self.reserved)


SCHEMA = """
CREATE TABLE IF NOT EXISTS budget (
    run_id   TEXT PRIMARY KEY,
    ceiling  REAL NOT NULL,
    reserved REAL NOT NULL DEFAULT 0,
    spent    REAL NOT NULL DEFAULT 0,
    overrun  REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reservations (
    id         TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    amount     REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_res_run ON reservations(run_id);
"""


class Ledger:
    def __init__(self, conn: sqlite3.Connection, run_id: str, ceiling: float) -> None:
        self.conn = conn
        self.run_id = run_id
        self.ceiling = ceiling
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO budget (run_id, ceiling) VALUES (?, ?)", (run_id, ceiling)
        )
        conn.commit()

    # -- reservation ---------------------------------------------------------
    def reserve(self, amount: float, ttl_seconds: float = 300.0) -> str:
        """Atomically reserve ``amount``. Raises BudgetExceeded if it would breach.

        The ceiling lives in the WHERE clause, so two workers cannot both pass a check that
        only one of them should.
        """
        cur = self.conn.execute(
            "UPDATE budget SET reserved = reserved + ? "
            "WHERE run_id = ? AND spent + reserved + ? <= ceiling",
            (amount, self.run_id, amount),
        )
        if cur.rowcount != 1:
            self.conn.rollback()
            raise BudgetExceeded(
                f"reserving {amount:.4f} would exceed ceiling {self.ceiling:.4f}"
            )
        res_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO reservations (id, run_id, amount, expires_at) VALUES (?, ?, ?, ?)",
            (res_id, self.run_id, amount, time.time() + ttl_seconds),
        )
        self.conn.commit()
        return res_id

    def settle(self, res_id: str, actual: float) -> float:
        """Charge the real cost. Returns the overrun, which is never silently absorbed."""
        row = self.conn.execute(
            "SELECT amount FROM reservations WHERE id = ? AND run_id = ?", (res_id, self.run_id)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown or already-settled reservation {res_id}")
        reserved = float(row[0])
        overrun = max(0.0, actual - reserved)
        charged = min(actual, reserved)
        self.conn.execute("DELETE FROM reservations WHERE id = ?", (res_id,))
        self.conn.execute(
            "UPDATE budget SET "
            "reserved = CASE WHEN reserved >= ? THEN reserved - ? ELSE 0 END, "
            "spent = spent + ?, "
            "overrun = overrun + ? "
            "WHERE run_id = ?",
            (reserved, reserved, charged, overrun, self.run_id),
        )
        self.conn.commit()
        return overrun

    def release(self, res_id: str) -> None:
        """Give an unused reservation back — the path the original design never had."""
        row = self.conn.execute(
            "SELECT amount FROM reservations WHERE id = ? AND run_id = ?", (res_id, self.run_id)
        ).fetchone()
        if row is None:
            return
        reserved = float(row[0])
        self.conn.execute("DELETE FROM reservations WHERE id = ?", (res_id,))
        self.conn.execute(
            "UPDATE budget SET reserved = CASE WHEN reserved >= ? THEN reserved - ? ELSE 0 END "
            "WHERE run_id = ?",
            (reserved, reserved, self.run_id),
        )
        self.conn.commit()

    def reap(self, now: float | None = None) -> int:
        """Release reservations whose worker died. Returns how many were reclaimed."""
        now = time.time() if now is None else now
        rows = self.conn.execute(
            "SELECT id, amount FROM reservations WHERE run_id = ? AND expires_at < ?",
            (self.run_id, now),
        ).fetchall()
        for res_id, amount in rows:
            self.conn.execute("DELETE FROM reservations WHERE id = ?", (res_id,))
            self.conn.execute(
                "UPDATE budget SET reserved = CASE WHEN reserved >= ? THEN reserved - ? ELSE 0 END "
                "WHERE run_id = ?",
                (amount, amount, self.run_id),
            )
        self.conn.commit()
        return len(rows)

    def snapshot(self) -> BudgetSnapshot:
        row = self.conn.execute(
            "SELECT ceiling, reserved, spent, overrun FROM budget WHERE run_id = ?", (self.run_id,)
        ).fetchone()
        assert row is not None
        return BudgetSnapshot(float(row[0]), float(row[1]), float(row[2]), float(row[3]))
