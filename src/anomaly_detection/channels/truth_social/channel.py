"""
truth_social/channel.py — TruthSocialChannel: collector → LLM scorer → ChannelSignal.

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §2, plan Step 3):

  A polling channel with the same structure as the X channel. Differences:
    · One post = one sample (no keyword Stage1 filter as in X — every Trump post
      is sent to the LLM. Cost burden is light: ~20 posts/day × $0.005 ≈ $0.10)
    · symbol is the ticker the LLM returns via key_tickers. Default "SPY".

────────────────────────────────────────────────────────────────────────
Pipeline (per polling cycle):

  posts = await collector.fetch_recent(limit=20)            # 1) ~5min polling
  for p in posts:
      if p.post_id in seen: continue                        # dedupe
      score = await scorer.score(p)                         # 2) LLM call
      if score.score < WATCH_THRESHOLD: continue             # 3) score too low — drop
      symbols = score.key_tickers or ["SPY"]                 # 4) decide fan-out
      for sym in symbols:
          sig = scorer.to_channel_signal(post=p, score=score, symbol=sym)
          latest_signal[sym] = (now, sig)

  · `get_current_signal()` returns the max-tier signal within the sticky window.

────────────────────────────────────────────────────────────────────────
Sticky window:

  · Default 30 min (same as X channel). Signal stays valid through 5 min × 6 cycles.
  · Market impact is immediate and single-shot — once a Trump post fires, the
    channel's verdict is fixed to that post for ~30 min.

────────────────────────────────────────────────────────────────────────
Env vars / config:

  · OPENAI_API_KEY  (required — used by scorer + reference_db embedding)

────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, ClassVar, Deque

from ...alerts.health_alert import HealthAlertKind, SystemHealthAlert
from ...core.schemas import (
    CHANNEL_TRUTH_SOCIAL,
    ChannelSignal,
    Tier,
)
from ...storage.raw_store import RawStore
from ..base import Channel
from .collector import TrumpCollector
from .llm_scorer import MarketImpactScore, TruthSocialLLMScorer
from .normalize import TruthPost
from .reference_db import TruthSocialReferenceDB

# Callback signature — kept compatible with dispatcher.dispatch_health_alert.
HealthAlertCallback = Callable[[SystemHealthAlert], Awaitable[object]]

logger = logging.getLogger(__name__)

_DEFAULT_STICKY_WINDOW_S: float = 30 * 60.0    # 30 min
_DEFAULT_POLL_INTERVAL_S: float = 300.0        # 5 min (user decision)
_DEFAULT_FETCH_LIMIT: int = 20                  # max fetch per cycle

# LLM score floor — below 5 is lower than WATCH → no ChannelSignal.
# (orchestrator treats it as NORMAL + saves resources).
_MIN_SCORE_TO_EMIT: int = 5


class TruthSocialChannel(Channel):
    """Truth Social channel — Trump post polling + LLM scoring.

    Args:
        collector: TrumpCollector instance (DI). None → default-create.
        scorer: TruthSocialLLMScorer (DI). None → create with reference_db + api_key.
        reference_db: pre-load()/precomputed ReferenceDB. None → default.
        openai_api_key: used by both scorer + ref_db embedding. None → env.
        model: LLM model name (default gpt-5.4).
        raw_store: optional — RawStore. When present, persist raw payload per post.
        poll_interval_s: polling interval (default 300 = 5 min).
        sticky_window_s: ChannelSignal validity window (default 1800 = 30 min).
        dedupe_capacity: seen post_id cache size (default 1024).
        enable_embedding: enable ReferenceDB Stage 2 embedding.
        health_alert_cb: P12-F. Async callback fired on consecutive fetch fail
            / recovery. Typically `ChannelAlertDispatcher.dispatch_health_alert`.
            None → no ops-health alerting (silent fail possible — fine for tests).
        fail_alert_threshold: number of consecutive fail cycles before firing a
            REACTIVE_FAIL alert. With poll_interval=300s × 5 = 25 min — absorbs
            short Cloudflare hiccups before paging.

    Raises:
        RuntimeError: when constructed without an openai key (scorer fails immediately).
    """

    name: ClassVar[str] = CHANNEL_TRUTH_SOCIAL

    def __init__(
        self,
        *,
        collector: TrumpCollector | None = None,
        scorer: TruthSocialLLMScorer | None = None,
        reference_db: TruthSocialReferenceDB | None = None,
        openai_api_key: str | None = None,
        model: str = "gpt-5.4",
        raw_store: RawStore | None = None,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        sticky_window_s: float = _DEFAULT_STICKY_WINDOW_S,
        dedupe_capacity: int = 1024,
        enable_embedding: bool = True,
        health_alert_cb: HealthAlertCallback | None = None,
        fail_alert_threshold: int = 5,
    ) -> None:
        # ── Reference DB ─────────────────────────────────────────────
        if reference_db is None:
            reference_db = TruthSocialReferenceDB(
                enable_embedding=enable_embedding,
                openai_api_key=openai_api_key,
            )
        self._ref_db = reference_db

        # ── Scorer ───────────────────────────────────────────────────
        if scorer is None:
            scorer = TruthSocialLLMScorer(
                reference_db=reference_db,
                api_key=openai_api_key,
                model=model,
            )
        self._scorer = scorer

        # ── Collector ────────────────────────────────────────────────
        self._collector = collector or TrumpCollector()

        # ── Storage ──────────────────────────────────────────────────
        self._raw_store = raw_store

        # ── Timing ───────────────────────────────────────────────────
        self._poll_interval_s = max(30.0, float(poll_interval_s))
        self._sticky_window_s = max(60.0, float(sticky_window_s))

        # ── State ────────────────────────────────────────────────────
        # symbol → (ts, ChannelSignal)
        self._latest_signal: dict[str, tuple[datetime, ChannelSignal]] = {}
        self._last_event_ts: datetime | None = None

        # post id dedupe
        self._seen_post_ids: Deque[str] = deque(maxlen=dedupe_capacity)
        self._seen_set: set[str] = set()

        # asyncio task
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

        # ── P12-F: Reactive health alerting ──────────────────────────
        # Consecutive fetch-fail counter — incremented by _poll_once when the
        # collector raises, reset on a successful cycle. Once it crosses the
        # threshold, one REACTIVE_FAIL alert is dispatched (dispatcher cooldown
        # then mutes repeats); recovery sends one REACTIVE_RECOVERY.
        self._health_alert_cb = health_alert_cb
        self._fail_alert_threshold = max(1, int(fail_alert_threshold))
        self._consecutive_fetch_failures: int = 0
        self._first_fail_ts: datetime | None = None
        self._last_fail_error: str = ""
        self._fail_alert_active: bool = False  # already paged for the current outage?

        logger.info(
            "TruthSocialChannel: initialized "
            "(model=%s, poll=%.0fs, sticky=%.0fs, embedding=%s, "
            "health_alert=%s, fail_threshold=%d)",
            model, self._poll_interval_s, self._sticky_window_s,
            enable_embedding,
            "on" if health_alert_cb is not None else "off",
            self._fail_alert_threshold,
        )

    # ─────────────────────────────────────────────────────────────────
    # Channel base — Lifecycle
    # ─────────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            logger.warning("TruthSocialChannel.start: already running")
            return

        # Precompute Reference DB embeddings once (~1-2 min).
        # Channel still works via keyword retrieval if this fails.
        try:
            self._ref_db.load()
            await self._ref_db.precompute_embeddings()
        except Exception as e:  # noqa: BLE001 — best-effort precompute
            logger.warning(
                "TruthSocialChannel: precompute_embeddings failed: %s "
                "(keyword retrieval still works)", e,
            )

        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._poll_loop(), name="truth-social-poll-loop",
        )
        logger.info(
            "TruthSocialChannel: started (interval=%.0fs)",
            self._poll_interval_s,
        )

    async def stop(self) -> None:
        if self._stop_event is None:
            return
        if self._stop_event.is_set():
            return

        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(
                    self._task, timeout=self._poll_interval_s + 5.0,
                )
            except asyncio.TimeoutError:
                logger.warning("TruthSocialChannel.stop: forcing cancel")
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, BaseException):
                    pass
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None

        await self._collector.aclose()
        logger.info("TruthSocialChannel: stopped")

    # ─────────────────────────────────────────────────────────────────
    # Channel base — Signal output
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
                continue
            candidates.append(sig)
        if not candidates:
            return None
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
        assert self._stop_event is not None
        await asyncio.sleep(2.0)  # yield to other channels' startup
        while not self._stop_event.is_set():
            cycle_start = asyncio.get_event_loop().time()
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — best-effort cycle
                logger.exception("TruthSocialChannel: poll cycle failed: %s", e)

            elapsed = asyncio.get_event_loop().time() - cycle_start
            sleep_s = max(0.1, self._poll_interval_s - elapsed)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=sleep_s,
                )
            except asyncio.TimeoutError:
                continue
            else:
                break

    async def _poll_once(self) -> None:
        """One polling cycle. Callable from outside (tests too)."""
        try:
            posts = await self._collector.fetch_recent(limit=_DEFAULT_FETCH_LIMIT)
        except Exception as e:  # noqa: BLE001 — collector exception
            logger.warning("TruthSocialChannel: fetch_recent failed: %s", e)
            await self._record_fetch_failure(error=e)
            return

        # Fetch succeeded — clear the fail streak; if we had already paged for
        # an outage, also dispatch a one-shot recovery notice.
        await self._record_fetch_success()

        if not posts:
            return

        signals_emitted = 0
        for post in posts:
            if post.post_id in self._seen_set:
                continue
            self._seen_set.add(post.post_id)
            self._seen_post_ids.append(post.post_id)
            assert self._seen_post_ids.maxlen is not None
            if len(self._seen_set) > self._seen_post_ids.maxlen:
                self._seen_set = set(self._seen_post_ids)

            self._maybe_store_raw(post)
            self._update_last_event_ts(post)

            score = await self._scorer.score(post)
            emit = await self._maybe_emit(post, score)
            signals_emitted += emit

        logger.info(
            "TruthSocialChannel: cycle done — posts=%d, signals=%d",
            len(posts), signals_emitted,
        )

    async def _maybe_emit(
        self,
        post: TruthPost,
        score: MarketImpactScore,
    ) -> int:
        """Emit ChannelSignal based on score. Returns the number of signals emitted.

        User decision (2026-05-15): no symbol fan-out. 1 post = 1 signal.
        Put the `topic_slug` produced by the LLM into ChannelSignal.symbol
        (e.g. "liberation_day", "rare_earth_export_ban") so it flows directly
        into the email subject's `Truthsocial · {topic}` slot.

        key_tickers is preserved in reason_codes (TICKERS=NVDA,TSM …).
        """
        if score.score < _MIN_SCORE_TO_EMIT:
            logger.info(
                "truth_social drop: post_id=%s score=%d/10 (<%d) cat=%s "
                "rationale=%s",
                post.post_id, score.score, _MIN_SCORE_TO_EMIT,
                score.category, score.rationale[:80],
            )
            return 0

        topic = (score.topic_slug or score.category or "other").strip().lower()
        # Safety: normalize so the subject has no whitespace / special chars
        topic = "".join(c if c.isalnum() or c == "_" else "_" for c in topic)
        if not topic:
            topic = "other"

        now = datetime.now(timezone.utc)
        sig = self._scorer.to_channel_signal(
            post=post, score=score, symbol=topic, ts=now,
        )
        # symbol = topic_slug — if the same topic recurs inside the short window, overwrite.
        self._latest_signal[topic] = (now, sig)
        logger.info(
            "truth_social signal: post_id=%s topic=%s tier=%s score=%.2f "
            "dir=%s tickers=%s sim=%s insider=%d/10",
            post.post_id, topic, sig.tier.value, sig.score,
            sig.direction.value,
            ",".join(score.key_tickers) if score.key_tickers else "-",
            score.most_similar_event_id or "none",
            score.insider_concern_score,
        )
        return 1

    # ─────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────
    def _maybe_store_raw(self, post: TruthPost) -> None:
        """RawStore append (best-effort)."""
        if self._raw_store is None:
            return
        try:
            from ...core.schemas import RawEvent, Source
            raw = RawEvent(
                channel=CHANNEL_TRUTH_SOCIAL,
                source=Source.REST,  # Mastodon API = REST polling
                symbol="",
                ts_source=post.created_at,
                payload=post.raw or {"post_id": post.post_id, "text": post.text},
            )
            self._raw_store.append(raw)
        except Exception as e:  # noqa: BLE001 — raw_store best-effort
            logger.warning("TruthSocialChannel: raw_store.append failed: %s", e)

    def _update_last_event_ts(self, post: TruthPost) -> None:
        if self._last_event_ts is None or post.created_at > self._last_event_ts:
            self._last_event_ts = post.created_at

    # ─────────────────────────────────────────────────────────────────
    # P12-F: Reactive health monitor
    # ─────────────────────────────────────────────────────────────────
    async def _record_fetch_failure(self, *, error: BaseException) -> None:
        """Record a 1-cycle fetch failure; page on threshold (REACTIVE_FAIL).

        Once `_fail_alert_active` flips to True the dispatcher's 24h cooldown
        suppresses repeats. After recovery, a new outage resets `first_fail_ts`
        and can page again.

        Also propagates to the base-class fetch_health used by weekly_digest.
        """
        self._record_fetch_fail(error)  # P12-F: update base fetch_health snapshot
        self._consecutive_fetch_failures += 1
        self._last_fail_error = f"{type(error).__name__}: {error!s}"[:500]
        if self._first_fail_ts is None:
            self._first_fail_ts = datetime.now(timezone.utc)

        if (
            self._consecutive_fetch_failures < self._fail_alert_threshold
            or self._fail_alert_active
            or self._health_alert_cb is None
        ):
            return

        alert = SystemHealthAlert(
            kind=HealthAlertKind.REACTIVE_FAIL,
            component=f"channel.{CHANNEL_TRUTH_SOCIAL}",
            message=(
                f"Truth Social fetch failed "
                f"{self._consecutive_fetch_failures} cycles in a row "
                f"(≈{self._consecutive_fetch_failures * self._poll_interval_s / 60:.0f} min). "
                f"Check publisher cron + GCS bucket."
            ),
            since=self._first_fail_ts or datetime.now(timezone.utc),
            consecutive_failures=self._consecutive_fetch_failures,
            detail=self._last_fail_error,
        )
        try:
            await self._health_alert_cb(alert)
        except Exception as cb_err:  # noqa: BLE001 — alert path is best-effort
            logger.exception(
                "TruthSocialChannel: health_alert_cb (FAIL) raised — %s", cb_err,
            )
            return
        self._fail_alert_active = True
        logger.warning(
            "TruthSocialChannel: REACTIVE_FAIL alert dispatched "
            "(consecutive=%d, first_fail=%s)",
            self._consecutive_fetch_failures,
            self._first_fail_ts.isoformat() if self._first_fail_ts else "—",
        )

    async def _record_fetch_success(self) -> None:
        """Reset counters; if we had already paged, send a one-shot recovery.

        Also propagates 'ok' to the base-class fetch_health used by weekly_digest.
        """
        self._record_fetch_ok()  # P12-F: update base fetch_health snapshot
        had_failures = self._consecutive_fetch_failures > 0
        was_alert_active = self._fail_alert_active

        self._consecutive_fetch_failures = 0
        self._first_fail_ts = None
        self._last_fail_error = ""
        self._fail_alert_active = False

        if not (had_failures and was_alert_active and self._health_alert_cb):
            return

        alert = SystemHealthAlert(
            kind=HealthAlertKind.REACTIVE_RECOVERY,
            component=f"channel.{CHANNEL_TRUTH_SOCIAL}",
            message="Truth Social fetch recovered — receiving posts again.",
        )
        try:
            await self._health_alert_cb(alert)
        except Exception as cb_err:  # noqa: BLE001
            logger.exception(
                "TruthSocialChannel: health_alert_cb (RECOVERY) raised — %s",
                cb_err,
            )
            return
        logger.info("TruthSocialChannel: REACTIVE_RECOVERY alert dispatched")
