"""
storage/decision_store.py — Persist DecisionRecord to SQLite.

────────────────────────────────────────────────────────────────────────
Role (architecture §4.2, §5.5):
  Persist the final decisions produced by decision policy + state manager.
  Main purposes:
    - post-alert retrospection "why did we send this alert?"
      (track state_change, recommended_action)
    - audit reasons for skipped alerts (delivery_tier=none, throttle applied, etc.)
    - verify that the same inputs produce the same decisions during P10
      historical replay

  Same SQLite + WAL + Lock pattern as signal_store.py.
  Only deals with a single DecisionRecord model → simpler.

────────────────────────────────────────────────────────────────────────
Table layout:

  decisions
    id                  TEXT PRIMARY KEY
    ts                  TEXT NOT NULL    (ISO 8601 UTC)
    fused_event_ref     TEXT NOT NULL    (FusedAnomalyEvent.id, FK not enforced)
    recommended_action  TEXT NOT NULL    ("no_action"|"monitor"|"reduce_risk"|"exit_or_hedge")
    policy_version      TEXT NOT NULL    (e.g. "v0.1-baseline")
    notes               TEXT NOT NULL
    state_change_from   TEXT             (Tier, nullable — NULL when no change)
    state_change_to     TEXT             (Tier, nullable)
    delivery_tier       TEXT NOT NULL    ("none"|"digest"|"realtime"|"urgent")
    delivery_channels   TEXT NOT NULL    (JSON array, e.g. ["email", "telegram"])
    cooldown_until      TEXT             (ISO 8601, nullable)
    external_links      TEXT NOT NULL    (JSON dict)

    INDEX  ix_dec_ts             (ts)
    INDEX  ix_dec_delivery_tier  (delivery_tier)
    INDEX  ix_dec_fused_ref      (fused_event_ref)

  Why state_change is split into from/to columns:
    Audit queries like "all NORMAL→EMERGENCY transitions" are common.
    Storing tuples as JSON makes SQL filtering awkward.

Architecture: §4.2 Storage Layout, §5.5 State-specific recommended action
Plan: §11 D3 (decision_store=SQLite)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from ..core.schemas import DecisionRecord, DeliveryTier, RecommendedAction, Tier


# =====================================================================
# DDL
# =====================================================================
_DDL_DECISIONS = """
CREATE TABLE IF NOT EXISTS decisions (
    id                  TEXT PRIMARY KEY,
    ts                  TEXT NOT NULL,
    fused_event_ref     TEXT NOT NULL,
    recommended_action  TEXT NOT NULL,
    policy_version      TEXT NOT NULL,
    notes               TEXT NOT NULL,
    state_change_from   TEXT,
    state_change_to     TEXT,
    delivery_tier       TEXT NOT NULL,
    delivery_channels   TEXT NOT NULL,
    cooldown_until      TEXT,
    external_links      TEXT NOT NULL
);
"""

_DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_dec_ts            ON decisions(ts);",
    "CREATE INDEX IF NOT EXISTS ix_dec_delivery_tier ON decisions(delivery_tier);",
    "CREATE INDEX IF NOT EXISTS ix_dec_fused_ref     ON decisions(fused_event_ref);",
]


# =====================================================================
# DecisionStore
# =====================================================================
class DecisionStore:
    """SQLite-backed store for DecisionRecord."""

    def __init__(self, db_path: Path) -> None:
        """
        Args:
            db_path: path like data/anomaly/decisions/decisions.db.
        """
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.row_factory = sqlite3.Row

        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA foreign_keys=OFF;")
        self._conn.execute("PRAGMA busy_timeout=5000;")

        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(_DDL_DECISIONS)
            for ddl in _DDL_INDEXES:
                self._conn.execute(ddl)

    # ─────────────────────────────────────────────────────────────────
    # Public API — write
    # ─────────────────────────────────────────────────────────────────
    def insert(self, record: DecisionRecord) -> None:
        """Insert one DecisionRecord. Same id → IntegrityError."""
        # state_change: tuple[Tier, Tier] | None  →  (str|None, str|None)
        if record.state_change is not None:
            from_tier: str | None = record.state_change[0].value
            to_tier: str | None = record.state_change[1].value
        else:
            from_tier = None
            to_tier = None

        sql = """
            INSERT INTO decisions (
                id, ts, fused_event_ref, recommended_action, policy_version, notes,
                state_change_from, state_change_to,
                delivery_tier, delivery_channels, cooldown_until, external_links
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            record.id,
            record.ts.isoformat(),
            record.fused_event_ref,
            record.recommended_action.value,
            record.policy_version,
            record.notes,
            from_tier,
            to_tier,
            record.delivery_tier.value,
            json.dumps(record.delivery_channels, ensure_ascii=False),
            record.cooldown_until.isoformat() if record.cooldown_until else None,
            json.dumps(record.external_links, ensure_ascii=False),
        )
        with self._lock:
            self._conn.execute(sql, params)

    # ─────────────────────────────────────────────────────────────────
    # Public API — read
    # ─────────────────────────────────────────────────────────────────
    def get(self, decision_id: str) -> DecisionRecord | None:
        """Lookup one by id. None if absent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def read_range(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        delivery_tier: DeliveryTier | None = None,
        state_change_to: Tier | None = None,
        fused_event_ref: str | None = None,
        limit: int | None = None,
    ) -> list[DecisionRecord]:
        """Filter and return rows. Sorted ts ASC.

        Args:
            since: ts >= since.
            until: ts < until.
            delivery_tier: only this delivery tier (e.g. only URGENT → EMERGENCY alerts).
            state_change_to: only transitions to this tier (e.g. only escalations to Tier.EMERGENCY).
            fused_event_ref: every decision produced by a specific fused event
                (usually 1, but kept generic in case multiple policies apply to
                the same event).
            limit: cap on result row count.

        Returns:
            list[DecisionRecord]: ts ascending.
        """
        clauses: list[str] = []
        params: list = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("ts < ?")
            params.append(until.isoformat())
        if delivery_tier is not None:
            clauses.append("delivery_tier = ?")
            params.append(delivery_tier.value)
        if state_change_to is not None:
            clauses.append("state_change_to = ?")
            params.append(state_change_to.value)
        if fused_event_ref is not None:
            clauses.append("fused_event_ref = ?")
            params.append(fused_event_ref)

        sql = "SELECT * FROM decisions"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts ASC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def read_recent(self, limit: int = 50) -> list[DecisionRecord]:
        """Most recent N rows (ts DESC). For UI / CLI "what just happened?".

        Note: unlike read_range, this is ts DESC — i.e. [0] is the most recent.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM decisions ORDER BY ts DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]

    # ─────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ─────────────────────────────────────────────────────────────────
    # Internal — row → Pydantic
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DecisionRecord:
        # Restore the state_change tuple.
        sc_from = row["state_change_from"]
        sc_to = row["state_change_to"]
        if sc_from is not None and sc_to is not None:
            state_change: tuple[Tier, Tier] | None = (Tier(sc_from), Tier(sc_to))
        else:
            state_change = None

        cooldown = row["cooldown_until"]
        cooldown_dt = datetime.fromisoformat(cooldown) if cooldown else None

        return DecisionRecord(
            id=row["id"],
            ts=datetime.fromisoformat(row["ts"]),
            fused_event_ref=row["fused_event_ref"],
            recommended_action=RecommendedAction(row["recommended_action"]),
            policy_version=row["policy_version"],
            notes=row["notes"],
            state_change=state_change,
            delivery_tier=DeliveryTier(row["delivery_tier"]),
            delivery_channels=json.loads(row["delivery_channels"]),
            cooldown_until=cooldown_dt,
            external_links=json.loads(row["external_links"]),
        )
