"""
cme/mock_collector.py — CME synthetic data generator (P4 walking-skeleton).

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §3 walking-skeleton, P4 decision):

  Real Databento / TradingView / UnusualWhales integration lands in P9 deep-dive.
  The P4 stage is **about verifying the entire alert pipeline using a mock collector**.

  Each cycle, emit 1~3 fake trades per watchlist symbol (CL, GC, ES, etc.).
  Normally: small random volume + random walk price → detector returns NORMAL.
  Spike mode (only when env CME_MOCK_SPIKE=true): every ~5 minutes, one cycle's
  volume × N spike → detector EMERGENCY → the entire alert pipeline (router →
  throttle → dedupe → digest → email/telegram dry-run) fires once.

  → Verifies the final piece of the walking-skeleton (end-to-end alerts).

────────────────────────────────────────────────────────────────────────
Design decisions:

  · Same "thin client + external-call abstraction" pattern as Polymarket / Hyperliquid.
    Only there are no real external calls (substituted with random.*).

  · When the real CME collector (Databento/TradingView/UW composite) lands in P9,
    leave this as a dev-only fallback or move MockCMECollector to its own folder.

  · Spike turns on only via env flag. Cloud Run prod stays quiet (no flag).
    When spike fires, "MOCK_SPIKE" is stamped in reason_codes to distinguish from real alerts.

  · Keep the "trade" payload format close to the future real Databento schema,
    so in P9 we can swap only the collector and reuse the normalizer almost as-is.

────────────────────────────────────────────────────────────────────────
Env vars:

  CME_MOCK_SPIKE      "true" | "false"  default false
                      true → inject periodic volume spikes
  CME_MOCK_SPIKE_EVERY_S  int (default 300)
                      Spike interval (seconds). Default 5 min.
  CME_MOCK_SPIKE_MULT     float (default 50.0)
                      Spike multiplier over normal volume.
  CME_MOCK_SEED       int (optional) — reproducible random seed.

────────────────────────────────────────────────────────────────────────
Plan: §3.1 Goal #3 (CME channel — mock for walking-skeleton)
"""

from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# Representative CME futures starting prices (for mock). The z-score detector
# only cares about deviation, not absolute price, so these need not match reality.
_DEFAULT_PRICES: dict[str, float] = {
    "CL": 78.0,    # WTI Crude Oil ($/barrel)
    "GC": 2400.0,  # Gold ($/oz)
    "ES": 5400.0,  # E-mini S&P 500 (index)
    "NQ": 19000.0,
    "ZB": 115.0,
    "6E": 1.08,
}

# Approximate USD notional per CME contract (mock; not exact).
# size_usd = contract_count × _CONTRACT_SIZE_USD[symbol]
_CONTRACT_SIZE_USD: dict[str, float] = {
    "CL": 78_000.0,    # 1000 barrels × $78
    "GC": 240_000.0,   # 100 oz × $2400
    "ES": 270_000.0,   # $50 multiplier × 5400
    "NQ": 380_000.0,
    "ZB": 115_000.0,
    "6E": 135_000.0,
}


@dataclass
class _SymbolState:
    """Mock state for one symbol."""
    last_price: float
    last_spike_ts: float | None = None  # event-loop time at last spike


class MockCMECollector:
    """CME mock data generator (P4 walking-skeleton).

    Differences from Polymarket / Hyperliquid collectors:
      - No external HTTP calls (open/close are idempotent no-ops).
      - generate_trades() is called every cycle → returns fake trades.
    """

    def __init__(
        self,
        *,
        symbols: list[str],
        seed: int | None = None,
    ) -> None:
        """
        Args:
            symbols: CME symbols from the watchlist.
            seed: random seed (reproducible). None → env CME_MOCK_SEED or OS time.
        """
        if not symbols:
            logger.warning("MockCMECollector: empty symbols list")

        self._symbols = list(symbols)

        # Read env vars
        self._spike_enabled = _env_bool("CME_MOCK_SPIKE", default=False)
        self._spike_every_s = _env_int("CME_MOCK_SPIKE_EVERY_S", default=300)
        self._spike_mult = _env_float("CME_MOCK_SPIKE_MULT", default=50.0)

        # Seed selection priority: arg > env > None (time-based)
        if seed is None:
            env_seed = os.getenv("CME_MOCK_SEED")
            if env_seed:
                try:
                    seed = int(env_seed)
                except ValueError:
                    seed = None
        self._rng = random.Random(seed)

        # Per-symbol state — stores the last price for the random walk
        self._states: dict[str, _SymbolState] = {
            sym: _SymbolState(last_price=_DEFAULT_PRICES.get(sym, 100.0))
            for sym in self._symbols
        }

        # Open/closed flag — keeps the channel interface consistent
        self._is_open = False

        logger.info(
            "MockCMECollector: initialized symbols=%s spike_enabled=%s "
            "spike_every_s=%d spike_mult=%.1f",
            self._symbols, self._spike_enabled, self._spike_every_s, self._spike_mult,
        )

    # ─────────────────────────────────────────────────────────────────
    # Lifecycle (no-op for mock — interface consistency)
    # ─────────────────────────────────────────────────────────────────
    async def open(self) -> None:
        self._is_open = True

    async def close(self) -> None:
        self._is_open = False

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def spike_enabled(self) -> bool:
        return self._spike_enabled

    # ─────────────────────────────────────────────────────────────────
    # Public API — called every cycle
    # ─────────────────────────────────────────────────────────────────
    def generate_trades(
        self,
        symbol: str,
        *,
        now_loop_time: float,
    ) -> list[dict[str, Any]]:
        """Generate mock trades for one symbol.

        Args:
            symbol: CME symbol (e.g. "CL") that exists in the watchlist.
            now_loop_time: asyncio event loop time (monotonic; used for spike timing).

        Returns:
            list[dict]: trade payloads. Each dict is the format the normalizer expects:
                {
                  "symbol": "CL",
                  "timestamp": <unix_seconds:int>,
                  "price": <float>,
                  "size_contracts": <float>,
                  "size_usd": <float>,
                  "side": "BUY" | "SELL",
                  "is_mock_spike": bool,           # True when a spike fires
                  "source": "mock_v1",
                }
        """
        if symbol not in self._states:
            return []

        state = self._states[symbol]
        unix_ts = int(datetime.now(timezone.utc).timestamp())

        # 1) Price random walk — usually 0.05% stdev (per cycle)
        drift_pct = self._rng.gauss(0.0, 0.0005)
        new_price = max(0.01, state.last_price * (1.0 + drift_pct))
        state.last_price = new_price

        # 2) Spike decision — env flag + spike_every_s elapsed since the last spike
        do_spike = self._should_spike(state, now_loop_time)

        # 3) Decide trade count + size
        if do_spike:
            n_trades = self._rng.randint(2, 4)        # spike: multiple trades
            base_contracts = self._rng.uniform(8.0, 25.0)
            volume_mult = self._spike_mult
            # During spike also bump the price slightly (0.3% ~ 0.8%)
            jump_pct = self._rng.uniform(0.003, 0.008) * self._rng.choice([-1.0, 1.0])
            new_price = max(0.01, state.last_price * (1.0 + jump_pct))
            state.last_price = new_price
            state.last_spike_ts = now_loop_time
            logger.info(
                "MockCMECollector: SPIKE injected symbol=%s mult=%.0fx "
                "new_price=%.4f",
                symbol, volume_mult, new_price,
            )
        else:
            n_trades = self._rng.randint(0, 2)        # 0~2 normal trades
            base_contracts = self._rng.uniform(1.0, 8.0)
            volume_mult = 1.0

        if n_trades == 0:
            return []

        contract_size_usd = _CONTRACT_SIZE_USD.get(symbol, 100_000.0)
        trades: list[dict[str, Any]] = []
        for _ in range(n_trades):
            size_contracts = max(0.5, base_contracts * volume_mult * self._rng.uniform(0.7, 1.3))
            size_usd = size_contracts * contract_size_usd
            side = "BUY" if self._rng.random() < 0.5 else "SELL"
            trades.append({
                "symbol": symbol,
                "timestamp": unix_ts,
                "price": new_price,
                "size_contracts": size_contracts,
                "size_usd": size_usd,
                "side": side,
                "is_mock_spike": do_spike,
                "source": "mock_v1",
            })

        return trades

    # ─────────────────────────────────────────────────────────────────
    # Internal — spike timing
    # ─────────────────────────────────────────────────────────────────
    def _should_spike(self, state: _SymbolState, now_loop_time: float) -> bool:
        """Spike condition: env enabled + (never spiked, or spike_every_s elapsed)."""
        if not self._spike_enabled:
            return False
        if state.last_spike_ts is None:
            # Don't spike immediately on first run — wait ~70% of spike_every_s to accumulate baseline.
            # event-loop time starts near 0, so this works as-is.
            return now_loop_time >= self._spike_every_s * 0.7
        return (now_loop_time - state.last_spike_ts) >= self._spike_every_s


# =====================================================================
# env var helpers (internal to this module)
# =====================================================================
def _env_bool(name: str, *, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, *, default: int) -> int:
    v = os.getenv(name)
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        logger.warning("MockCMECollector: invalid env %s=%r, using default %d",
                       name, v, default)
        return default


def _env_float(name: str, *, default: float) -> float:
    v = os.getenv(name)
    if not v:
        return default
    try:
        return float(v)
    except ValueError:
        logger.warning("MockCMECollector: invalid env %s=%r, using default %.2f",
                       name, v, default)
        return default
