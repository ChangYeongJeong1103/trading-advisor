"""
storage/signal_store.py — Persist ChannelSignal + FusedAnomalyEvent to SQLite.

────────────────────────────────────────────────────────────────────────
Role (architecture §4.2):
  Persist v1 fusion engine inputs (ChannelSignal) and outputs (FusedAnomalyEvent).
  Main purposes:
    - audit/replay queries from CLI/SQL (`sqlite3 signals.db "SELECT ..."`)
    - post-alert retrospection "why did this alert fire?"
    - re-injecting the same signal during P10 historical event replay

  Why SQLite (not Parquet):
    - Small rows arrive frequently (a few to a few dozen per minute)
    - Point queries (lookup by id) are common
    - Need indexes on several columns
    - CLI ad-hoc SQL is useful
    - Single daemon = single writer → SQLite is enough

────────────────────────────────────────────────────────────────────────
Table layout:

  channel_signals
    id              TEXT PRIMARY KEY
    channel         TEXT NOT NULL
    symbol          TEXT NOT NULL
    ts              TEXT NOT NULL    (ISO 8601 UTC, e.g. "2026-04-13T10:23:45+00:00")
    score           REAL NOT NULL    (0.0 ~ 1.0)
    tier            TEXT NOT NULL    ("NORMAL"|"WATCH"|"RISK_OFF"|"EMERGENCY")
    direction       TEXT NOT NULL    ("up"|"down"|"neutral")
    confidence      REAL NOT NULL
    features_ref    TEXT             (FeatureSnapshot.id, nullable)
    fired_detectors TEXT NOT NULL    (JSON array)
    reason_codes    TEXT NOT NULL    (JSON array)

    INDEX  ix_cs_ch_sym_ts (channel, symbol, ts)
    INDEX  ix_cs_ts        (ts)
    INDEX  ix_cs_tier      (tier)

  fused_anomaly_events
    id                  TEXT PRIMARY KEY
    ts                  TEXT NOT NULL
    fused_score         REAL NOT NULL
    state               TEXT NOT NULL    (4-tier)
    tier_floor          TEXT NOT NULL
    boost_applied       TEXT             (nullable)
    per_channel_scores  TEXT NOT NULL    (JSON: {channel: score})
    per_channel_tiers   TEXT NOT NULL    (JSON: {channel: tier})
    per_channel_signal  TEXT NOT NULL    (JSON: {channel: signal_id|null})
    contributing        TEXT NOT NULL    (JSON array of signal_id)
    agreeing_channels   INTEGER NOT NULL
    agreeing_direction  TEXT             (nullable)
    weights             TEXT NOT NULL    (JSON: {channel: weight})
    rationale           TEXT NOT NULL

    INDEX  ix_fe_ts    (ts)
    INDEX  ix_fe_state (state)

────────────────────────────────────────────────────────────────────────
Foreign key policy:
  FusedAnomalyEvent.per_channel_signal / contributing reference ChannelSignal.id.
  But we don't enforce FKs — in real time, fused may commit before signal,
  and best-effort saves are better for audit than silent drops in race conditions.
  Joins are still possible at read time (lookup by signal_id).

────────────────────────────────────────────────────────────────────────
Concurrency:
  - WAL mode → readers can read even while a writer holds the lock.
  - threading.Lock explicitly serializes writes (safe with asyncio + thread executors).
  - External CLI (the `sqlite3` command) can also read concurrently.

Architecture: §4.2 Storage Layout, §5.4 state-decision flow
Plan: §11 D3 (signal_store=SQLite)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from ..core.schemas import (
    ChannelSignal,
    Direction,
    FusedAnomalyEvent,
    Tier,
)


# =====================================================================
# DDL — single source of truth. New columns go here + a migration function.
# =====================================================================
_DDL_CHANNEL_SIGNALS = """
CREATE TABLE IF NOT EXISTS channel_signals (
    id              TEXT PRIMARY KEY,
    channel         TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    ts              TEXT NOT NULL,
    score           REAL NOT NULL,
    tier            TEXT NOT NULL,
    direction       TEXT NOT NULL,
    confidence      REAL NOT NULL,
    features_ref    TEXT,
    fired_detectors TEXT NOT NULL,
    reason_codes    TEXT NOT NULL
);
"""

_DDL_FUSED_EVENTS = """
CREATE TABLE IF NOT EXISTS fused_anomaly_events (
    id                  TEXT PRIMARY KEY,
    ts                  TEXT NOT NULL,
    fused_score         REAL NOT NULL,
    state               TEXT NOT NULL,
    tier_floor          TEXT NOT NULL,
    boost_applied       TEXT,
    per_channel_scores  TEXT NOT NULL,
    per_channel_tiers   TEXT NOT NULL,
    per_channel_signal  TEXT NOT NULL,
    contributing        TEXT NOT NULL,
    agreeing_channels   INTEGER NOT NULL,
    agreeing_direction  TEXT,
    weights             TEXT NOT NULL,
    rationale           TEXT NOT NULL
);
"""

_DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_cs_ch_sym_ts ON channel_signals(channel, symbol, ts);",
    "CREATE INDEX IF NOT EXISTS ix_cs_ts        ON channel_signals(ts);",
    "CREATE INDEX IF NOT EXISTS ix_cs_tier      ON channel_signals(tier);",
    "CREATE INDEX IF NOT EXISTS ix_fe_ts        ON fused_anomaly_events(ts);",
    "CREATE INDEX IF NOT EXISTS ix_fe_state     ON fused_anomaly_events(state);",
]


# =====================================================================
# SignalStore
# =====================================================================
class SignalStore:
    """SQLite-backed store for ChannelSignal + FusedAnomalyEvent."""

    def __init__(self, db_path: Path) -> None:
        """
        Args:
            db_path: path like data/anomaly/signals/signals.db. Parent dir auto-created.
        """
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # check_same_thread=False → usable in asyncio + threading environments
        # (writes are serialized by _lock, so it's safe).
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit mode (manage BEGIN/COMMIT manually)
        )
        self._conn.row_factory = sqlite3.Row  # enables row["col"] access

        # PRAGMA — performance + safety.
        self._conn.execute("PRAGMA journal_mode=WAL;")        # allow concurrent reads
        self._conn.execute("PRAGMA synchronous=NORMAL;")      # recommended pair with WAL
        self._conn.execute("PRAGMA foreign_keys=OFF;")        # don't enforce FKs (race)
        self._conn.execute("PRAGMA busy_timeout=5000;")       # wait up to 5s on lock

        self._lock = threading.Lock()

        # Create tables + indexes (idempotent).
        self._init_schema()

    def _init_schema(self) -> None:
        """Create tables + indexes. No-op if they already exist."""
        with self._lock:
            self._conn.execute(_DDL_CHANNEL_SIGNALS)
            self._conn.execute(_DDL_FUSED_EVENTS)
            for ddl in _DDL_INDEXES:
                self._conn.execute(ddl)

    # ─────────────────────────────────────────────────────────────────
    # Public API — write
    # ─────────────────────────────────────────────────────────────────
    def insert_channel_signal(self, signal: ChannelSignal) -> None:
        """Insert one ChannelSignal. Same id → raises OperationalError.

        Note:
            id is auto-generated as uuid4 in schemas.py → effectively zero collisions.
            A duplicate insert is a caller bug, so we don't silently ignore it.
        """
        sql = """
            INSERT INTO channel_signals (
                id, channel, symbol, ts, score, tier, direction, confidence,
                features_ref, fired_detectors, reason_codes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            signal.id,
            signal.channel,
            signal.symbol,
            signal.ts.isoformat(),
            signal.score,
            signal.tier.value,
            signal.direction.value,
            signal.confidence,
            signal.features_ref,
            json.dumps(signal.fired_detectors, ensure_ascii=False),
            json.dumps(signal.reason_codes, ensure_ascii=False),
        )
        with self._lock:
            self._conn.execute(sql, params)

    def insert_fused_event(self, event: FusedAnomalyEvent) -> None:
        """Insert one FusedAnomalyEvent."""
        sql = """
            INSERT INTO fused_anomaly_events (
                id, ts, fused_score, state, tier_floor, boost_applied,
                per_channel_scores, per_channel_tiers, per_channel_signal,
                contributing, agreeing_channels, agreeing_direction,
                weights, rationale
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        # Tier enum dict → str dict (for JSON serialization)
        per_tiers_str = {ch: t.value for ch, t in event.per_channel_tiers.items()}

        params = (
            event.id,
            event.ts.isoformat(),
            event.fused_score,
            event.state.value,
            event.tier_floor.value,
            event.boost_applied,
            json.dumps(event.per_channel_scores, ensure_ascii=False),
            json.dumps(per_tiers_str, ensure_ascii=False),
            json.dumps(event.per_channel_signal, ensure_ascii=False),
            json.dumps(event.contributing, ensure_ascii=False),
            event.agreeing_channels,
            event.agreeing_direction.value if event.agreeing_direction else None,
            json.dumps(event.weights, ensure_ascii=False),
            event.rationale,
        )
        with self._lock:
            self._conn.execute(sql, params)

    # ─────────────────────────────────────────────────────────────────
    # Public API — read (single)
    # ─────────────────────────────────────────────────────────────────
    def get_channel_signal(self, signal_id: str) -> ChannelSignal | None:
        """Lookup one ChannelSignal by id. None if absent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM channel_signals WHERE id = ?", (signal_id,)
            ).fetchone()
        return self._row_to_channel_signal(row) if row else None

    def get_fused_event(self, event_id: str) -> FusedAnomalyEvent | None:
        """Lookup one FusedAnomalyEvent by id. None if absent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM fused_anomaly_events WHERE id = ?", (event_id,)
            ).fetchone()
        return self._row_to_fused_event(row) if row else None

    # ─────────────────────────────────────────────────────────────────
    # Public API — read (range)
    # ─────────────────────────────────────────────────────────────────
    def read_channel_signals(
        self,
        channel: str | None = None,
        symbol: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        tier: Tier | None = None,
        limit: int | None = None,
    ) -> list[ChannelSignal]:
        """Filter and return ChannelSignals. Sorted ts ASC.

        Args:
            channel: only this channel. None = all.
            symbol: only this symbol.
            since: ts >= since (UTC).
            until: ts < until (UTC).
            tier: only this tier (e.g. only Tier.EMERGENCY).
            limit: cap on result row count.

        Returns:
            list[ChannelSignal]: ts ascending.
        """
        clauses, params = self._build_signal_where(channel, symbol, since, until, tier)
        sql = "SELECT * FROM channel_signals"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts ASC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_channel_signal(r) for r in rows]

    def read_fused_events(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        state: Tier | None = None,
        limit: int | None = None,
    ) -> list[FusedAnomalyEvent]:
        """Filter and return FusedAnomalyEvents. Sorted ts ASC."""
        clauses: list[str] = []
        params: list = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("ts < ?")
            params.append(until.isoformat())
        if state is not None:
            clauses.append("state = ?")
            params.append(state.value)

        sql = "SELECT * FROM fused_anomaly_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts ASC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_fused_event(r) for r in rows]

    # ─────────────────────────────────────────────────────────────────
    # Public API — counts (for monitoring)
    # ─────────────────────────────────────────────────────────────────
    def count_channel_signals(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM channel_signals").fetchone()[0]

    def count_fused_events(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM fused_anomaly_events").fetchone()[0]

    # ─────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────
    def close(self) -> None:
        """Close the connection. Recommended on daemon shutdown."""
        with self._lock:
            self._conn.close()

    # ─────────────────────────────────────────────────────────────────
    # Internal — row → Pydantic
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _row_to_channel_signal(row: sqlite3.Row) -> ChannelSignal:
        return ChannelSignal(
            id=row["id"],
            channel=row["channel"],
            symbol=row["symbol"],
            ts=datetime.fromisoformat(row["ts"]),
            score=row["score"],
            tier=Tier(row["tier"]),
            direction=Direction(row["direction"]),
            confidence=row["confidence"],
            features_ref=row["features_ref"],
            fired_detectors=json.loads(row["fired_detectors"]),
            reason_codes=json.loads(row["reason_codes"]),
        )

    @staticmethod
    def _row_to_fused_event(row: sqlite3.Row) -> FusedAnomalyEvent:
        per_tiers_raw: dict[str, str] = json.loads(row["per_channel_tiers"])
        per_tiers = {ch: Tier(t) for ch, t in per_tiers_raw.items()}

        return FusedAnomalyEvent(
            id=row["id"],
            ts=datetime.fromisoformat(row["ts"]),
            fused_score=row["fused_score"],
            state=Tier(row["state"]),
            tier_floor=Tier(row["tier_floor"]),
            boost_applied=row["boost_applied"],
            per_channel_scores=json.loads(row["per_channel_scores"]),
            per_channel_tiers=per_tiers,
            per_channel_signal=json.loads(row["per_channel_signal"]),
            contributing=json.loads(row["contributing"]),
            agreeing_channels=row["agreeing_channels"],
            agreeing_direction=(
                Direction(row["agreeing_direction"]) if row["agreeing_direction"] else None
            ),
            weights=json.loads(row["weights"]),
            rationale=row["rationale"],
        )

    # ─────────────────────────────────────────────────────────────────
    # Internal — query builder
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _build_signal_where(
        channel: str | None,
        symbol: str | None,
        since: datetime | None,
        until: datetime | None,
        tier: Tier | None,
    ) -> tuple[list[str], list]:
        """Build the WHERE clause + parameterized values. Prevents SQL injection."""
        clauses: list[str] = []
        params: list = []
        if channel is not None:
            clauses.append("channel = ?")
            params.append(channel)
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("ts < ?")
            params.append(until.isoformat())
        if tier is not None:
            clauses.append("tier = ?")
            params.append(tier.value)
        return clauses, params
