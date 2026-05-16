"""
storage/polymarket_baseline_store.py — Time-of-day Polymarket baseline (P9.1 M1).

────────────────────────────────────────────────────────────────────────
Role (docs/p9-detection-design.md M1):
    Polymarket markets have very different volume patterns by time of day.
        e.g. active in US mornings, quiet in Korean early hours.
    Using only a simple 30-minute rolling baseline can make even normal
    early-hours activity look like "z-score 5σ".

    → Aggregate samples from the past 14 days of "same weekday + hour + 5-minute
      bucket" and compute a median/MAD baseline (robust statistics).
    → Measure "how unusual is the current time vs. usual same-time-of-day"
      more accurately.

────────────────────────────────────────────────────────────────────────
Design decisions:

    · Bucket key = (symbol, weekday[0-6], hour[0-23], minute_bucket[0,5,10,...,55]).
      → Possible buckets per symbol: 7 × 24 × 12 = 2016. 14 days ≈ ~4000 rows.
      → One SQLite database fits even with many symbols.

    · A bucket's sample = "USD volume sum over that 5 minutes" (single scalar).
      Median/MAD are computed in Python from the samples SQLite gathers at query time.
      (SQLite has no built-in median — external functions are possible but kept
      simple for v1.)

    · Retention = 14 days (default). prune_older_than() for periodic cleanup.
      Daemon calling this once a day is enough.

    · Uses the same SQLite pattern as signal_store / decision_store (WAL, threading.Lock).

────────────────────────────────────────────────────────────────────────
Table layout:

    polymarket_baseline_samples
        symbol         TEXT NOT NULL    e.g. "iran-strike-by-feb28"
        ts             TEXT NOT NULL    bucket_end (ISO 8601 UTC) — end of the bucket
        weekday        INTEGER NOT NULL  0=Monday .. 6=Sunday (datetime.weekday())
        hour           INTEGER NOT NULL  0..23 (UTC)
        minute_bucket  INTEGER NOT NULL  0,5,10,...,55 (5-min bins)
        volume_usd     REAL NOT NULL     USD sum over [bucket_end - 5min, bucket_end)
        PRIMARY KEY (symbol, ts)         re-record on the same bucket → REPLACE

        INDEX ix_pbs_lookup (symbol, weekday, hour, minute_bucket)
        INDEX ix_pbs_ts     (ts)

────────────────────────────────────────────────────────────────────────
Architecture: §4.2 Storage (SQLite for query-heavy state)
Plan: docs/p9-detection-design.md P9.1 M1
"""

# Standard library only — no external deps.
from __future__ import annotations

import logging
import sqlite3
import statistics
import threading
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


# =====================================================================
# Width of one bucket. v1 is fixed at 5 minutes — must match features.py's
# current_window for the meaning to line up. If you change this, change features.py too.
# =====================================================================
_BUCKET_WIDTH_SECONDS: int = 300  # 5 minutes


# =====================================================================
# DDL
# =====================================================================
_DDL_TABLE = """
CREATE TABLE IF NOT EXISTS polymarket_baseline_samples (
    symbol         TEXT    NOT NULL,
    ts             TEXT    NOT NULL,
    weekday        INTEGER NOT NULL,
    hour           INTEGER NOT NULL,
    minute_bucket  INTEGER NOT NULL,
    volume_usd     REAL    NOT NULL,
    PRIMARY KEY (symbol, ts)
);
"""

_DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_pbs_lookup "
    "ON polymarket_baseline_samples(symbol, weekday, hour, minute_bucket);",
    "CREATE INDEX IF NOT EXISTS ix_pbs_ts "
    "ON polymarket_baseline_samples(ts);",
]


# =====================================================================
# Helper functions — bucket alignment
# =====================================================================
def align_to_bucket_end(ts: datetime) -> datetime:
    """Return the "end" of the 5-minute bucket containing ts (= next 5-min boundary).

    Examples:
        14:23:45 → 14:25:00
        14:25:00 → 14:25:00 (already on a boundary, return as-is)
        14:25:01 → 14:30:00

    bucket = [bucket_end - 5min, bucket_end) — end exclusive, start inclusive.

    Args:
        ts: UTC datetime (must be tz-aware).

    Returns:
        bucket_end (UTC datetime). Always on a 5-min boundary (seconds/microseconds = 0).
    """
    if ts.tzinfo is None:
        raise ValueError("align_to_bucket_end: ts must be timezone-aware (UTC).")

    # Drop seconds/microseconds and floor to the minute.
    floored_min = (ts.minute // 5) * 5
    bucket_start = ts.replace(minute=floored_min, second=0, microsecond=0)

    # If ts is exactly on the boundary, that's the bucket_end. Otherwise add 5 min.
    if ts == bucket_start:
        return bucket_start
    return bucket_start + timedelta(seconds=_BUCKET_WIDTH_SECONDS)


def extract_bucket_keys(bucket_end: datetime) -> tuple[int, int, int]:
    """Extract the lookup key (weekday, hour, minute_bucket) from bucket_end.

    minute_bucket is the bucket's "start" minute (= bucket_end.minute - 5, mod 60).
        e.g. bucket_end=14:25:00 → bucket_start=14:20:00 → minute_bucket=20.

    The weekday/hour of bucket_start defines the "time bracket" we group by.

    Args:
        bucket_end: datetime aligned to a 5-minute boundary (UTC).

    Returns:
        (weekday[0-6], hour[0-23], minute_bucket[0,5,...,55])
    """
    bucket_start = bucket_end - timedelta(seconds=_BUCKET_WIDTH_SECONDS)
    return (
        bucket_start.weekday(),  # 0=Monday .. 6=Sunday
        bucket_start.hour,
        bucket_start.minute,
    )


# =====================================================================
# PolymarketBaselineStore
# =====================================================================
class PolymarketBaselineStore:
    """SQLite-backed time-of-day baseline buckets for Polymarket (P9.1 M1).

    Lifecycle:
        store = PolymarketBaselineStore(Path("data/polymarket_baseline.db"))
        store.record_bucket("iran-strike", bucket_end_ts, total_usd_in_bucket)
        median, mad, n = store.get_baseline("iran-strike", current_ts)
        store.prune_older_than(cutoff)
        store.close()
    """

    def __init__(
        self,
        db_path: Path,
        *,
        retention_days: int = 14,
    ) -> None:
        """
        Args:
            db_path: SQLite file path. Parent dir is auto-created.
            retention_days: default cutoff (in days) for prune_older_than.
                14 days is enough for a robust median (~2 samples per weekday).
        """
        if retention_days < 2:
            raise ValueError("retention_days >= 2 recommended (for median computation)")

        self._db_path = db_path
        self._retention_days = retention_days
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # Same pattern: WAL + autocommit + check_same_thread=False
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.row_factory = sqlite3.Row

        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")

        # write serialization lock
        self._lock = threading.Lock()

        # schema init (idempotent)
        self._init_schema()

        logger.info(
            "PolymarketBaselineStore: opened db=%s retention=%dd",
            db_path, retention_days,
        )

    def _init_schema(self) -> None:
        """Create the table + indexes. No-op if they already exist."""
        with self._lock:
            self._conn.execute(_DDL_TABLE)
            for ddl in _DDL_INDEXES:
                self._conn.execute(ddl)

    # ─────────────────────────────────────────────────────────────────
    # Public API — write
    # ─────────────────────────────────────────────────────────────────
    def record_bucket(
        self,
        symbol: str,
        bucket_end: datetime,
        volume_usd: float,
    ) -> None:
        """Record (or overwrite) one 5-minute bucket.

        Same (symbol, bucket_end) re-arriving → INSERT OR REPLACE — overwritten
        with the most recent value. (Handles the case where the same bucket is
        re-computed after a re-fetch.)

        Args:
            symbol: market slug (Polymarket).
            bucket_end: UTC datetime aligned to a 5-min boundary
                       (caller aligns with align_to_bucket_end() before passing).
            volume_usd: total USD volume over [bucket_end - 5min, bucket_end). 0 is OK.
        """
        if volume_usd < 0:
            raise ValueError(f"volume_usd must be >= 0, got {volume_usd}")
        if bucket_end.tzinfo is None:
            raise ValueError("bucket_end must be timezone-aware (UTC).")

        weekday, hour, minute_bucket = extract_bucket_keys(bucket_end)

        sql = """
            INSERT OR REPLACE INTO polymarket_baseline_samples
                (symbol, ts, weekday, hour, minute_bucket, volume_usd)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        params = (
            symbol,
            bucket_end.isoformat(),
            weekday,
            hour,
            minute_bucket,
            float(volume_usd),
        )
        with self._lock:
            self._conn.execute(sql, params)

    # ─────────────────────────────────────────────────────────────────
    # Public API — read
    # ─────────────────────────────────────────────────────────────────
    def get_baseline(
        self,
        symbol: str,
        ts: datetime,
        *,
        lookback_days: int | None = None,
    ) -> tuple[float, float, int]:
        """Compute baseline from past samples of the same weekday/hour/5min-bucket.

        Args:
            symbol: market slug.
            ts: "now" (UTC). Bucket alignment is handled internally.
            lookback_days: how many days of history to look at. None → retention_days.

        Returns:
            (median, mad, n_samples):
                median   : median of volume_usd. 0.0 if n=0.
                mad      : Median Absolute Deviation. 0.0 if n=0 or all identical.
                n_samples: number of matching samples.

            When n_samples is too low (<3), callers should not trust the baseline.
        """
        if lookback_days is None:
            lookback_days = self._retention_days

        # Lookup key for the bucket that contains ts.
        bucket_end = align_to_bucket_end(ts)
        weekday, hour, minute_bucket = extract_bucket_keys(bucket_end)

        cutoff = ts - timedelta(days=lookback_days)

        sql = """
            SELECT volume_usd
            FROM polymarket_baseline_samples
            WHERE symbol = ?
              AND weekday = ?
              AND hour = ?
              AND minute_bucket = ?
              AND ts >= ?
            ORDER BY ts DESC
        """
        params = (
            symbol,
            weekday,
            hour,
            minute_bucket,
            cutoff.isoformat(),
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        volumes = [float(r["volume_usd"]) for r in rows]
        n = len(volumes)
        if n == 0:
            return (0.0, 0.0, 0)

        # statistics.median: with even count, returns mean of the two middle values.
        med = float(statistics.median(volumes))
        # MAD = median(|x - median|). Robust spread (more outlier-resistant than stdev).
        abs_dev = [abs(v - med) for v in volumes]
        mad = float(statistics.median(abs_dev)) if abs_dev else 0.0

        return (med, mad, n)

    # ─────────────────────────────────────────────────────────────────
    # Public API — maintenance
    # ─────────────────────────────────────────────────────────────────
    def prune_older_than(self, cutoff: datetime) -> int:
        """Delete bucket samples older than `cutoff`. Returns rows deleted.

        Recommended to call periodically (e.g. once per day). Cleans data older
        than retention_days.
        """
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware (UTC).")

        sql = "DELETE FROM polymarket_baseline_samples WHERE ts < ?"
        with self._lock:
            cur = self._conn.execute(sql, (cutoff.isoformat(),))
            deleted = cur.rowcount
        logger.info(
            "PolymarketBaselineStore.prune: deleted %d rows older than %s",
            deleted, cutoff.isoformat(),
        )
        return deleted

    # ─────────────────────────────────────────────────────────────────
    # Diagnostics — for test/debug
    # ─────────────────────────────────────────────────────────────────
    def total_rows(self, symbol: str | None = None) -> int:
        """Total stored sample rows. If `symbol` is given, restrict to that symbol."""
        if symbol is None:
            sql = "SELECT COUNT(*) AS n FROM polymarket_baseline_samples"
            params: tuple = ()
        else:
            sql = "SELECT COUNT(*) AS n FROM polymarket_baseline_samples WHERE symbol = ?"
            params = (symbol,)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    def latest_bucket_end(self, symbol: str | None = None) -> datetime | None:
        """Most recently recorded bucket_end.

        symbol=None: max across everything (for the snapshot endpoint — "is the store alive?").
        symbol set : that symbol only (for debug/diagnostic).

        features.py could use this to track "where did we record up to?", but in
        v1 only in-memory state (`_SymbolState.last_recorded_bucket_end`) is used,
        making this auxiliary.
        """
        if symbol is None:
            sql = "SELECT MAX(ts) AS max_ts FROM polymarket_baseline_samples"
            params: tuple = ()
        else:
            sql = "SELECT MAX(ts) AS max_ts FROM polymarket_baseline_samples WHERE symbol = ?"
            params = (symbol,)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        if not row or not row["max_ts"]:
            return None
        return datetime.fromisoformat(row["max_ts"])

    # ─────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────
    def close(self) -> None:
        """Close the SQLite connection. Called on daemon shutdown."""
        with self._lock:
            try:
                self._conn.close()
            except Exception as e:  # pragma: no cover
                logger.warning("PolymarketBaselineStore.close: %s", e)
        logger.info("PolymarketBaselineStore: closed db=%s", self._db_path)
