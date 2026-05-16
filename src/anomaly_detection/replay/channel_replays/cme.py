"""
channel_replays/cme.py — CmeChannelReplay: bundles source + features + detector.

────────────────────────────────────────────────────────────────────────
Role:
  CME implementation of the runner's per-channel step(sim_clock) → ChannelSignal.

  Internal data flow (1 cycle = 1 minute):

    1. source.get_bar(symbol, sim_clock) → BarTick (trades list)
    2. trades → [NormalizedEvent, ...] (computes size_usd)
    3. features.add_events(...) — push into the rolling buffer
    4. snapshot = features.compute_snapshot(symbol, now=bar_end)
    5. signal = detector.evaluate(snapshot)
    6. For multi-symbol events (e.g. ES + BZ), keep the max-tier signal

────────────────────────────────────────────────────────────────────────
Design decisions:

  · features / detector are imported directly from production code.
    P9 calibration / threshold tuning results immediately flow into replay validation.

  · CMEFeatures separates symbols internally via dict → one instance is enough.

  · sim_clock convention:
      sim_clock     = "start of this 1-min bar"
      bar_end       = sim_clock + 60s
      compute_snapshot(now=bar_end) — "feature state at the end of this minute"
    → ChannelSignal.ts = bar_end. Differs from the runner's sim_clock by 60s,
      but both fusion / state_manager handle everything in sim_clock terms
      (signal.ts is audit metadata).

  · Side mapping (Databento → our schema):
      'B' (Bid hit, buyer aggressor)  → Side.BUY
      'A' (Ask hit, seller aggressor) → Side.SELL
      'N' (unknown)                   → Side.BUY (default; CMEFeatures doesn't use side)

  · step() for multi-symbol events:
      Run the detector on every symbol and keep the signal with the highest tier.
      Same pattern as production's channel.py (tier=Tier.max_of).

────────────────────────────────────────────────────────────────────────
References:
  · src/anomaly/channels/cme/features.py     — CMEFeatures
  · src/anomaly/channels/cme/detector.py     — CMEDetector / CMEDetectorConfig
  · src/anomaly/channels/cme/tradingview_adapter.py CME_CONTRACT_MULT
  · docs/p10-replay-framework.md §3 / §7 #3
"""

from __future__ import annotations

# --- standard library ---
import logging
from datetime import datetime, timedelta
from typing import ClassVar

# --- local: production CME pipeline ---
from ...channels.cme.detector import CMEDetector, CMEDetectorConfig
from ...channels.cme.features import CMEFeatures
from ...channels.cme.tradingview_adapter import CME_CONTRACT_MULT
from ...core.schemas import (
    CHANNEL_CME,
    ChannelSignal,
    Direction,  # noqa: F401  — re-exported for symmetry / future use
    NormalizedEvent,
    Side,
    Tier,  # noqa: F401  — used by callers via channels API
)

# --- local: replay layer ---
from ..schemas import HistoricalEvent
from ..data_sources.cme_databento import CmeDatabentoSource

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# CmeChannelReplay
# ─────────────────────────────────────────────────────────────────────
class CmeChannelReplay:
    """CME channel's replay-side wrapper. Satisfies the ChannelReplay Protocol.

    Args:
        source: warmed CmeDatabentoSource. If not injected, a new one is created.
        detector_config: CMEDetectorConfig override (None=production default).
        features_kwargs: CMEFeatures __init__ override (None=production default).
    """

    channel: ClassVar[str] = CHANNEL_CME

    def __init__(
        self,
        *,
        source: CmeDatabentoSource | None = None,
        detector_config: CMEDetectorConfig | None = None,
        features_kwargs: dict | None = None,
    ) -> None:
        # data layer.
        self._source = source or CmeDatabentoSource()
        # production feature engine — multi-symbol is separated internally.
        self._features = CMEFeatures(**(features_kwargs or {}))
        # production detector.
        self._detector = CMEDetector(config=detector_config)

        # warmup() will populate — the CME roots this event covers.
        self._symbols: list[str] = []
        # For logging
        self._event_id: str | None = None

    # ─────────────────────────────────────────────────────────────────
    # warmup — source warmup + extract this event's CME roots.
    # ─────────────────────────────────────────────────────────────────
    async def warmup(self, event: HistoricalEvent) -> None:
        """Source warmup → pre-fetch trades. Features fill naturally during step().

        Matches production semantics: the features' baseline accumulates "live".
        The replay window_start must be far enough (≥35 min) before announcement
        so baseline_ready=True and the detector starts firing.

        Active symbol selection:
          1) CME is the primary channel:  CME roots from event.primary_symbols only.
             (e.g. liberation_day → ['BZ'])
          2) CME is a secondary channel: auto-activate all known CME roots.
             (e.g. china_tariff_100 — primary=hyperliquid → CME is used to verify
              spillover on ES/CL/BZ/GC. Since we don't know which contract will
              spike, watch all of them.)
          3) Otherwise active_symbols is empty → always return None.

        Only roots the source actually warmed up (warmed_symbols) are kept.
        """
        self._event_id = event.event_id

        await self._source.warmup(event)

        # After warmup, only roots the source actually has data for become active.
        warmed = self._source.warmed_symbols
        # Candidate selection — depends on primary vs. secondary.
        if event.primary_channel == CHANNEL_CME:
            # primary: CME roots in the listed primary_symbols.
            candidates = [
                s for s in event.primary_symbols if s in CME_CONTRACT_MULT
            ]
            mode = "primary"
        elif CHANNEL_CME in event.secondary_channels:
            # secondary: spillover verification — all CME roots.
            candidates = list(CME_CONTRACT_MULT.keys())
            mode = "secondary"
        else:
            # Event unrelated to CME — no active symbols.
            candidates = []
            mode = "inactive"

        # Only roots the source actually received data for are active.
        self._symbols = [s for s in candidates if s in warmed]

        if not self._symbols:
            logger.warning(
                "CmeChannelReplay: no active CME symbols for event=%s "
                "(mode=%s, candidates=%s, warmed=%s) — step() will return None",
                event.event_id, mode, candidates, sorted(warmed),
            )
        else:
            logger.info(
                "CmeChannelReplay: warmed event=%s mode=%s active_symbols=%s",
                event.event_id, mode, self._symbols,
            )

    # ─────────────────────────────────────────────────────────────────
    # step — one 1-minute cycle.
    # ─────────────────────────────────────────────────────────────────
    async def step(self, sim_clock: datetime) -> ChannelSignal | None:
        """Process one minute of trades and return one ChannelSignal (or None).

        For multi-symbol, take the signal of the symbol with the highest tier.

        Returns:
            ChannelSignal: detector fired (including NORMAL — the runner also
                emits NORMAL).
            None: no active symbols, or features never produced a snapshot
                (extreme edge case).
        """
        if not self._symbols:
            return None

        bar_end = sim_clock + timedelta(minutes=1)

        # 1) Each symbol's bar this minute → NormalizedEvent stream → push into features.
        for symbol in self._symbols:
            bar = self._source.get_bar(symbol, sim_clock)
            if bar is None:
                continue

            normalized = self._bar_to_normalized(symbol, bar.payload.get("trades", []))
            if normalized:
                self._features.add_events(normalized)

        # 2) Per symbol snapshot → evaluate detector, keep the max-tier signal.
        candidates: list[ChannelSignal] = []
        for symbol in self._symbols:
            snapshot = self._features.compute_snapshot(symbol=symbol, now=bar_end)
            if snapshot is None:
                # Buffer empty (market closed or zero trades accumulated) — skip.
                continue
            signal = self._detector.evaluate(snapshot)
            candidates.append(signal)

        if not candidates:
            return None

        # One max-tier signal. Break ties by score.
        winner = max(candidates, key=lambda s: (s.tier.rank(), s.score))
        return winner

    # ─────────────────────────────────────────────────────────────────
    # close.
    # ─────────────────────────────────────────────────────────────────
    async def close(self) -> None:
        await self._source.close()

    # ─────────────────────────────────────────────────────────────────
    # Internal — Databento trade dict → NormalizedEvent.
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _databento_side_to_side(raw: str) -> Side:
        """'A'=ask hit→SELL, 'B'=bid hit→BUY, 'N'/other=BUY default."""
        if raw == "A":
            return Side.SELL
        if raw == "B":
            return Side.BUY
        # 'N' or unknown — features doesn't use side, so default to BUY.
        return Side.BUY

    def _bar_to_normalized(
        self,
        symbol: str,
        trades: list[dict],
    ) -> list[NormalizedEvent]:
        """Convert this minute's trade dict list into a NormalizedEvent list.

        size_usd = size_contracts × price × CME_CONTRACT_MULT[symbol].
        """
        mult = CME_CONTRACT_MULT.get(symbol)
        if mult is None:
            # Only handle known roots. (Already filtered in warmup but kept as a defensive guard.)
            return []

        out: list[NormalizedEvent] = []
        for t in trades:
            size_contracts = float(t.get("size", 0))
            price = float(t.get("price", 0.0))
            if size_contracts <= 0 or price <= 0:
                continue  # Bad trade — skip.
            size_usd = size_contracts * price * mult

            ts_event = t["ts"]
            # raw_ref is the audit key linking to a RawEvent.id. Replay doesn't
            # produce a RawEvent, so build a synthetic ID for tracing —
            # "databento:{ts_iso}:{raw_symbol}".
            raw_ref = f"databento:{ts_event.isoformat()}:{t.get('symbol', symbol)}"
            out.append(NormalizedEvent(
                channel=CHANNEL_CME,
                symbol=symbol,
                ts_source=ts_event,
                # In replay, ingest=source — CMEFeatures only uses ts_source.
                ts_ingest=ts_event,
                side=self._databento_side_to_side(t.get("side", "N")),
                size_usd=size_usd,
                price=price,
                meta={
                    "size_contracts": size_contracts,
                    "is_mock_spike": False,  # real Databento data
                    "source_label": "databento_replay",
                    "raw_symbol": t.get("symbol", symbol),  # contract code (e.g. "ESM5")
                },
                raw_ref=raw_ref,
            ))
        return out

    def __repr__(self) -> str:
        return (
            f"<CmeChannelReplay event={self._event_id} "
            f"symbols={self._symbols}>"
        )


__all__ = ["CmeChannelReplay"]
