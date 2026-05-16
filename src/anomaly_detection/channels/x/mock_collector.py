"""
x/mock_collector.py — X (Twitter) synthetic data generator (P5 walking-skeleton).

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §3 walking-skeleton, P5 decision):

  Real snscrape integration arrives in EVT-1. The P5 stage validates the entire
  X channel pipeline with a mock (parser → features → detector → fusion
  corroboration boost).

  Each cycle, emit 0~2 fake posts per watchlist account.
    Normal (NORMAL):
      - 50% of cycles: no posts (an account doesn't tweet every minute)
      - Otherwise: short chitchat ("gm", "interesting", "📊 chart")
    Spike (env X_MOCK_SPIKE=true):
      - Every ~3 minutes, one cycle has multiple accounts mention the same
        symbol/direction with a magnitude keyword → 3+ accounts → EMERGENCY tier

  → The last piece to validate the fusion engine's corroboration boost
    (≥2 channels at WATCH+ in the same direction → +1 tier).

────────────────────────────────────────────────────────────────────────
Design decisions:

  · Same "thin client + external-call abstraction" pattern as the CME mock_collector.
    snscrape is dropped in during EVT-1.

  · Spike is env-flag only. Stays quiet in prod.

  · Spike-generated posts mimic real-world anomaly posts:
    "🚨 BIG WTI long $580M position opened on Hyperliquid"
    "ALERT: massive ES sweep — 6,200 contracts in 1 min"

  · Post payload format follows the snscrape output (snscrape.modules.twitter.Tweet)
    schema closely — so EVT-1 just swaps the collector and reuses the parser.

────────────────────────────────────────────────────────────────────────
Env vars:

  X_MOCK_SPIKE         "true" | "false"  default false
  X_MOCK_SPIKE_EVERY_S int (default 180) — spike interval (seconds)
  X_MOCK_SEED          int (optional) — reproducible seed

────────────────────────────────────────────────────────────────────────
Plan: §3.1 Goal #3 (X channel — mock for walking-skeleton, snscrape EVT-1)
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# Candidate symbols for mock spike mentions — macro assets from the real watchlist.
_SPIKE_SYMBOLS: list[str] = ["BTC", "ETH", "WTI", "GOLD", "ES"]

# "magnitude keywords" used in spikes — trigger the detector to escalate to EMERGENCY
_MAGNITUDE_PHRASES: list[str] = [
    "$580M position",
    "6,200 contracts in 1 min",
    "massive sweep",
    "150 accounts buying",
    "$1.2B notional",
    "12k contracts",
]

# Normal chitchat — noise without symbol mentions
_NOISE_PHRASES: list[str] = [
    "gm",
    "interesting chart 📊",
    "vol picking up",
    "watching closely",
    "this week's setup looks fun",
    "ngmi",
    "bullish on patience 🧘",
]

# Spike post template
_SPIKE_TEMPLATES: list[str] = [
    "🚨 ALERT: BIG ${sym} long {mag} opened on Hyperliquid",
    "Massive ${sym} sweep detected — {mag}",
    "Whale alert: ${sym} {mag} pumping",
    "Unusual ${sym} flow — {mag}",
    "${sym} {mag} in last 5 minutes 🚨",
]


@dataclass
class _AccountState:
    last_spike_loop_t: float | None = None


class MockXCollector:
    """X mock data generator (P5 walking-skeleton).

    Differences from Polymarket / Hyperliquid / CME collectors:
      - No external HTTP calls.
      - generate_posts() is called every cycle → returns 0~2 fake posts.
    """

    def __init__(
        self,
        *,
        accounts: list[str],
        seed: int | None = None,
    ) -> None:
        if not accounts:
            logger.warning("MockXCollector: empty accounts list")

        self._accounts = list(accounts)
        self._spike_enabled = _env_bool("X_MOCK_SPIKE", default=False)
        self._spike_every_s = _env_int("X_MOCK_SPIKE_EVERY_S", default=180)

        if seed is None:
            env_seed = os.getenv("X_MOCK_SEED")
            if env_seed:
                try:
                    seed = int(env_seed)
                except ValueError:
                    seed = None
        self._rng = random.Random(seed)

        self._states: dict[str, _AccountState] = {
            acc: _AccountState() for acc in self._accounts
        }
        # Spike means all accounts mention the same symbol at once — system-level spike timer
        self._last_global_spike_t: float | None = None
        self._is_open = False

        logger.info(
            "MockXCollector: initialized accounts=%s spike_enabled=%s spike_every_s=%d",
            self._accounts, self._spike_enabled, self._spike_every_s,
        )

    # ─────────────────────────────────────────────────────────────────
    # Lifecycle (no-op for mock)
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
    # Public API
    # ─────────────────────────────────────────────────────────────────
    async def fetch_recent_posts(self) -> list[dict[str, Any]]:
        """Unified async interface with XCollector.

        XChannel doesn't know whether this is mock or real — just await.
        Internally wraps sync `generate_posts()` (uses event-loop time).

        Returns:
            list[dict]. Each dict matches what parser/stage1_filter/llm_classifier
            expects. Same schema as the real XCollector (includes `image_urls`).
        """
        try:
            now_loop_time = asyncio.get_event_loop().time()
        except RuntimeError:
            # Fallback when there's no event loop yet
            now_loop_time = time.monotonic()
        return self.generate_posts(now_loop_time=now_loop_time)

    def generate_posts(self, *, now_loop_time: float) -> list[dict[str, Any]]:
        """Generate mock posts for every watchlist account.

        Args:
            now_loop_time: asyncio event loop time (monotonic).

        Returns:
            list[dict]: post payloads. Each dict is the parser's expected format:
                {
                  "id":        "<post_id>",
                  "user":      "@Lookonchain",
                  "text":      "<post body>",
                  "timestamp": <unix_seconds:int>,
                  "url":       "https://x.com/Lookonchain/status/...",
                  "is_mock_spike": bool,
                  "source":    "mock_v1",
                }
        """
        do_spike_now = self._should_spike(now_loop_time)

        posts: list[dict[str, Any]] = []
        unix_ts = int(datetime.now(timezone.utc).timestamp())

        if do_spike_now:
            # Every account mentions the same symbol/direction (corroboration trigger)
            sym = self._rng.choice(_SPIKE_SYMBOLS)
            mag = self._rng.choice(_MAGNITUDE_PHRASES)
            tpl = self._rng.choice(_SPIKE_TEMPLATES)
            for acc in self._accounts:
                posts.append(self._make_post(
                    user=acc,
                    text=tpl.format(sym=sym, mag=mag),
                    unix_ts=unix_ts,
                    is_mock_spike=True,
                ))
                self._states[acc].last_spike_loop_t = now_loop_time
            self._last_global_spike_t = now_loop_time
            logger.info(
                "MockXCollector: SPIKE injected — %d accounts mention $%s (%s)",
                len(self._accounts), sym, mag,
            )
            return posts

        # Normal — 50% chance of one chitchat per account
        for acc in self._accounts:
            if self._rng.random() < 0.5:
                continue
            posts.append(self._make_post(
                user=acc,
                text=self._rng.choice(_NOISE_PHRASES),
                unix_ts=unix_ts,
                is_mock_spike=False,
            ))

        return posts

    # ─────────────────────────────────────────────────────────────────
    # Internal
    # ─────────────────────────────────────────────────────────────────
    def _make_post(
        self,
        *,
        user: str,
        text: str,
        unix_ts: int,
        is_mock_spike: bool,
    ) -> dict[str, Any]:
        post_id = f"{int(unix_ts)}_{uuid.uuid4().hex[:10]}"
        return {
            "id": post_id,
            "user": user.lstrip("@").lower(),
            "text": text,
            "timestamp": unix_ts,
            "url": f"https://x.com/{user.lstrip('@')}/status/{post_id}",
            "image_urls": [],   # schema-matched with real XCollector (mock has no images)
            "is_mock_spike": is_mock_spike,
            "source": "mock_v1",
        }

    def _should_spike(self, now_loop_time: float) -> bool:
        if not self._spike_enabled:
            return False
        if self._last_global_spike_t is None:
            # First spike: 50% of spike_every_s later
            return now_loop_time >= self._spike_every_s * 0.5
        return (now_loop_time - self._last_global_spike_t) >= self._spike_every_s


# =====================================================================
# env helpers
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
        logger.warning("MockXCollector: invalid env %s=%r, using default %d",
                       name, v, default)
        return default
