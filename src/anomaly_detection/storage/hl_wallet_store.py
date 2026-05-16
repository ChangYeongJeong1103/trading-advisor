"""
storage/hl_wallet_store.py — Hyperliquid wallet/trade tracking store (P9.2.P2).

────────────────────────────────────────────────────────────────────────
Role (docs/p9-detection-design.md P9.2 — D2 New whale emergence):
    Cumulatively track the wallet addresses returned by Hyperliquid recentTrades.

    To catch "wallets first seen within the last 24 hours that quickly
    accumulate a large notional", we store two kinds of data:

      1) hl_wallet           : wallet metadata (first_seen / last_seen / cumulative notional)
      2) hl_wallet_trade     : individual trades in the most recent 1-hour window (rolling)

    The detector queries "fresh wallet × 5-minute cumulative notional" and
    decides the tier.

────────────────────────────────────────────────────────────────────────
Design decisions (same pattern as PolymarketBaselineStore):

    · SQLite + WAL + autocommit + threading.Lock for thread safety.
    · Uses INSERT OR IGNORE / INSERT OR REPLACE — safe to re-record.
    · trade table has 1-hour retention (rolling window). Prune every 5 minutes.
    · Wallet metadata has 7-day retention. Prune once a day.
    · Persist "started_at_utc" at store init → used as a cold-start guard:
       first_seen is only meaningful after the daemon boot time — wallets
       seen before that may be "just newly observed by the store but
       actually old" so we conservatively define fresh as
       "first_seen >= max(now-24h, started_at)".

────────────────────────────────────────────────────────────────────────
Table layout:

    hl_wallet
      wallet                TEXT PRIMARY KEY    (taker address, lowercase)
      first_seen_ms         INTEGER NOT NULL    (the first trade time this store saw, UTC ms)
      last_seen_ms          INTEGER NOT NULL
      total_notional_usd    REAL    NOT NULL    (cumulative notional, taker side)
      trade_count           INTEGER NOT NULL

    hl_wallet_trade   (rolling 1h)
      tid                   INTEGER PRIMARY KEY (Hyperliquid trade id)
      wallet                TEXT NOT NULL       (taker address)
      coin                  TEXT NOT NULL
      side                  TEXT NOT NULL       ("B" buy / "A" sell)
      px                    REAL NOT NULL
      sz                    REAL NOT NULL
      ts_ms                 INTEGER NOT NULL    (fill time, UTC ms)

      INDEX ix_hwt_wallet_ts (wallet, ts_ms)
      INDEX ix_hwt_ts        (ts_ms)
      INDEX ix_hwt_coin_ts   (coin, ts_ms)

    hl_wallet_meta
      key                   TEXT PRIMARY KEY    (e.g. "started_at_ms")
      value                 TEXT NOT NULL

────────────────────────────────────────────────────────────────────────
Architecture: §4.2 Storage (SQLite for query-heavy state)
Plan: docs/anomaly-upgrade-plan.md P9.2 D2
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


# =====================================================================
# DDL
# =====================================================================
_DDL_TABLE_WALLET = """
CREATE TABLE IF NOT EXISTS hl_wallet (
    wallet              TEXT    PRIMARY KEY,
    first_seen_ms       INTEGER NOT NULL,
    last_seen_ms        INTEGER NOT NULL,
    total_notional_usd  REAL    NOT NULL,
    trade_count         INTEGER NOT NULL
);
"""

_DDL_TABLE_TRADE = """
CREATE TABLE IF NOT EXISTS hl_wallet_trade (
    tid     INTEGER PRIMARY KEY,
    wallet  TEXT    NOT NULL,
    coin    TEXT    NOT NULL,
    side    TEXT    NOT NULL,
    px      REAL    NOT NULL,
    sz      REAL    NOT NULL,
    ts_ms   INTEGER NOT NULL
);
"""

_DDL_TABLE_META = """
CREATE TABLE IF NOT EXISTS hl_wallet_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_hwt_wallet_ts ON hl_wallet_trade(wallet, ts_ms);",
    "CREATE INDEX IF NOT EXISTS ix_hwt_ts        ON hl_wallet_trade(ts_ms);",
    "CREATE INDEX IF NOT EXISTS ix_hwt_coin_ts   ON hl_wallet_trade(coin, ts_ms);",
    "CREATE INDEX IF NOT EXISTS ix_hwt_wallet_first_seen ON hl_wallet(first_seen_ms);",
]


# =====================================================================
# Public dataclasses — shapes handed to the detector
# =====================================================================
@dataclass(frozen=True)
class FreshWhaleCandidate:
    """A wallet first seen within 24h whose recent N-minute cumulative notional is large.

    Fields:
        wallet            : taker address (lowercase).
        coin              : most recently traded coin.
        first_seen_ms     : first trade time as known to this store (UTC ms).
        cum_notional_usd  : cumulative notional within the window (taker side, USD).
        trade_count       : number of trades in the window.
        last_side         : last trade side ("B"/"A") — direction hint.
    """
    wallet: str
    coin: str
    first_seen_ms: int
    cum_notional_usd: float
    trade_count: int
    last_side: str


@dataclass(frozen=True)
class ClusterCandidate:
    """Distributed-betting cluster (P9.2.P3) — a group of fresh wallets entering the
    same coin × same side × similar price band within a short window.

    Fields:
        coin              : traded coin.
        side              : "B" (taker buy) / "A" (taker sell).
        price_anchor      : representative cluster price (anchor_px). Band center.
        n_wallets         : number of distinct wallets in the cluster.
        sum_notional_usd  : total cumulative notional in the cluster (USD).
        last_ts_ms        : most recent trade time in the cluster (UTC ms).
        first_ts_ms       : oldest trade time in the cluster (UTC ms).
    """
    coin: str
    side: str
    price_anchor: float
    n_wallets: int
    sum_notional_usd: float
    last_ts_ms: int
    first_ts_ms: int


# =====================================================================
# HLWalletStore
# =====================================================================
class HLWalletStore:
    """SQLite-backed Hyperliquid wallet/trade tracker (P9.2 D2).

    Lifecycle:
        store = HLWalletStore(Path("data/hl_wallet/hl_wallet.db"))
        store.record_trades(trades_list)       # after collector polling
        candidates = store.get_fresh_whale_candidates(now=...)
        store.prune_trades_older_than(now - 1h)
        store.close()
    """

    def __init__(
        self,
        db_path: Path,
        *,
        trade_retention_hours: int = 1,
        wallet_retention_days: int = 7,
    ) -> None:
        """
        Args:
            db_path: SQLite file path. The parent dir is created automatically.
            trade_retention_hours: hl_wallet_trade retention duration. Default 1h.
                Plenty if the detector window is 5–10 minutes. Longer = more disk.
            wallet_retention_days: hl_wallet (metadata) retention in days. Default 7.
        """
        if trade_retention_hours < 1:
            raise ValueError("trade_retention_hours >= 1 recommended")
        if wallet_retention_days < 1:
            raise ValueError("wallet_retention_days >= 1 recommended")

        self._db_path = db_path
        self._trade_retention_hours = trade_retention_hours
        self._wallet_retention_days = wallet_retention_days
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

        self._lock = threading.Lock()
        self._init_schema()

        # For the cold-start guard — the time the store was first initialized
        # (persisted across restarts).
        self._started_at_ms = self._get_or_init_started_at_ms()

        logger.info(
            "HLWalletStore: opened db=%s trade_retention=%dh wallet_retention=%dd "
            "started_at_ms=%d",
            db_path, trade_retention_hours, wallet_retention_days,
            self._started_at_ms,
        )

    def _init_schema(self) -> None:
        """Create tables + indexes. Idempotent."""
        with self._lock:
            self._conn.execute(_DDL_TABLE_WALLET)
            self._conn.execute(_DDL_TABLE_TRADE)
            self._conn.execute(_DDL_TABLE_META)
            for ddl in _DDL_INDEXES:
                self._conn.execute(ddl)

    def _get_or_init_started_at_ms(self) -> int:
        """Read started_at_ms from the meta table. If missing, record the current time."""
        sql_get = "SELECT value FROM hl_wallet_meta WHERE key = ?"
        sql_put = "INSERT OR IGNORE INTO hl_wallet_meta(key, value) VALUES (?, ?)"
        with self._lock:
            row = self._conn.execute(sql_get, ("started_at_ms",)).fetchone()
            if row is not None:
                try:
                    return int(row["value"])
                except (TypeError, ValueError):
                    pass
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            self._conn.execute(sql_put, ("started_at_ms", str(now_ms)))
            return now_ms

    @property
    def started_at_ms(self) -> int:
        """The time (UTC ms) this store was first initialized (persisted)."""
        return self._started_at_ms

    # ─────────────────────────────────────────────────────────────────
    # Public API — write
    # ─────────────────────────────────────────────────────────────────
    def record_trades(
        self,
        trades: list[dict],
        *,
        taker_only: bool = True,
    ) -> int:
        """Receive a recentTrades response and update both wallet/trade tables.

        Each element of `trades` follows the Hyperliquid recentTrades schema:
            {"coin": str, "side": "B"|"A", "px": str, "sz": str,
             "time": int (ms), "tid": int, "users": [taker, maker], ...}

        taker_only=True (default): treat only users[0] (taker) as the wallet.
            Insiders are typically takers (aggressive entries), so this avoids
            maker (market maker) noise.

        Returns:
            int — number of trades newly INSERTed (after dedupe). The same tid
            re-arriving is ignored.
        """
        if not trades:
            return 0

        sql_trade = """
            INSERT OR IGNORE INTO hl_wallet_trade
                (tid, wallet, coin, side, px, sz, ts_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        # Wallet meta upsert: for a new wallet, record first_seen; for an existing
        # wallet, update last_seen / cumulative.
        sql_wallet = """
            INSERT INTO hl_wallet
                (wallet, first_seen_ms, last_seen_ms, total_notional_usd, trade_count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(wallet) DO UPDATE SET
                last_seen_ms       = MAX(hl_wallet.last_seen_ms, excluded.last_seen_ms),
                total_notional_usd = hl_wallet.total_notional_usd + excluded.total_notional_usd,
                trade_count        = hl_wallet.trade_count + 1
        """

        inserted = 0
        with self._lock:
            for t in trades:
                try:
                    parsed = _parse_trade(t, taker_only=taker_only)
                except _BadTrade:
                    continue
                if parsed is None:
                    continue

                tid, wallet, coin, side, px, sz, ts_ms = parsed
                notional_usd = px * sz

                # trade row insert (dedupe by tid)
                cur = self._conn.execute(
                    sql_trade,
                    (tid, wallet, coin, side, px, sz, ts_ms),
                )
                if cur.rowcount > 0:
                    inserted += 1
                    # Only update wallet meta on a brand-new trade
                    # (prevents cumulative blow-up on duplicates).
                    self._conn.execute(
                        sql_wallet,
                        (wallet, ts_ms, ts_ms, notional_usd),
                    )
        return inserted

    # ─────────────────────────────────────────────────────────────────
    # Public API — read (detector calls this every cycle)
    # ─────────────────────────────────────────────────────────────────
    def get_fresh_whale_candidates(
        self,
        now: datetime,
        *,
        coin: str | None = None,
        window_min: int = 5,
        fresh_within_h: int = 24,
        min_cum_notional_usd: float = 1_000_000.0,
    ) -> list[FreshWhaleCandidate]:
        """Return fresh wallets (first seen in last 24h) with the largest
        recent window_min cumulative notional.

        Args:
            now: current time (UTC, tz-aware).
            coin: filter to a specific coin (None = all).
            window_min: window (minutes) for the recent cumulative notional.
            fresh_within_h: only wallets with "first_seen ≥ now-Nh".
                            Cold-start guard: the real cutoff is not
                            max(now - fresh_within_h, store.started_at + warmup),
                            we only apply the "fresh" decision here. Callers
                            judge cold-start separately.
            min_cum_notional_usd: candidates below this are dropped (small noise filter).

        Returns:
            list[FreshWhaleCandidate], sorted by cum_notional_usd descending.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware (UTC)")

        now_ms = int(now.timestamp() * 1000)
        window_cutoff_ms = now_ms - window_min * 60 * 1000
        fresh_cutoff_ms = now_ms - fresh_within_h * 3600 * 1000

        # SQL: fresh-wallet filter + sum trades within the window.
        # px*sz sum uses SUM(px*sz) in SQLite.
        params: list = [fresh_cutoff_ms, window_cutoff_ms]
        coin_clause = ""
        if coin is not None:
            coin_clause = " AND t.coin = ? "
            params.append(coin)

        sql = f"""
            SELECT
                t.wallet                              AS wallet,
                MAX(t.coin)                           AS last_coin,
                w.first_seen_ms                       AS first_seen_ms,
                SUM(t.px * t.sz)                      AS cum_notional_usd,
                COUNT(*)                              AS trade_count,
                (
                    SELECT side
                    FROM hl_wallet_trade t2
                    WHERE t2.wallet = t.wallet
                    ORDER BY t2.ts_ms DESC
                    LIMIT 1
                )                                     AS last_side
            FROM hl_wallet_trade t
            JOIN hl_wallet w ON w.wallet = t.wallet
            WHERE w.first_seen_ms >= ?
              AND t.ts_ms >= ?
              {coin_clause}
            GROUP BY t.wallet
            HAVING cum_notional_usd >= ?
            ORDER BY cum_notional_usd DESC
        """
        params.append(min_cum_notional_usd)

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        out: list[FreshWhaleCandidate] = []
        for r in rows:
            out.append(
                FreshWhaleCandidate(
                    wallet=str(r["wallet"]),
                    coin=str(r["last_coin"] or ""),
                    first_seen_ms=int(r["first_seen_ms"]),
                    cum_notional_usd=float(r["cum_notional_usd"] or 0.0),
                    trade_count=int(r["trade_count"] or 0),
                    last_side=str(r["last_side"] or ""),
                )
            )
        return out

    # ─────────────────────────────────────────────────────────────────
    # Public API — cluster query (P9.2.P3 — D5 distributed betting)
    # ─────────────────────────────────────────────────────────────────
    def get_fresh_wallet_cluster(
        self,
        now: datetime,
        *,
        coin: str,
        anchor_px: float,
        fresh_within_h: int = 24,
        cluster_window_min: int = 10,
        price_band_pct: float = 0.005,
        min_n_wallets: int = 3,
        min_sum_notional_usd: float = 5_000_000.0,
    ) -> list[ClusterCandidate]:
        """Clusters where fresh wallets pile into the same coin × same side × similar price band.

        Distributed insiders don't dump big capital onto one wallet; they split
        it across several fresh wallets that enter at the same time, in the
        same direction, in a similar price band. We catch that pattern with
        SQL aggregation.

        Args:
            now: current time (UTC, tz-aware).
            coin: which coin's cluster to look at (required — price-band
                  comparison is per-coin).
            anchor_px: reference for the price-band grouping (typically the
                       current mark_px). The band is anchor ± price_band_pct,
                       making one bucket.
                       e.g. anchor_px=3000, price_band_pct=0.005 → bucket width = $15.
            fresh_within_h: "fresh" wallet definition — first_seen_ms ≥ now-Nh.
                            Keep this consistent with the P2 new_whale_v1 definition.
            cluster_window_min: only wallets with a trade in the last N minutes
                                are cluster candidates.
            price_band_pct: band width for grouping by price (fraction of anchor_px).
                            0.005 = ±0.5% (1% total span) — one bucket.
            min_n_wallets: minimum distinct wallets required to count as a cluster.
                  min_sum_notional_usd: minimum total notional required.

        Returns:
            list[ClusterCandidate], sorted by sum_notional_usd desc.
            If clusters form in multiple (side × price_bucket) combos at once,
            all of them are returned.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware (UTC)")
        if anchor_px <= 0:
            return []
        if price_band_pct <= 0:
            raise ValueError("price_band_pct must be > 0")
        if min_n_wallets < 2:
            raise ValueError("min_n_wallets >= 2 recommended (cluster definition)")

        now_ms = int(now.timestamp() * 1000)
        window_cutoff_ms = now_ms - cluster_window_min * 60 * 1000
        fresh_cutoff_ms = now_ms - fresh_within_h * 3600 * 1000

        # Band width (USD) — px values in the same integer bucket count as the
        # same price band.
        band_width = anchor_px * price_band_pct
        if band_width <= 0:
            return []

        # SQL: group by side × floor(px/band_width); filter on fresh wallet × window.
        # SQLite's CAST(... AS INTEGER) acts like floor (we only deal with positive px so this is OK).
        sql = """
            SELECT
                t.side                                              AS side,
                CAST(t.px / ? AS INTEGER)                           AS px_bucket,
                AVG(t.px)                                           AS price_anchor,
                COUNT(DISTINCT t.wallet)                            AS n_wallets,
                SUM(t.px * t.sz)                                    AS sum_notional_usd,
                MAX(t.ts_ms)                                        AS last_ts_ms,
                MIN(t.ts_ms)                                        AS first_ts_ms
            FROM hl_wallet_trade t
            JOIN hl_wallet w ON w.wallet = t.wallet
            WHERE t.coin = ?
              AND w.first_seen_ms >= ?
              AND t.ts_ms         >= ?
            GROUP BY t.side, px_bucket
            HAVING n_wallets         >= ?
               AND sum_notional_usd  >= ?
            ORDER BY sum_notional_usd DESC
        """
        params = (
            band_width,
            coin,
            fresh_cutoff_ms,
            window_cutoff_ms,
            min_n_wallets,
            min_sum_notional_usd,
        )

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        out: list[ClusterCandidate] = []
        for r in rows:
            out.append(
                ClusterCandidate(
                    coin=coin,
                    side=str(r["side"]),
                    price_anchor=float(r["price_anchor"] or 0.0),
                    n_wallets=int(r["n_wallets"] or 0),
                    sum_notional_usd=float(r["sum_notional_usd"] or 0.0),
                    last_ts_ms=int(r["last_ts_ms"] or 0),
                    first_ts_ms=int(r["first_ts_ms"] or 0),
                )
            )
        return out

    # ─────────────────────────────────────────────────────────────────
    # Public API — maintenance
    # ─────────────────────────────────────────────────────────────────
    def prune_trades_older_than(self, cutoff: datetime) -> int:
        """Remove trade rows older than cutoff. Returns the count deleted."""
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware (UTC)")
        cutoff_ms = int(cutoff.timestamp() * 1000)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM hl_wallet_trade WHERE ts_ms < ?", (cutoff_ms,),
            )
            return int(cur.rowcount or 0)

    def prune_wallets_older_than(self, cutoff: datetime) -> int:
        """Remove wallet metadata whose last_seen is older than cutoff.

        Call periodically (e.g. once per day).
        """
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware (UTC)")
        cutoff_ms = int(cutoff.timestamp() * 1000)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM hl_wallet WHERE last_seen_ms < ?", (cutoff_ms,),
            )
            return int(cur.rowcount or 0)

    # ─────────────────────────────────────────────────────────────────
    # Diagnostics — for the snapshot endpoint
    # ─────────────────────────────────────────────────────────────────
    def total_wallets(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM hl_wallet"
            ).fetchone()
        return int(row["n"]) if row else 0

    def total_trades(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM hl_wallet_trade"
            ).fetchone()
        return int(row["n"]) if row else 0

    def latest_trade_ms(self) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts_ms) AS m FROM hl_wallet_trade"
            ).fetchone()
        if not row or row["m"] is None:
            return None
        return int(row["m"])

    # ─────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────
    def close(self) -> None:
        """Close the SQLite connection."""
        with self._lock:
            try:
                self._conn.close()
            except Exception as e:  # pragma: no cover
                logger.warning("HLWalletStore.close: %s", e)
        logger.info("HLWalletStore: closed db=%s", self._db_path)


# =====================================================================
# Internal helpers — trade row parsing
# =====================================================================
class _BadTrade(Exception):
    """Trade dict parse failure — sentinel used to skip a single row."""


def _parse_trade(
    t: dict,
    *,
    taker_only: bool,
) -> tuple[int, str, str, str, float, float, int] | None:
    """Convert one recentTrades element into (tid, wallet, coin, side, px, sz, ts_ms).

    Returns None on bad format (caller skips).
    """
    try:
        tid = int(t.get("tid"))
    except (TypeError, ValueError):
        return None

    coin = t.get("coin")
    side = t.get("side")
    if not isinstance(coin, str) or not isinstance(side, str):
        return None
    if side not in ("B", "A"):
        return None

    try:
        px = float(t.get("px"))
        sz = float(t.get("sz"))
    except (TypeError, ValueError):
        return None
    if px <= 0 or sz <= 0:
        return None

    try:
        ts_ms = int(t.get("time"))
    except (TypeError, ValueError):
        return None

    users = t.get("users")
    if not isinstance(users, list) or not users:
        return None
    if taker_only:
        wallet_raw = users[0]
    else:
        wallet_raw = users[0]  # taker first (same default — for maker accumulation, caller does it)
    if not isinstance(wallet_raw, str) or not wallet_raw:
        return None

    wallet = wallet_raw.lower().strip()
    return (tid, wallet, coin, side, px, sz, ts_ms)
