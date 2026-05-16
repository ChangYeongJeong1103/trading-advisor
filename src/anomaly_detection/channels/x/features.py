"""
x/features.py — Per-symbol rolling aggregation of X mentions.

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §5 pipeline step 3):

  Accumulate the NormalizedEvents emitted by parse_post() into per-symbol
  rolling 15-min windows. On detector evaluate(), provide:

    - n_unique_accounts_15min      : unique accounts that mentioned the symbol
                                     (1 → WATCH, 2+ → RISK_OFF, 3+ → just below EMER)
    - sum_account_weight_15min     : sum of credibility weights (big-meaning weighting)
    - magnitude_count_15min        : count of posts containing a magnitude phrase
    - direction_buy_count_15min    : count of mentions labeled Side.BUY
    - direction_sell_count_15min   : count of mentions labeled Side.SELL
    - mock_spike_count_15min       : for audit

  Unlike other channels, the X channel does not benefit from a 30-min baseline
  (posts are not a continuous numeric stream). So baseline_ready is always 1.0.
  Caveat: "at least 1 mention" is required for the detector to reach a tier
  other than NORMAL.

────────────────────────────────────────────────────────────────────────
Design decisions:

  · Per-symbol deque<_Mention>. Prune every cycle.

  · "Unique accounts" counts as 1 even if the same account mentions the symbol
    multiple times (spam guard).

  · direction_*_count is used by the detector for "same-direction agreement" (plan §8 P5).

────────────────────────────────────────────────────────────────────────
Plan: §8 P5 (v0 detector inputs), §5 pipeline FeatureSnapshot
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Deque

from ...core.schemas import CHANNEL_X, FeatureSnapshot, NormalizedEvent, Side
from .credibility import account_weight

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Mention:
    ts: datetime
    user: str
    weight: float
    has_magnitude: bool
    side: Side | None
    is_mock_spike: bool


@dataclass
class _SymbolState:
    buf: Deque[_Mention] = field(default_factory=deque)


class XFeatures:
    """X per-symbol mention aggregation engine."""

    def __init__(
        self,
        *,
        window_s: int = 900,           # 15 min — plan §8 P5 rule (corroboration within 15 min)
        max_history_s: int | None = None,
    ) -> None:
        if window_s < 60:
            raise ValueError("window_s too small (>=60s)")

        self._window_s = window_s
        self._max_history_s = max_history_s or (window_s + 60)
        self._states: dict[str, _SymbolState] = {}
        self._baseline_ref = f"x:rolling:win{window_s}s:v1"

    # ─────────────────────────────────────────────────────────────────
    def add_events(self, events: Iterable[NormalizedEvent]) -> None:
        """Push NormalizedEvents into per-symbol buffers."""
        for ev in events:
            if ev.channel != CHANNEL_X:
                continue
            user = str(ev.meta.get("user", ""))
            weight = account_weight(user)
            if weight <= 0.0:
                # Unknown account → silent skip (noise filter)
                continue
            has_mag = bool(ev.meta.get("has_magnitude", False))
            is_spike = bool(ev.meta.get("is_mock_spike", False))
            state = self._states.setdefault(ev.symbol, _SymbolState())
            state.buf.append(_Mention(
                ts=ev.ts_source,
                user=user,
                weight=weight,
                has_magnitude=has_mag,
                side=ev.side,
                is_mock_spike=is_spike,
            ))

    # ─────────────────────────────────────────────────────────────────
    def compute_snapshot(
        self,
        symbol: str,
        now: datetime,
    ) -> FeatureSnapshot | None:
        """Current FeatureSnapshot for symbol. None if buffer empty."""
        state = self._states.get(symbol)
        if state is None or not state.buf:
            return None

        self._prune(state, now)
        if not state.buf:
            return None

        cutoff = now - timedelta(seconds=self._window_s)
        recent = [m for m in state.buf if m.ts >= cutoff]
        if not recent:
            return None

        unique_users: set[str] = set()
        sum_w = 0.0
        mag_count = 0
        buy_count = 0
        sell_count = 0
        spike_count = 0

        # unique accounts: per-user max weight (one account spamming counts once)
        per_user_weight: dict[str, float] = {}
        for m in recent:
            if m.has_magnitude:
                mag_count += 1
            if m.side == Side.BUY:
                buy_count += 1
            elif m.side == Side.SELL:
                sell_count += 1
            if m.is_mock_spike:
                spike_count += 1
            unique_users.add(m.user.lower())
            prev = per_user_weight.get(m.user.lower(), 0.0)
            if m.weight > prev:
                per_user_weight[m.user.lower()] = m.weight

        sum_w = sum(per_user_weight.values())

        features: dict[str, float] = {
            "n_unique_accounts_15min": float(len(unique_users)),
            "sum_account_weight_15min": float(sum_w),
            "magnitude_count_15min": float(mag_count),
            "direction_buy_count_15min": float(buy_count),
            "direction_sell_count_15min": float(sell_count),
            "mock_spike_count_15min": float(spike_count),
            "n_mentions_15min": float(len(recent)),
            "baseline_ready": 1.0,        # X has no baseline warmup concept
        }

        return FeatureSnapshot(
            channel=CHANNEL_X,
            symbol=symbol,
            ts=now,
            features=features,
            baseline_ref=self._baseline_ref,
        )

    # ─────────────────────────────────────────────────────────────────
    def _prune(self, state: _SymbolState, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self._max_history_s)
        while state.buf and state.buf[0].ts < cutoff:
            state.buf.popleft()

    # ─────────────────────────────────────────────────────────────────
    def known_symbols(self) -> list[str]:
        return list(self._states.keys())

    def buffer_size(self, symbol: str) -> int:
        s = self._states.get(symbol)
        return len(s.buf) if s else 0
