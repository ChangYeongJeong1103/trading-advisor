"""
channel_replays/hyperliquid.py — HyperliquidChannelReplay (CSV trades → production detector).

────────────────────────────────────────────────────────────────────────
Role (P10.4 — free path):
  Run channel replay using only the single-wallet trades CSV distributed by
  hypedexer.com, without the Hyperliquid S3 archive (paid AWS requester-pays).

  Internal data flow (1 cycle = 1 minute):

    1. source.get_bar(coin, sim_clock)
         → BarTick (payload = {"trades": [...]}) or None (0 trades that minute).
    2. trades → build this minute's "synthetic snapshot"
         · day_ntl_vlm_usd : this wallet's cumulative notional (sum since start).
         · mark_px         : last trade px in this minute.
         · open_interest_coins : this wallet's net short position
                                 = sum(Open Short sz) - sum(Close Short sz).
         · funding_rate    : None (not in the CSV).
    3. One synthetic NormalizedEvent → features.add_events.
    4. features.compute_snapshot(coin, bar_end) → snapshot for the detector.
    5. Inject new_whale_* / cluster_* wallet features into snapshot.features:
         · last 5-minute ntl sum → new_whale_max_cum5m_usd
         · single wallet → count_24h = 1 (when present), cluster_* = 0
         · Open-Short family → last_side_code = -1 (A, sell)
           Close-Short family → +1 (B, buy)
    6. detector.evaluate → ChannelSignal (NORMAL included).

────────────────────────────────────────────────────────────────────────
Design decisions:

  · We assume the CSV was already exported with a single-wallet filter applied.
    → cluster_v1 cannot be verified (inject 0). new_whale_v1 is the key signal.

  · vol_z_v1 ends up using "this wallet's own historical vol" as the baseline,
    which means even the first trade produces a distorted, large z-score. To
    get a "whole-market vol baseline" we'd need the S3 asset_ctxs (planned for
    P10.4.b). For now treat it as a descriptive signal only.

  · insider_v1's OI delta / funding are based on "this wallet's net short", so
    they may differ from the whole-market OI/funding moves. Instead of the real
    values, it reflects "which direction this wallet accumulated".

  · Replay does not use production's wallet_store (SQLite warmup 24h). The
    fresh_within_24h guard is always True → more permissive than production.
    Even with a 27–48h CSV window, everything stays "fresh".

────────────────────────────────────────────────────────────────────────
References:
  · src/anomaly/channels/hyperliquid/features.py       — HyperliquidFeatures
  · src/anomaly/channels/hyperliquid/detector.py       — HyperliquidDetector
  · src/anomaly/channels/hyperliquid/channel.py        — _inject_wallet_features
  · src/anomaly/replay/data_sources/hyperliquid_trades_csv.py
  · src/anomaly/replay/channel_replays/polymarket.py   — parallel pattern
"""

from __future__ import annotations

# --- standard library ---
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import ClassVar

# --- local: production Hyperliquid pipeline ---
from ...channels.hyperliquid.detector import (
    HyperliquidDetector,
    HyperliquidDetectorConfig,
)
from ...channels.hyperliquid.features import HyperliquidFeatures
from ...core.schemas import (
    CHANNEL_HYPERLIQUID,
    ChannelSignal,
    NormalizedEvent,
    Tier,  # noqa: F401 — re-exported for external import convenience
)

# --- local: replay layer ---
from ..data_sources.hyperliquid_trades_csv import HyperliquidTradesCsvSource
from ..schemas import HistoricalEvent

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Side classification — CSV dir string → Hyperliquid A/B convention.
# ─────────────────────────────────────────────────────────────────────
# The production detector uses:
#   code = +1.0 → "B" (buy, bid hit not ask hit, buy fill)
#   code = -1.0 → "A" (sell, ask hit)
#   code =  0.0 → unknown / neutral
#
# CSV "dir" column interpretation:
#   "Open Short", "Add Short"  → trader opens/adds short = **sell** → "A" → -1.0
#   "Close Short"              → closing short = **buy**            → "B" → +1.0
#   "Open Long",  "Add Long"   → buy                                → "B" → +1.0
#   "Close Long"               → sell                               → "A" → -1.0
#   "Auto-Deleveraging", "Liquidation" → forced liquidations (filled on the opposite side).
#       Direction is treated as unknown (0.0) — not a voluntary intent.
_SELL_DIRS: frozenset[str] = frozenset({"Open Short", "Add Short", "Close Long"})
_BUY_DIRS: frozenset[str] = frozenset({"Close Short", "Open Long", "Add Long"})


def _dir_to_side_code(direction: str) -> float:
    """CSV dir string → side code (-1.0/0.0/+1.0).

    Unknown dir (e.g. "Auto-Deleveraging") → 0.0 (unknown).
    """
    if direction in _SELL_DIRS:
        return -1.0
    if direction in _BUY_DIRS:
        return +1.0
    return 0.0


def _dir_to_oi_sign(direction: str) -> float:
    """Sign used to compute this wallet's net short position.

    "Open Short"/"Add Short" → +1 (short increases)
    "Close Short"            → -1 (short decreases)
    everything else (long side) →  0 (no impact on short position)
    """
    if direction in ("Open Short", "Add Short"):
        return +1.0
    if direction == "Close Short":
        return -1.0
    return 0.0


# ─────────────────────────────────────────────────────────────────────
# Internal per-coin state
# ─────────────────────────────────────────────────────────────────────
@dataclass
class _CoinReplayState:
    """Cumulative state for one coin (e.g. BTC).

    · `cum_notional_usd` replaces production's dayNtlVlm — 'this wallet's
      cumulative notional'; features compute volume delta as the diff from
      the previous snapshot.
    · `net_short_coins` replaces open interest — "size of net short held by
      this wallet".
    · `last_mark_px` is this minute's last px (carries forward if absent).
    · `recent_trades` is the trades from the last ~7 minutes (buffer) —
      used for the new_whale 5-minute window calculation.
    """

    cum_notional_usd: float = 0.0
    net_short_coins: float = 0.0
    last_mark_px: float = 0.0
    # deque entries: (ts, ntl, side_code). Prune criterion: now - 5min.
    recent_trades: deque = field(default_factory=deque)


# ─────────────────────────────────────────────────────────────────────
# HyperliquidChannelReplay
# ─────────────────────────────────────────────────────────────────────
class HyperliquidChannelReplay:
    """Replay wrapper for the Hyperliquid channel. Satisfies the ChannelReplay Protocol.

    Args:
        source: HyperliquidTradesCsvSource (constructed before warmup).
            If None, a new one is created at the default path.
        detector_config: HyperliquidDetectorConfig override.
        new_whale_window_min: window (minutes) for the wallet's fresh
            cumulative notional. Same as the production default (5 minutes).
    """

    channel: ClassVar[str] = CHANNEL_HYPERLIQUID

    def __init__(
        self,
        *,
        source: HyperliquidTradesCsvSource | None = None,
        detector_config: HyperliquidDetectorConfig | None = None,
        features_kwargs: dict | None = None,
        new_whale_window_min: int = 5,
    ) -> None:
        self._source = source or HyperliquidTradesCsvSource()
        self._features = HyperliquidFeatures(**(features_kwargs or {}))
        self._detector = HyperliquidDetector(config=detector_config)
        self._new_whale_window = timedelta(minutes=new_whale_window_min)

        # Per-event state
        self._symbols: list[str] = []
        self._states: dict[str, _CoinReplayState] = {}
        self._event_id: str | None = None

    # ─────────────────────────────────────────────────────────────────
    # warmup
    # ─────────────────────────────────────────────────────────────────
    async def warmup(self, event: HistoricalEvent) -> None:
        """source warmup + activate only those primary/secondary symbols present in the CSV.

        Hyperliquid is only active when primary. Hyperliquid as secondary is
        not supported in v0.
        """
        self._event_id = event.event_id

        await self._source.warmup(event)

        warmed = self._source.warmed_symbols
        if event.primary_channel == CHANNEL_HYPERLIQUID:
            candidates = list(event.primary_symbols)
            mode = "primary"
        else:
            candidates = []
            mode = "inactive"

        self._symbols = [s for s in candidates if s in warmed]
        # Initialize coin state (idempotent)
        for sym in self._symbols:
            self._states.setdefault(sym, _CoinReplayState())

        if not self._symbols:
            logger.warning(
                "HyperliquidChannelReplay: no active coins for event=%s "
                "(mode=%s, candidates=%s, warmed=%s)",
                event.event_id, mode, candidates, sorted(warmed),
            )
        else:
            logger.info(
                "HyperliquidChannelReplay: warmed event=%s mode=%s active=%s "
                "(CSV rows loaded=%d)",
                event.event_id, mode, self._symbols, self._source.total_loaded_rows,
            )

    # ─────────────────────────────────────────────────────────────────
    # step — one 1-minute cycle
    # ─────────────────────────────────────────────────────────────────
    async def step(self, sim_clock: datetime) -> ChannelSignal | None:
        """Trades from this minute → synthetic snapshot → features → detector → signal.

        With multiple coins (e.g. BTC, ETH), keep the signal with the highest
        tier. Break ties by score.
        """
        if not self._symbols:
            return None

        bar_end = sim_clock + timedelta(minutes=1)

        candidates: list[ChannelSignal] = []
        for coin in self._symbols:
            signal = self._step_one_coin(coin, sim_clock, bar_end)
            if signal is not None:
                candidates.append(signal)

        if not candidates:
            return None

        winner = max(candidates, key=lambda s: (s.tier.rank(), s.score))
        return winner

    # ─────────────────────────────────────────────────────────────────
    # close
    # ─────────────────────────────────────────────────────────────────
    async def close(self) -> None:
        await self._source.close()

    # ─────────────────────────────────────────────────────────────────
    # Internal — process 1 minute for 1 coin
    # ─────────────────────────────────────────────────────────────────
    def _step_one_coin(
        self,
        coin: str,
        sim_clock: datetime,
        bar_end: datetime,
    ) -> ChannelSignal | None:
        state = self._states.setdefault(coin, _CoinReplayState())

        bar = self._source.get_bar(coin, sim_clock)
        trades = bar.payload.get("trades", []) if bar is not None else []

        # ── 1) Accumulate this minute's trades into state + append to recent_trades ──
        # Trades are guaranteed to be sorted by ts ascending.
        trades_sorted = sorted(trades, key=lambda t: t["ts"])
        for t in trades_sorted:
            ntl = float(t.get("ntl", 0.0))
            sz = float(t.get("sz", 0.0))
            px = float(t.get("px", 0.0))
            direction = t.get("dir", "")

            state.cum_notional_usd += ntl
            state.net_short_coins += _dir_to_oi_sign(direction) * sz
            state.last_mark_px = px  # last trade's px

            side_code = _dir_to_side_code(direction)
            state.recent_trades.append((t["ts"], ntl, side_code))

        # ── 2) One synthetic NormalizedEvent (snapshot as of bar_end) ──
        # Features are snapshot-based, so a single event per minute is enough.
        if state.last_mark_px > 0.0:
            synth = NormalizedEvent(
                channel=CHANNEL_HYPERLIQUID,
                symbol=coin,
                ts_source=bar_end,
                ts_ingest=bar_end,
                side=None,
                size_usd=0.0,
                price=state.last_mark_px,
                meta={
                    "day_ntl_vlm_usd": state.cum_notional_usd,
                    "mark_px": state.last_mark_px,
                    "open_interest_coins": state.net_short_coins,
                    "funding_rate": None,  # not in CSV
                },
                raw_ref=f"hl_replay:{coin}:{bar_end.isoformat()}",
            )
            self._features.add_events([synth])

        # ── 3) features.compute_snapshot ──
        snapshot = self._features.compute_snapshot(coin, bar_end)
        if snapshot is None:
            return None

        # ── 4) Inject wallet features directly ──
        self._inject_wallet_features(snapshot, state, bar_end)

        # ── 5) detector.evaluate ──
        return self._detector.evaluate(snapshot)

    # ─────────────────────────────────────────────────────────────────
    # Direct wallet feature injection (replay-only simplified form)
    # ─────────────────────────────────────────────────────────────────
    def _inject_wallet_features(
        self,
        snapshot,
        state: _CoinReplayState,
        now: datetime,
    ) -> None:
        """Reproduce production channel.py's _inject_wallet_features in a simplified form.

        CSV = single wallet, so:
          · new_whale_max_cum5m_usd = sum of ntl in the last 5 minutes
          · new_whale_count_24h     = 1 if cum5m > 0 else 0
          · new_whale_warmup_ready  = 1.0 (no cold-start in replay)
          · new_whale_last_side_code = side code of the most recent trade
          · cluster_*               = 0 (single wallet → no cluster forms)
        """
        feats = snapshot.features  # mutable dict

        # Pop anything in recent_trades outside the 5-minute window.
        cutoff = now - self._new_whale_window
        while state.recent_trades and state.recent_trades[0][0] < cutoff:
            state.recent_trades.popleft()

        cum5m = sum(ntl for (_, ntl, _) in state.recent_trades)
        if state.recent_trades:
            last_side = state.recent_trades[-1][2]
        else:
            last_side = 0.0

        feats["new_whale_max_cum5m_usd"] = float(cum5m)
        feats["new_whale_count_24h"] = 1.0 if cum5m > 0 else 0.0
        feats["new_whale_warmup_ready"] = 1.0
        feats["new_whale_last_side_code"] = float(last_side)

        # cluster — single wallet means always 0
        feats["cluster_top_sum_notional_usd"] = 0.0
        feats["cluster_top_n_wallets"] = 0.0
        feats["cluster_warmup_ready"] = 1.0
        feats["cluster_top_side_code"] = 0.0

    def __repr__(self) -> str:
        return (
            f"<HyperliquidChannelReplay event={self._event_id} "
            f"symbols={self._symbols}>"
        )


__all__ = ["HyperliquidChannelReplay"]
