"""
x/channel.py — XChannel: collector → Stage1Filter → LLMClassifier → ChannelSignal.

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §2 Component, §3 Process View, plan P9.4):

  Implements the Channel base lifecycle. 10-minute polling cycle per plan §8 P5.
  Reduce poll_interval_s for fast walking-skeleton smoke.

  Pipeline (P9.4 — fully replaces the v0 keyword/count detector):

    every cycle:
      posts = await collector.fetch_recent_posts()                    # 1)
      for p in posts:
          if p.id seen → skip                                         # dedupe
          stage1 = stage1_filter.evaluate(p)                          # 2)
          if not stage1.passed: drop (cost 0)                         # 3)
          classification = await llm.classify(p)                      # 4)
          for symbol in classification.symbols:
              signal = llm.to_channel_signal(classification, symbol)  # 5)
              latest_signal[symbol] = (now, signal)                   # 6)

      # get_current_signal() returns the max-tier signal within the sticky window.

────────────────────────────────────────────────────────────────────────
Design decisions:

  · **Fully removes v0 (XFeatures + XDetector + parser regex)**.
    Reason: the "multiple accounts mention the same symbol simultaneously" walking
    skeleton signal is poorly suited for early insider-trading detection (most
    cases are single-post). The LLM must be able to judge EMERGENCY from a single
    post on its own.

  · Collector is via dependency injection — `MockXCollector`
    (test/walking-skeleton) or real `XCollector` (X API v2 Bearer) both work.
    Both share the same async interface (`fetch_recent_posts()`).

  · Stage1Filter is **free** (regex). LLMClassifier is invoked **only when stage1
    passes**. → Usually cost ≈ 0; suspicious posts trigger the LLM at ~$0.005/post.

  · LLMClassifier has built-in cache (1h TTL keyed by post_id) → it never calls
    the same post twice. Acts as a second safety net alongside channel dedupe.

  · sticky_window_s = 30 min: signal stays valid through 3 polling cycles of 10 min.
    Works as long as the fusion engine polls within poll_interval_s.

  · `get_current_signal()` is **sync** (required by Channel base interface).
    Internally a dict lookup + filter — safe without locks.

────────────────────────────────────────────────────────────────────────
Env vars / config dependencies:

  - `OPENAI_API_KEY`        : used by LLMClassifier (raise at init if missing)
  - `X_API_BEARER_TOKEN`    : real XCollector fallback (optional)
  - `config/x_keywords.yaml`: used by Stage1Filter
  - `config/x_few_shot.yaml`: used by LLMClassifier

────────────────────────────────────────────────────────────────────────
Plan: P9.4 (X channel LLM upgrade), architecture §5.4.1 channel tier
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar, Deque, Protocol

from ...core.schemas import (
    CHANNEL_X,
    ChannelSignal,
    Tier,
)
from ...storage.raw_store import RawStore
from ..base import Channel
from .llm_classifier import LLMClassifier
from .stage1_filter import Stage1Filter, Stage1Result

logger = logging.getLogger(__name__)

_DEFAULT_STICKY_WINDOW_S: float = 30 * 60.0   # 30 min
_DEFAULT_POLL_INTERVAL_S: float = 600.0       # 10 min (D8: P5 polling interval)


# =====================================================================
# Collector interface — duck-typing protocol (satisfied by both
# MockXCollector and XCollector). No isinstance check; in practice only
# attribute matching matters.
# =====================================================================
class _CollectorProto(Protocol):
    """Minimum interface XChannel demands of a collector."""

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def fetch_recent_posts(self) -> list[dict[str, Any]]: ...


# =====================================================================
# Main class
# =====================================================================
class XChannel(Channel):
    """X channel — Stage1 keyword filter + LLM (GPT-5.4-mini) classification pipeline.

    Args:
        collector: MockXCollector or real XCollector. None → auto-create a real
                   XCollector with the supplied accounts.
        accounts: list of X account handles to monitor (used when collector=None).
        keywords_yaml_path: path to config/x_keywords.yaml. None → default
                            (project_root/config/x_keywords.yaml).
        few_shot_yaml_path: path to config/x_few_shot.yaml. None → default.
        openai_api_key: OpenAI key. None → env OPENAI_API_KEY.
        model: GPT model name (default "gpt-5.4-mini" — enough for classification
               and ~3.3x cheaper than frontier. Promote to "gpt-5.4" if accuracy is insufficient).
        raw_store: optional. None → do not store raw payloads.
        poll_interval_s: polling interval seconds (default 600 = 10 min).
        sticky_window_s: signal valid-window seconds (default 1800 = 30 min).
        dedupe_capacity: cache size for seen post ids (default 1024).
        x_api_bearer_token: real XCollector fallback. None → env.

    Raises:
        FileNotFoundError: when the yaml file is missing.
        ValueError: when collector and accounts are both None.
    """

    name: ClassVar[str] = CHANNEL_X

    def __init__(
        self,
        *,
        collector: _CollectorProto | None = None,
        accounts: list[str] | None = None,
        keywords_yaml_path: str | Path | None = None,
        few_shot_yaml_path: str | Path | None = None,
        openai_api_key: str | None = None,
        model: str = "gpt-5.4-mini",
        raw_store: RawStore | None = None,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        sticky_window_s: float = _DEFAULT_STICKY_WINDOW_S,
        dedupe_capacity: int = 1024,
        x_api_bearer_token: str | None = None,
    ) -> None:
        # ── Collector DI (or auto-create) ───────────────────────────
        if collector is None:
            if not accounts:
                raise ValueError(
                    "XChannel: must provide either collector or accounts"
                )
            from .collector import XCollector
            collector = XCollector(
                accounts=accounts,
                x_api_bearer_token=x_api_bearer_token,
            )
        self._collector = collector
        self._accounts = list(accounts or [])

        # ── Stage 1 (keyword/regex filter) ──────────────────────────
        if keywords_yaml_path is None:
            keywords_yaml_path = _default_config_path("x_keywords.yaml")
        self._stage1 = Stage1Filter(keywords_yaml_path=keywords_yaml_path)

        # ── Stage 2 (LLM classifier) ────────────────────────────────
        if few_shot_yaml_path is None:
            few_shot_yaml_path = _default_config_path("x_few_shot.yaml")
        self._llm = LLMClassifier(
            few_shot_yaml_path=few_shot_yaml_path,
            model=model,
            api_key=openai_api_key,
        )

        # ── Storage (optional) ──────────────────────────────────────
        self._raw_store = raw_store

        # ── Timing ──────────────────────────────────────────────────
        self._poll_interval_s = max(1.0, float(poll_interval_s))
        self._sticky_window_s = max(60.0, float(sticky_window_s))

        # ── State ───────────────────────────────────────────────────
        # symbol → (signal_ts, ChannelSignal). Valid within the sticky window.
        self._latest_signal: dict[str, tuple[datetime, ChannelSignal]] = {}

        # ts of the most recently processed post — used by health monitor (`last_event_ts`)
        self._last_event_ts: datetime | None = None

        # post id dedupe across cycles
        self._seen_post_ids: Deque[str] = deque(maxlen=dedupe_capacity)
        self._seen_set: set[str] = set()

        # ── asyncio task management ────────────────────────────────
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

        logger.info(
            "XChannel: initialized "
            "(collector=%s, model=%s, poll=%.0fs, sticky=%.0fs)",
            type(self._collector).__name__, model,
            self._poll_interval_s, self._sticky_window_s,
        )

    # ─────────────────────────────────────────────────────────────────
    # Channel base — Lifecycle
    # ─────────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            logger.warning("XChannel.start: already running, ignoring")
            return

        await self._collector.open()
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._poll_loop(), name="x-poll-loop")
        logger.info("XChannel: started (interval=%.0fs)", self._poll_interval_s)

    async def stop(self) -> None:
        if self._stop_event is None:
            return
        if self._stop_event.is_set():
            return

        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(
                    self._task, timeout=self._poll_interval_s + 5.0
                )
            except asyncio.TimeoutError:
                logger.warning("XChannel.stop: forcing cancel")
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, BaseException):
                    pass
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None

        await self._collector.close()
        logger.info("XChannel: stopped")

    # ─────────────────────────────────────────────────────────────────
    # Channel base — Signal output (fusion engine polls)
    # ─────────────────────────────────────────────────────────────────
    def get_current_signal(self) -> ChannelSignal | None:
        """Return the highest-tier signal within the sticky window."""
        if not self._latest_signal:
            return None

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=self._sticky_window_s)

        candidates: list[ChannelSignal] = []
        for ts, sig in self._latest_signal.values():
            if ts < cutoff:
                continue
            if sig.tier == Tier.NORMAL:
                # NORMAL is equivalent to "no signal" for the fusion engine
                continue
            candidates.append(sig)

        if not candidates:
            return None

        # max-tier wins; break ties on higher score
        return max(candidates, key=lambda s: (s.tier.rank(), s.score))

    @property
    def last_event_ts(self) -> datetime | None:
        return self._last_event_ts

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ─────────────────────────────────────────────────────────────────
    # Polling loop
    # ─────────────────────────────────────────────────────────────────
    async def _poll_loop(self) -> None:
        """Call _poll_once() every poll_interval_s. Exits on stop_event."""
        assert self._stop_event is not None
        # Don't poll immediately on start (yield to other channels' startup)
        await asyncio.sleep(1.0)

        while not self._stop_event.is_set():
            cycle_start = asyncio.get_event_loop().time()
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("XChannel: poll cycle failed: %s", e)

            elapsed = asyncio.get_event_loop().time() - cycle_start
            sleep_s = max(0.1, self._poll_interval_s - elapsed)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_s)
            except asyncio.TimeoutError:
                continue
            else:
                break   # stop_event set

    async def _poll_once(self) -> None:
        """One polling cycle: collector → stage1 → llm → ChannelSignal.

        Callable from outside (e.g. tests) — verifies one cycle without start().
        """
        # P12-F: surface fetch attempt success/fail to weekly_digest. The
        # outer _poll_loop already catches exceptions, but we mark explicitly
        # so the digest distinguishes "fetch never tried" from "fetch failed".
        try:
            posts = await self._collector.fetch_recent_posts()
        except Exception as e:
            self._record_fetch_fail(e)
            raise
        self._record_fetch_ok()
        if not posts:
            return

        stage1_pass_count = 0
        signals_emitted = 0

        for post in posts:
            pid = str(post.get("id", ""))
            if not pid or pid in self._seen_set:
                continue

            # ── dedupe registration ────────────────────────────────
            self._seen_set.add(pid)
            self._seen_post_ids.append(pid)
            # Also drop from set ids evicted by deque maxlen
            assert self._seen_post_ids.maxlen is not None
            if len(self._seen_set) > self._seen_post_ids.maxlen:
                self._seen_set = set(self._seen_post_ids)

            # ── raw_store (optional, for audit) ────────────────────
            self._maybe_store_raw(post)

            # ── update last_event_ts (for health monitor) ──────────
            self._update_last_event_ts(post)

            # ── Stage 1: keyword/regex filter (cost 0) ─────────────
            try:
                stage1 = self._stage1.evaluate(post)
            except Exception as e:
                logger.warning(
                    "XChannel: stage1 failed post_id=%s: %s", pid, e
                )
                continue

            if not stage1.passed:
                self._log_stage1_drop(post, stage1)
                continue

            stage1_pass_count += 1

            # ── Stage 2: LLM classification (cost) ──────────────────
            try:
                classification = await self._llm.classify(post)
            except Exception as e:
                logger.error(
                    "XChannel: LLM classify failed post_id=%s: %s", pid, e
                )
                continue

            # NORMAL result emits no ChannelSignal (no signal = NORMAL)
            if classification.tier == Tier.NORMAL:
                self._log_llm_normal(post, classification.reasoning)
                continue

            # No symbols → nothing to emit
            if not classification.symbols:
                logger.warning(
                    "XChannel: LLM tier=%s but symbols are empty (post_id=%s)",
                    classification.tier.value, pid,
                )
                continue

            # ── Emit one ChannelSignal per symbol ──────────────────
            now = datetime.now(timezone.utc)
            for symbol in classification.symbols:
                signal = self._llm.to_channel_signal(
                    classification, symbol=symbol, ts=now,
                )
                self._latest_signal[symbol] = (now, signal)
                signals_emitted += 1
                logger.info(
                    "x signal: symbol=%s tier=%s score=%.3f reasons=%s",
                    symbol, signal.tier.value, signal.score,
                    signal.reason_codes[:3],
                )

        if posts:
            logger.info(
                "XChannel: cycle done — posts=%d, stage1_passed=%d, signals=%d",
                len(posts), stage1_pass_count, signals_emitted,
            )

    # ─────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────
    def _maybe_store_raw(self, post: dict[str, Any]) -> None:
        """If raw_store is configured, build a RawEvent and store it (best-effort)."""
        if self._raw_store is None:
            return
        try:
            from ...core.schemas import RawEvent, Source
            ts_unix = post.get("timestamp")
            ts_source = (
                datetime.fromtimestamp(int(ts_unix), tz=timezone.utc)
                if isinstance(ts_unix, (int, float))
                else datetime.now(timezone.utc)
            )
            raw = RawEvent(
                channel=CHANNEL_X,
                source=Source.SCRAPE,
                symbol="",   # X channel extracts the symbol via LLM → not yet known at raw stage
                ts_source=ts_source,
                payload=post,
            )
            self._raw_store.append(raw)
        except Exception as e:
            logger.error("XChannel: raw_store.append failed: %s", e)

    def _update_last_event_ts(self, post: dict[str, Any]) -> None:
        """For the health monitor — update ts of the most recently processed post."""
        ts_unix = post.get("timestamp")
        if not isinstance(ts_unix, (int, float)):
            return
        ts = datetime.fromtimestamp(int(ts_unix), tz=timezone.utc)
        if self._last_event_ts is None or ts > self._last_event_ts:
            self._last_event_ts = ts

    def _log_stage1_drop(self, post: dict[str, Any], r: Stage1Result) -> None:
        """Log posts dropped at Stage 1 (for audit, at debug level)."""
        if r.rejected_by_irrelevant:
            logger.debug(
                "XChannel: stage1 reject (irrelevant) post_id=%s reasons=%s",
                post.get("id"), r.irrelevant_matched[:3],
            )
        else:
            logger.debug(
                "XChannel: stage1 drop post_id=%s score=%.2f<thresh=%.2f",
                post.get("id"), r.score, self._stage1.llm_threshold,
            )

    def _log_llm_normal(self, post: dict[str, Any], reasoning: str) -> None:
        """Log posts that passed Stage 1 but the LLM ruled NORMAL (false positive guard)."""
        logger.debug(
            "XChannel: LLM=NORMAL (stage1 false positive) post_id=%s reason=%s",
            post.get("id"), reasoning[:100],
        )


# =====================================================================
# Helpers
# =====================================================================
def _default_config_path(filename: str) -> Path:
    """Auto-locate config/<filename> at the project root.

    src/anomaly/channels/x/channel.py → ../../../../config/<filename>
    """
    here = Path(__file__).resolve()
    # src/anomaly/channels/x/  → project root is 4 levels up
    project_root = here.parent.parent.parent.parent.parent
    return project_root / "config" / filename
