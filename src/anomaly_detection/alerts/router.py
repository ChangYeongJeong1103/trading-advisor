"""
alerts/router.py — Per-tier dispatch + cross-tag + heartbeat (architecture §6.5).

────────────────────────────────────────────────────────────────────────
Role:
  AlertRouter is the orchestrator's AlertRouter Protocol implementation.
  dispatch(decision) is called on every transition. Responsibilities:

    1) Look up fused_event + contributing signals from signal_store
    2) Pick primary_symbol (the symbol of the strongest-tier channel signal)
    3) Update active_higher_tier for cross-tag (track escalation/de-escalation)
    4) Check throttle.should_send → if it doesn't pass (queue if DIGEST, drop otherwise)
    5) Build external_links via link_builder (per channel type)
    6) Always send via email_renderer
    7) For URGENT (EMERGENCY), also send via telegram_renderer
    8) Return the list of channels delivered to (orchestrator records as a metric)

  Failure isolation: a failure in one channel (email/telegram) does not block the other.

────────────────────────────────────────────────────────────────────────
EMERGENCY heartbeat (architecture §6.5.5):
  emit_heartbeat_if_due() — polled once per minute by a separate task in the daemon.
  When an active EMERGENCY exceeds emergency_heartbeat_hours (default 1h),
  send a reminder email + telegram. The 1h count then restarts.

Cross-tag (architecture §6.5.5):
  Add to active when entering RISK_OFF/EMERGENCY; remove when entering NORMAL.
  Appended to the email subject as a suffix like "[🚨 EMERGENCY (BTC) ACTIVE]".

────────────────────────────────────────────────────────────────────────
v0 simplifications (refined in P9):
  - Daily digest auto flush — owned by the daemon's scheduled task
    (router only provides the flush_digest_now() helper)
  - 5-min batch (group multiple symbols) — TODO P9
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock

from ..core.schemas import (
    ChannelSignal,
    DecisionRecord,
    DeliveryTier,
    FusedAnomalyEvent,
    Tier,
    CHANNEL_CME,
    CHANNEL_HYPERLIQUID,
    CHANNEL_POLYMARKET,
    CHANNEL_X,
)
from ..storage.signal_store import SignalStore
from . import link_builder
from .format_time import format_pdt
from .renderer.email import EmailRenderer
from .renderer.telegram import TelegramRenderer
from .throttle import AlertThrottle

# P9.3.P1.C — optional CME enrichment (Databento fetch + post-analysis).
# Type-only import to avoid an import cycle + keep enricher module optional.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..channels.cme.enricher import CMEEnricher

logger = logging.getLogger(__name__)


@dataclass
class _ActiveTier:
    """Tracking entry for active high-tier (cross-tag + heartbeat)."""
    tier: Tier                            # currently active tier (RISK_OFF / EMERGENCY)
    since: datetime                       # time of first entry
    last_heartbeat: datetime | None = None  # time the last reminder was sent


# ─────────────────────────────────────────────────────────────────────
# AlertRouter
# ─────────────────────────────────────────────────────────────────────
class AlertRouter:
    """orchestrator.AlertRouter Protocol implementation."""

    def __init__(
        self,
        *,
        throttle: AlertThrottle,
        email_renderer: EmailRenderer,
        telegram_renderer: TelegramRenderer,
        signal_store: SignalStore,
        emergency_heartbeat_hours: int = 1,
        cme_enricher: "CMEEnricher | None" = None,
        email_enabled: bool = True,
        telegram_enabled: bool = True,
    ) -> None:
        """
        Args:
            email_enabled: P11(a).4.5 (user decision locked 2026-04-21) —
                When False, the system-level fusion email path is disabled.
                The per-channel dispatcher (alerts/channel_dispatcher.py) takes
                over email responsibility. Default True for v0/v1 compatibility.
            telegram_enabled: P11(d) (user decision locked 2026-04-21) —
                When False, both the system-level URGENT telegram and the
                heartbeat reminder are disabled. The per-channel dispatcher's
                telegram path takes over EMERGENCY notifications. Default True
                for v0/v1 compatibility.
        """
        self._throttle = throttle
        self._email = email_renderer
        self._telegram = telegram_renderer
        self._signal_store = signal_store
        self._heartbeat_interval = timedelta(hours=emergency_heartbeat_hours)
        self._email_enabled = email_enabled
        self._telegram_enabled = telegram_enabled
        # P9.3.P1.C — optional Databento enrichment (None → dispatch with primary signal only).
        # Injected by the daemon only when secrets/cap allow → safe in unconfigured envs (test/dry-run).
        self._cme_enricher = cme_enricher
        # symbol → active high-tier (RISK_OFF / EMERGENCY).
        # Removed when entering NORMAL/WATCH. Used for cross-tag + heartbeat.
        self._active: dict[str, _ActiveTier] = {}
        self._lock = Lock()

    # ─────────────────────────────────────────────────────────────────
    # Public API — called by the orchestrator
    # ─────────────────────────────────────────────────────────────────
    async def dispatch(self, decision: DecisionRecord) -> list[str]:
        """Take a DecisionRecord, throttle/render/send. Returns the list of channels delivered to.

        Failure isolation: a failure in one channel does not block another. Failures are logged + skipped.
        """
        delivered: list[str] = []

        # 1) fused_event + contributing signals lookup
        fused = self._signal_store.get_fused_event(decision.fused_event_ref)
        if fused is None:
            logger.error(
                "AlertRouter: fused event %s not found, skipping dispatch",
                decision.fused_event_ref,
            )
            return delivered

        contributing = self._lookup_contributing(fused)

        # 2) Choose primary symbol
        primary_symbol = self._pick_primary_symbol(fused, contributing)

        # 3) Update active_higher_tier (transition-driven)
        # (P9.3.P2.B) Beyond primary, also add every RISK_OFF+ symbol from contributing.
        # → On simultaneous multi-channel firings in the same cycle, all are exposed in the cross-tag body.
        if decision.state_change is not None:
            self._update_active(
                primary_symbol,
                decision.state_change,
                decision.ts,
                contributing=contributing,
            )

        # 4) Throttle check
        ok, why = self._throttle.should_send(decision, primary_symbol, decision.ts)
        if not ok:
            # If DIGEST, send to the queue separately
            if decision.delivery_tier == DeliveryTier.DIGEST:
                self._throttle.queue_for_digest(decision, fused, primary_symbol, decision.ts)
                logger.info("AlertRouter: queued for digest (%s)", primary_symbol)
            else:
                logger.info("AlertRouter: throttled — %s", why)
            return delivered

        # 4b) CME enrichment (P9.3.P1.C) — only call after throttle passes
        #     (avoids Databento fetch costs for blocked alerts).
        #     Tier gate (RISK_OFF/EMERGENCY) is handled inside the enricher.
        #     The enricher never raises → contributing is always safe.
        if self._cme_enricher is not None and decision.state_change is not None:
            target_tier = decision.state_change[1]
            try:
                contributing = await self._cme_enricher.enrich_decision(
                    decision_tier=target_tier,
                    contributing=contributing,
                )
            except Exception as e:
                # Safety net — already caught inside the enricher, but just in case
                logger.error("AlertRouter: enricher unexpected error: %s", e)

        # 5) Build external_links
        external_links = self._build_links(contributing)

        # 6) cross-tag — exclude primary_symbol itself (don't cross-tag yourself)
        active_higher = self._snapshot_active(exclude_symbol=primary_symbol)

        # 7) email — always send (REALTIME, URGENT)
        # P11(a).4.5 — when email_enabled=False, the system-level email is disabled.
        # The per-channel dispatcher takes over email responsibility (user decision 2026-04-21).
        if self._email_enabled:
            try:
                subject = self._email.make_subject(decision, primary_symbol, active_higher)
                html = self._email.render_html(
                    decision, fused,
                    contributing_signals=contributing,
                    external_links=external_links,
                    active_higher_tier=active_higher,
                )
                await self._email.send(subject, html)
                delivered.append("email")
            except Exception as e:
                logger.error("AlertRouter: email send failed: %s", e)
        else:
            logger.debug(
                "AlertRouter: system-level email skipped (email_enabled=False, "
                "using per-channel dispatcher)"
            )

        # 8) telegram — URGENT (EMERGENCY) only.
        # P11(d) — when telegram_enabled=False, the system-level URGENT path is also OFF.
        # The per-channel dispatcher's telegram (EMERGENCY only) takes over.
        if decision.delivery_tier == DeliveryTier.URGENT:
            if self._telegram_enabled:
                try:
                    text = self._telegram.render_text(
                        decision, fused,
                        primary_symbol=primary_symbol,
                        external_links=external_links,
                        contributing_signals=contributing,
                    )
                    await self._telegram.send(text)
                    delivered.append("telegram")
                except Exception as e:
                    logger.error("AlertRouter: telegram send failed: %s", e)
            else:
                logger.debug(
                    "AlertRouter: system-level telegram skipped "
                    "(telegram_enabled=False, using per-channel dispatcher)"
                )

        if delivered:
            logger.info(
                "AlertRouter: dispatched %s/%s via %s",
                primary_symbol, decision.recommended_action.value, delivered,
            )
        return delivered

    # ─────────────────────────────────────────────────────────────────
    # EMERGENCY heartbeat (called once per minute by a separate scheduled task)
    # ─────────────────────────────────────────────────────────────────
    async def emit_heartbeat_if_due(self, now: datetime | None = None) -> list[str]:
        """Send reminders for active EMERGENCYs that exceeded heartbeat_interval.

        P11(d) — when telegram_enabled=False, returns [] immediately (no-op).
        The per-channel dispatcher's 24h cooldown serves as a natural reminder
        (the same alert is re-sent with reason cooldown_expired).

        Returns:
            List of symbols whose reminders were sent.
        """
        if not self._telegram_enabled:
            return []
        now = now or datetime.now(timezone.utc)
        reminded: list[str] = []

        with self._lock:
            due_symbols = []
            for sym, info in self._active.items():
                if info.tier != Tier.EMERGENCY:
                    continue
                last = info.last_heartbeat or info.since
                if (now - last) >= self._heartbeat_interval:
                    due_symbols.append(sym)
            # Mark before async (await happens outside the lock)
            for sym in due_symbols:
                self._active[sym].last_heartbeat = now

        for sym in due_symbols:
            since_ts = self._active[sym].since
            duration = now - since_ts
            duration_str = self._humanize_duration(duration)
            text = (
                f"🚨🚨 <b>EMERGENCY heartbeat</b>\n"
                f"<b>Symbol:</b> {sym}\n"
                f"<b>Since:</b> {format_pdt(since_ts)} ({duration_str} ago)\n"
                f"\nState is still EMERGENCY. Recommend re-checking position."
            )
            try:
                await self._telegram.send(text)
                reminded.append(sym)
                logger.info("AlertRouter: heartbeat sent for %s (%s)", sym, duration_str)
            except Exception as e:
                logger.error("AlertRouter: heartbeat telegram failed for %s: %s", sym, e)
        return reminded

    # ─────────────────────────────────────────────────────────────────
    # Digest flush (called by the daemon's scheduled task at 06:00 Bay Area)
    # ─────────────────────────────────────────────────────────────────
    async def flush_digest_now(self) -> int:
        """Bundle accumulated digest entries into a single email and send.

        Returns:
            Number of entries sent (0 means nothing to send).
        """
        # P11(a).4.5 — if the email path is off, digest is meaningless (only drain the queue).
        if not self._email_enabled:
            entries = self._throttle.flush_digest()
            if entries:
                logger.info(
                    "AlertRouter: digest skipped (email_enabled=False), "
                    "dropped %d queued entries", len(entries),
                )
            return 0

        entries = self._throttle.flush_digest()
        if not entries:
            return 0

        # Simple v0: gather entries' symbols → one subject + body.
        symbols = [e.primary_symbol for e in entries]
        subject = f"📊 [WATCH digest] {len(entries)} signals — {', '.join(symbols[:5])}"
        if len(symbols) > 5:
            subject += f" ... (+{len(symbols)-5})"

        # body — concatenate per-entry mini-cards
        sections = []
        for e in entries:
            sections.append(self._email.render_html(
                e.decision, e.fused_event,
                contributing_signals=self._lookup_contributing(e.fused_event),
            ))
        digest_html = "<br>".join(sections)

        try:
            await self._email.send(subject, digest_html)
            logger.info("AlertRouter: digest email sent (%d entries)", len(entries))
            return len(entries)
        except Exception as e:
            logger.error("AlertRouter: digest email failed: %s", e)
            return 0

    # ─────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────
    def _lookup_contributing(self, fused: FusedAnomalyEvent) -> list[ChannelSignal]:
        """Look up the ChannelSignal ids in fused.contributing from the store.

        Skip ids not found. In actual v1 they should almost always be present.
        """
        sigs: list[ChannelSignal] = []
        for sid in fused.contributing:
            s = self._signal_store.get_channel_signal(sid)
            if s is not None:
                sigs.append(s)
        return sigs

    def _pick_primary_symbol(
        self,
        fused: FusedAnomalyEvent,
        contributing: list[ChannelSignal],
    ) -> str:
        """Pick the symbol of the contributing signal with highest tier + highest score as primary.

        Sort key (P9.3.P2.B):
          1st: tier rank — EMERGENCY > RISK_OFF > WATCH > NORMAL
          2nd: score    — within the same tier, prefer the stronger signal
                          → fixes the issue where slow-moving signals like
                            polymarket-iran would mask a fresh CME TV trigger.

        Returns "system" when contributing is empty.
        """
        if not contributing:
            return "system"
        # max key: (tier_rank, score) — both descending.
        top = max(contributing, key=lambda s: (s.tier.rank(), s.score))
        return top.symbol or "system"

    def _build_links(self, contributing: list[ChannelSignal]) -> dict[str, str]:
        """Build the external_links dict from contributing signals.

        Failed link builds (validation errors) are skipped — they do not block other links.
        """
        links: dict[str, str] = {}
        for s in contributing:
            try:
                if s.channel == CHANNEL_CME:
                    links[f"TradingView ({s.symbol})"] = link_builder.tradingview_chart(s.symbol)
                elif s.channel == CHANNEL_POLYMARKET:
                    links[f"Polymarket ({s.symbol})"] = link_builder.polymarket_market(s.symbol)
                elif s.channel == CHANNEL_HYPERLIQUID:
                    # Whether symbol is an asset name or a wallet — assume asset for now.
                    # In v1.1, distinguish by symbol prefix ("0x...").
                    links[f"Hyperliquid ({s.symbol})"] = link_builder.hyperliquid_asset(s.symbol)
                elif s.channel == CHANNEL_X:
                    # Assume the symbol holds a post URL (defined in P5).
                    if s.symbol.startswith("http"):
                        links[f"X post"] = link_builder.x_post(s.symbol)
            except (ValueError, Exception) as e:
                logger.debug("AlertRouter: link_builder skip for %s/%s: %s",
                              s.channel, s.symbol, e)
        return links

    def _update_active(
        self,
        symbol: str,
        state_change: tuple[Tier, Tier],
        ts: datetime,
        *,
        contributing: list[ChannelSignal] | None = None,
    ) -> None:
        """Update the active high-tier set based on the transition.

        Rules:
          - new == NORMAL  → clear all active across the system
          - escalation entering RISK_OFF/EMERGENCY → add/refresh that symbol
          - (P9.3.P2.B) on escalation, also add every RISK_OFF+ symbol from contributing
            → on simultaneous multi-channel firings, all show up in cross-tag
          - Otherwise (partial de-escalation, WATCH change) → remove that symbol

        Note: On de-escalation, contributing may be empty and primary_symbol
        falls back to "system". Explicitly check the escalation condition so
        that the symbol is not mistakenly added at RISK_OFF/EMERGENCY.
        """
        old, new = state_change
        is_escalation = new.rank() > old.rank()
        with self._lock:
            if new == Tier.NORMAL:
                self._active.clear()
            elif is_escalation and new in (Tier.RISK_OFF, Tier.EMERGENCY):
                if symbol not in self._active or self._active[symbol].tier != new:
                    self._active[symbol] = _ActiveTier(tier=new, since=ts)

                # (P9.3.P2.B) Also push every other RISK_OFF+ contributing into the active set.
                # Lets every channel that fired in the same cycle show up in the cross-tag body.
                # If already present at the same tier, keep `since` (don't overwrite).
                if contributing:
                    for sig in contributing:
                        if sig.tier not in (Tier.RISK_OFF, Tier.EMERGENCY):
                            continue
                        sym = sig.symbol or "system"
                        if sym == symbol:
                            continue  # primary already handled above
                        existing = self._active.get(sym)
                        if existing is None or existing.tier.rank() < sig.tier.rank():
                            # Newly added or tier escalated → refresh
                            self._active[sym] = _ActiveTier(tier=sig.tier, since=ts)
            else:
                self._active.pop(symbol, None)

    def _snapshot_active(self, exclude_symbol: str | None = None) -> dict[str, str]:
        """Snapshot the current active high-tier as {symbol: tier_value}.

        Excludes exclude_symbol (to prevent self cross-tag).
        """
        with self._lock:
            return {
                sym: info.tier.value
                for sym, info in self._active.items()
                if sym != exclude_symbol
            }

    @staticmethod
    def _humanize_duration(d: timedelta) -> str:
        """Human-friendly format for a timedelta (e.g. '1h 23m')."""
        total_s = int(d.total_seconds())
        h, rem = divmod(total_s, 3600)
        m = rem // 60
        if h > 0:
            return f"{h}h {m}m"
        return f"{m}m"

    # ─────────────────────────────────────────────────────────────────
    # Debug / health
    # ─────────────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "active_high_tier": {
                    sym: {"tier": info.tier.value, "since": info.since.isoformat()}
                    for sym, info in self._active.items()
                },
                "throttle": self._throttle.snapshot(),
            }


__all__ = ["AlertRouter"]
