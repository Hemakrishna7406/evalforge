"""Item queue with leases.

The Goldfish were decisive: ``SELECT … FOR UPDATE SKIP LOCKED`` alone is not a queue. Hold
the transaction across a provider call and you get idle-in-transaction; commit the claim and
a ``kill -9`` strands the row as ``running`` with no owner and no expiry — forever. Their
verdict was that "resumable runs (items are independent rows)" is not resumability, because
claimed-and-abandoned is indistinguishable from claimed-and-in-progress.

So a claim is a **lease**:

* ``claim`` sets ``lease_expires_at`` and increments ``attempts`` in one statement.
* ``heartbeat`` extends the lease while real work is happening.
* ``reap`` returns expired leases to the queue, or dead-letters them past ``max_attempts``.

The transaction never spans item processing.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

PENDING, RUNNING, DONE, DEAD = "pending", "running", "done", "dead_letter"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    run_id           TEXT NOT NULL,
    item_id          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    worker_id        TEXT,
    lease_expires_at REAL,
    result_status    TEXT,
    payload          TEXT,
    PRIMARY KEY (run_id, item_id)
);
CREATE INDEX IF NOT EXISTS ix_items_claim ON items(run_id, status, lease_expires_at);
"""


@dataclass(frozen=True)
class ClaimedItem:
    item_id: str
    attempts: int
    payload: str | None


class ItemQueue:
    def __init__(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        lease_ttl_seconds: int = 120,
        max_attempts: int = 3,
        dialect: str = "sqlite",
    ) -> None:
        self.conn = conn
        self.run_id = run_id
        self.lease_ttl = lease_ttl_seconds
        self.max_attempts = max_attempts
        self.dialect = dialect
        conn.executescript(SCHEMA)
        conn.commit()

    def enqueue(self, item_ids: list[str], payloads: dict[str, str] | None = None) -> None:
        payloads = payloads or {}
        self.conn.executemany(
            "INSERT OR IGNORE INTO items (run_id, item_id, payload) VALUES (?, ?, ?)",
            [(self.run_id, i, payloads.get(i)) for i in item_ids],
        )
        self.conn.commit()

    def claim(self, worker_id: str) -> ClaimedItem | None:
        """Lease one item. The transaction ends here — it does not span the work."""
        now = time.time()
        expires = now + self.lease_ttl
        # Postgres needs SKIP LOCKED so two workers cannot lease the same row; SQLite
        # serialises writers, so the plain subquery is already safe there.
        skip = " FOR UPDATE SKIP LOCKED" if self.dialect == "postgresql" else ""
        select_next = (
            "SELECT item_id FROM items "
            "WHERE run_id = ? AND status = ? AND attempts < ? "
            "ORDER BY item_id LIMIT 1" + skip
        )
        cur = self.conn.execute(
            "UPDATE items SET status = ?, worker_id = ?, lease_expires_at = ?, "
            "attempts = attempts + 1 "
            "WHERE run_id = ? AND item_id = (" + select_next + ")",
            (RUNNING, worker_id, expires, self.run_id, self.run_id, PENDING, self.max_attempts),
        )
        self.conn.commit()
        if cur.rowcount != 1:
            return None
        row = self.conn.execute(
            "SELECT item_id, attempts, payload FROM items WHERE run_id = ? AND worker_id = ? "
            "AND status = ? ORDER BY item_id LIMIT 1",
            (self.run_id, worker_id, RUNNING),
        ).fetchone()
        if row is None:
            return None
        return ClaimedItem(row[0], int(row[1]), row[2])

    def heartbeat(self, item_id: str, worker_id: str) -> bool:
        """Extend the lease. Returns False if we lost the item to the reaper."""
        cur = self.conn.execute(
            "UPDATE items SET lease_expires_at = ? "
            "WHERE run_id = ? AND item_id = ? AND worker_id = ? AND status = ?",
            (time.time() + self.lease_ttl, self.run_id, item_id, worker_id, RUNNING),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def finish(self, item_id: str, result_status: str) -> None:
        self.conn.execute(
            "UPDATE items SET status = ?, result_status = ?, worker_id = NULL, "
            "lease_expires_at = NULL WHERE run_id = ? AND item_id = ?",
            (DONE, result_status, self.run_id, item_id),
        )
        self.conn.commit()

    def reap(self, now: float | None = None) -> tuple[int, int]:
        """Return expired leases to the pool, or dead-letter them.

        Returns ``(requeued, dead_lettered)``.
        """
        now = time.time() if now is None else now
        rows = self.conn.execute(
            "SELECT item_id, attempts FROM items "
            "WHERE run_id = ? AND status = ? AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at < ?",
            (self.run_id, RUNNING, now),
        ).fetchall()
        requeued = dead = 0
        for item_id, attempts in rows:
            if int(attempts) >= self.max_attempts:
                self.conn.execute(
                    "UPDATE items SET status = ?, result_status = ?, worker_id = NULL, "
                    "lease_expires_at = NULL WHERE run_id = ? AND item_id = ?",
                    (DEAD, "dead_letter", self.run_id, item_id),
                )
                dead += 1
            else:
                self.conn.execute(
                    "UPDATE items SET status = ?, worker_id = NULL, lease_expires_at = NULL "
                    "WHERE run_id = ? AND item_id = ?",
                    (PENDING, self.run_id, item_id),
                )
                requeued += 1
        self.conn.commit()
        return requeued, dead

    def stats(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM items WHERE run_id = ? GROUP BY status", (self.run_id,)
        ).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}
