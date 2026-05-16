"""
channel_replays/polymarket.py — PolymarketChannelReplay (source + features + detector).

────────────────────────────────────────────────────────────────────────
Role:
  Polymarket implementation of the runner's per-channel step(sim_clock) → ChannelSignal.
  Mirrors the CME pattern (same structure as channel_replays/cme.py).

  Internal data flow (1 cycle = 1 minute):

    1. source.get_bar(symbol, sim_clock) → BarTick (trades list)
    2. trades → [NormalizedEvent, ...] (uses size_usd as-is)
    3. features.add_events(...) — push to rolling buffer (CUSUM/baseline update too)
    4. snapshot = features.compute_snapshot(symbol, now=bar_end)
    5. signal = detector.evaluate(snapshot)
    6. For multi-slug events, keep the single max-tier signal.

────────────────────────────────────────────────────────────────────────
Design decisions:

  · features / detector are imported directly from production code.
    P9.1's 4 detector pool (vol_burst_v2/odds_gap_v2/odds_cusum_v1/directional_v1)
    flows straight into replay validation.

  · baseline_store (M1 time-of-day SQLite) is None in v0 — the live daemon's
    production SQLite lives elsewhere, so replay uses only the absolute-USD
    fallback (vol_burst_abs_v1).
    → Independent of the first 30-minute accumulation (absolute USD threshold,
      no baseline needed). But for historical events with a single-wallet
      one-shot pattern (e.g. Iran 5/6), single_wallet_burst_v1 also fires.
    (HistoricalEvent.window_start = announcement - max(pre_event_window, 60min))

  · Side mapping (Dune source → our Side enum):
      - Polymarket trade rows only carry "buy"/"sell" (BUY/SELL per outcome).
      - Dune-normalized 'buy' → Side.BUY, 'sell' → Side.SELL.
      - PolymarketFeatures._classify_side unifies BUY/YES/LONG → "buy".
      - The exact outcome (YES/NO) is preserved as-is in meta['outcome'] (audit).

  · meta['mid_price'] is None — Dune SQL results don't include mid.
    → has_mid_price=0 → odds_gap_v2 uses the v1 fallback (price_jump_v1).

  · step() for multi-slug events:
      Run the detector for every slug and keep the signal with the highest tier.
      Same pattern as production's channel.py (identical to CmeChannelReplay).

────────────────────────────────────────────────────────────────────────
References:
  · src/anomaly/channels/polymarket/features.py  — PolymarketFeatures
  · src/anomaly/channels/polymarket/detector.py  — PolymarketDetector
  · src/anomaly/replay/data_sources/polymarket_dune.py — Dune source
  · src/anomaly/replay/channel_replays/cme.py    — parallel pattern (reference)
"""

from __future__ import annotations

# --- standard library ---
import logging
from datetime import datetime, timedelta
from typing import ClassVar

# --- local: production Polymarket pipeline ---
from ...channels.polymarket.detector import (
    PolymarketDetector,
    PolymarketDetectorConfig,
)
from ...channels.polymarket.features import PolymarketFeatures
from ...core.schemas import (
    CHANNEL_POLYMARKET,
    ChannelSignal,
    NormalizedEvent,
    Side,
    Tier,  # noqa: F401  — re-exported for caller convenience
)

# --- local: replay layer ---
# v0.1: backend = Polymarket public Data API (free, no key).
# The older Dune backend (polymarket_dune.py) has the same interface, so any
# type-compatible source works.
from ..data_sources.polymarket_data_api import PolymarketDataApiSource
from ..schemas import HistoricalEvent

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# PolymarketChannelReplay
# ─────────────────────────────────────────────────────────────────────
class PolymarketChannelReplay:
    """Polymarket channel's replay-side wrapper. Satisfies the ChannelReplay Protocol.

    Args:
        source: warmed historical source (default = PolymarketDataApiSource).
            Other backends are fine as long as they share the interface
            (legacy: PolymarketDuneSource).
        detector_config: PolymarketDetectorConfig override (None=production default).
        features_kwargs: PolymarketFeatures __init__ override (None=production default).
    """

    channel: ClassVar[str] = CHANNEL_POLYMARKET

    def __init__(
        self,
        *,
        source: PolymarketDataApiSource | None = None,
        detector_config: PolymarketDetectorConfig | None = None,
        features_kwargs: dict | None = None,
    ) -> None:
        # data layer.
        self._source = source or PolymarketDataApiSource()
        # production feature engine — multi-symbol is separated internally.
        self._features = PolymarketFeatures(**(features_kwargs or {}))
        # production detector.
        self._detector = PolymarketDetector(config=detector_config)

        # warmup() will populate — the slugs this event covers.
        self._symbols: list[str] = []
        # For logging
        self._event_id: str | None = None

    # ─────────────────────────────────────────────────────────────────
    # warmup — source warmup + extract this event's slugs.
    # ─────────────────────────────────────────────────────────────────
    async def warmup(self, event: HistoricalEvent) -> None:
        """Source warmup → pre-fetch trades. Features fill naturally during step().

        Active slug selection:
          1) Polymarket is the primary channel: all event.primary_symbols (slugs).
          2) Otherwise (including secondary) → not activated — Polymarket secondary
             monitoring is unsupported in v0 ("monitor everything" doesn't fit
             since slugs are specific markets).

        Only slugs the source actually warmed up (warmed_symbols) are kept.
        """
        self._event_id = event.event_id

        await self._source.warmup(event)

        warmed = self._source.warmed_symbols
        if event.primary_channel == CHANNEL_POLYMARKET:
            candidates = list(event.primary_symbols)
            mode = "primary"
        else:
            # secondary not supported in v0.
            candidates = []
            mode = "inactive"

        self._symbols = [s for s in candidates if s in warmed]

        if not self._symbols:
            logger.warning(
                "PolymarketChannelReplay: no active slugs for event=%s "
                "(mode=%s, candidates=%s, warmed=%s) — step() will return None",
                event.event_id, mode, candidates, sorted(warmed),
            )
        else:
            logger.info(
                "PolymarketChannelReplay: warmed event=%s mode=%s active_symbols=%s",
                event.event_id, mode, self._symbols,
            )

    # ─────────────────────────────────────────────────────────────────
    # step — one 1-minute cycle.
    # ─────────────────────────────────────────────────────────────────
    async def step(self, sim_clock: datetime) -> ChannelSignal | None:
        """Process one minute of trades and return one ChannelSignal (or None).

        For multi-slug, take the signal of the slug with the highest tier.

        Returns:
            ChannelSignal: detector fired (NORMAL included — the runner also emits NORMAL).
            None: no active slugs, or features never produced a snapshot (extreme edge case).
        """
        if not self._symbols:
            return None

        bar_end = sim_clock + timedelta(minutes=1)

        # 1) Each slug's bar this minute → NormalizedEvent stream → push into features.
        for slug in self._symbols:
            bar = self._source.get_bar(slug, sim_clock)
            if bar is None:
                continue
            normalized = self._bar_to_normalized(slug, bar.payload.get("trades", []))
            if normalized:
                self._features.add_events(normalized)

        # 2) Per slug snapshot → evaluate detector, keep the max-tier signal.
        candidates: list[ChannelSignal] = []
        for slug in self._symbols:
            snapshot = self._features.compute_snapshot(symbol=slug, now=bar_end)
            if snapshot is None:
                # Buffer empty (no trades ever) — skip.
                continue
            signal = self._detector.evaluate(snapshot)
            candidates.append(signal)

        if not candidates:
            return None

        # max-tier; break ties by score.
        winner = max(candidates, key=lambda s: (s.tier.rank(), s.score))
        return winner

    # ─────────────────────────────────────────────────────────────────
    # close.
    # ─────────────────────────────────────────────────────────────────
    async def close(self) -> None:
        await self._source.close()

    # ─────────────────────────────────────────────────────────────────
    # Internal — Dune trade dict → NormalizedEvent.
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _side_from_str(raw: str) -> Side:
        """'buy' → Side.BUY, 'sell' → Side.SELL, otherwise → Side.BUY (default)."""
        s = raw.strip().lower()
        if s.startswith("s"):
            return Side.SELL
        # 'buy', 'b', 'BUY', 'long' etc. → BUY.
        return Side.BUY

    def _bar_to_normalized(
        self,
        slug: str,
        trades: list[dict],
    ) -> list[NormalizedEvent]:
        """Convert this minute's trade dict list into a NormalizedEvent list.

        Polymarket trade row size_usd is already USD-converted by the Dune SQL.
        price is the outcome's implied probability (0..1).
        """
        out: list[NormalizedEvent] = []
        for t in trades:
            price = float(t.get("price", 0.0))
            size_usd = float(t.get("size_usd", 0.0))
            if price <= 0.0 or price > 1.0 or size_usd <= 0.0:
                # Bad trade (price out of range, 0 size) — skip.
                continue

            ts_event = t["ts"]
            outcome = t.get("outcome", "")
            trader = t.get("trader", "")
            tx_hash = t.get("tx_hash", "")
            if tx_hash:
                raw_ref = f"polymarket_replay:{tx_hash}"
            else:
                raw_ref = f"polymarket_replay:{ts_event.isoformat()}:{slug}"

            out.append(NormalizedEvent(
                channel=CHANNEL_POLYMARKET,
                symbol=slug,
                ts_source=ts_event,
                ts_ingest=ts_event,
                side=self._side_from_str(t.get("side", "buy")),
                size_usd=size_usd,
                price=price,
                meta={
                    "outcome": outcome,
                    # Same key as the production normalizer. PolymarketFeatures
                    # uses it for wallet concentration.
                    "proxy_wallet": trader,
                    # Compat alias — keep so old reports/CSVs looking at 'trader' still work.
                    "trader": trader,
                    "tx_hash": tx_hash,
                    "source_label": "polymarket_data_api",
                },
                raw_ref=raw_ref,
            ))
        return out

    def __repr__(self) -> str:
        return (
            f"<PolymarketChannelReplay event={self._event_id} "
            f"symbols={self._symbols}>"
        )


__all__ = ["PolymarketChannelReplay"]
