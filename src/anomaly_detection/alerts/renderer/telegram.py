"""
alerts/renderer/telegram.py — Telegram bot message render + send.

────────────────────────────────────────────────────────────────────────
Role (architecture §6.5.1, §6.5.5):
  Telegram only receives EMERGENCY (D11) — so the inbox does not get
  buried. Keep the body short — only symbol + tier + 1-line action +
  a link to the email body. Steer the user to the email for full detail.

  dry_run=True mode supported: no actual HTTP requests, only capture
  into self.last_sent.

────────────────────────────────────────────────────────────────────────
Telegram bot HTTP API:
  POST https://api.telegram.org/bot<TOKEN>/sendMessage
  body: {"chat_id": "...", "text": "...", "parse_mode": "HTML"}

  HTML mode supports: <b>, <i>, <a href="...">, <code>, etc.
  Here we use only plain text + bold emphasis.

────────────────────────────────────────────────────────────────────────
Credentials (architecture §6.1):
  TELEGRAM_BOT_TOKEN — issued by @BotFather
  TELEGRAM_CHAT_ID   — your own chat id (numeric, look up via @userinfobot)

  On Cloud Run, store these in Secret Manager.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape

import httpx

from ..format_time import format_pdt
from ..intensity import intensity_label
from ...core.schemas import ChannelSignal, DecisionRecord, FusedAnomalyEvent, Tier

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramConfig:
    """Minimum config required to send via Telegram bot."""
    bot_token: str = ""
    chat_id: str = ""
    dry_run: bool = False
    timeout_s: float = 10.0


@dataclass
class SentTelegram:
    """Captured message in dry_run mode (for tests)."""
    text: str
    chat_id: str = ""
    sent_at_iso: str = ""


_TIER_EMOJI = {
    Tier.NORMAL: "✅",
    Tier.WATCH: "📊",
    Tier.RISK_OFF: "⚠️",
    Tier.EMERGENCY: "🚨🚨",
}


# ─────────────────────────────────────────────────────────────────────
# TelegramRenderer
# ─────────────────────────────────────────────────────────────────────
class TelegramRenderer:
    """Short plain text + email link. EMERGENCY only (the router filters)."""

    def __init__(self, config: TelegramConfig) -> None:
        self._cfg = config
        self.last_sent: list[SentTelegram] = []

    # ─────────────────────────────────────────────────────────────────
    # render
    # ─────────────────────────────────────────────────────────────────
    def render_text(
        self,
        decision: DecisionRecord,
        fused_event: FusedAnomalyEvent,
        primary_symbol: str | None = None,
        email_link: str | None = None,
        external_links: dict[str, str] | None = None,
        contributing_signals: list[ChannelSignal] | None = None,
    ) -> str:
        """Short HTML-style text (Telegram parse_mode=HTML).

        Args:
            decision: DecisionRecord (typically EMERGENCY).
            fused_event: related fused event (used for per_channel_tiers display).
            primary_symbol: main symbol (e.g. "CL"). Falls back to "system".
            email_link: link to the email body of the same alert (omitted if missing).
            external_links: deep links for 1-click visual checks
                (e.g. {"TradingView (CL)": "https://...", ...}).
            contributing_signals: ChannelSignals from the same cycle. If present,
                add a multi-channel block to the body, sorted by score, with
                intensity labels and key reason_codes on a single line each
                (P9.3.P2.B).

        Returns:
            Short HTML-style text — 5~12 lines.
        """
        sym = primary_symbol or "system"
        state = decision.state_change[1] if decision.state_change else fused_event.state
        emoji = _TIER_EMOJI[state]

        if decision.state_change:
            old, new = decision.state_change
            arrow = f"{old.value} → {new.value}"
        else:
            arrow = state.value

        # ── Per-channel block ──
        # (P9.3.P2.B) If contributing_signals is provided, sort by score, attach
        # intensity labels, and show the first reason_code on one line. Otherwise
        # use only fused_event.per_channel_tiers (legacy).
        channels_block = self._render_channels_block(
            fused_event=fused_event,
            contributing_signals=contributing_signals or [],
            primary_symbol=sym,
        )

        action = decision.recommended_action.value
        notes_short = decision.notes[:120]

        ts_str = format_pdt(decision.ts)

        text = (
            f"{emoji} <b>{escape(arrow)}</b>\n"
            f"<b>Primary:</b> {escape(sym)}\n"
            f"<b>Action:</b> <code>{escape(action)}</code>\n"
            f"<b>Detected:</b> {escape(ts_str)}\n"
            f"\n"
            f"<b>Channels:</b>\n{channels_block}\n"
            f"\n"
            f"{escape(notes_short)}"
        )

        # ── Deep-link section (Q3: visual verify) ──
        # On EMERGENCY, the user can 1-click open a chart and visually verify volume/price.
        # If too many external links (assume 5+ unlikely), show only the first 4 — protects against the Telegram message-length limit.
        if external_links:
            link_lines = []
            for label, url in list(external_links.items())[:4]:
                link_lines.append(f'🔍 <a href="{escape(url, quote=True)}">{escape(label)}</a>')
            text += "\n\n" + "\n".join(link_lines)

        if email_link:
            text += f'\n\n<a href="{escape(email_link, quote=True)}">📧 Full email</a>'
        return text

    # ─────────────────────────────────────────────────────────────────
    # helpers — multi-channel block (P9.3.P2.B)
    # ─────────────────────────────────────────────────────────────────
    def _render_channels_block(
        self,
        *,
        fused_event: FusedAnomalyEvent,
        contributing_signals: list[ChannelSignal],
        primary_symbol: str,
    ) -> str:
        """Render multi-channel block sorted by score, with intensity + reason.

        One line = one channel's RISK_OFF+ signal.
        Primary is marked with (★). Sort by (tier rank desc, score desc).

        legacy fallback: when contributing is empty, show one tier line per
        channel from fused.per_channel_tiers (same as before).
        """
        # legacy fallback — if no contributing signals, show only tier (same as before).
        if not contributing_signals:
            lines = []
            for name, tier in fused_event.per_channel_tiers.items():
                if tier.rank() > 0:
                    ce = _TIER_EMOJI[tier]
                    lines.append(f"  {ce} <b>{escape(name)}</b>: {escape(tier.value)}")
            return "\n".join(lines) if lines else "  —"

        # Show RISK_OFF+ only. Sort: highest tier first → highest score first.
        relevant = [
            s for s in contributing_signals
            if s.tier in (Tier.RISK_OFF, Tier.EMERGENCY)
        ]
        relevant.sort(key=lambda s: (s.tier.rank(), s.score), reverse=True)

        if not relevant:
            return "  —"

        lines: list[str] = []
        for s in relevant:
            ce = _TIER_EMOJI[s.tier]
            sym = s.symbol or "system"
            star = " ★" if sym == primary_symbol else ""
            label = intensity_label(s.score)
            # First 1~2 reason_codes only — protect against the Telegram message-length limit.
            #   Too many becomes noise. Summarize the key trigger metric in one line.
            reason_short = ", ".join(s.reason_codes[:2]) if s.reason_codes else ""
            reason_part = f"\n      <code>{escape(reason_short)}</code>" if reason_short else ""
            lines.append(
                f"  {ce} <b>{escape(s.channel)}</b> "
                f"({escape(sym)}){star} — "
                f"score={s.score:.2f} <i>({escape(label)})</i>"
                f"{reason_part}"
            )
        return "\n".join(lines)

    # ─────────────────────────────────────────────────────────────────
    # send
    # ─────────────────────────────────────────────────────────────────
    async def send(self, text: str) -> None:
        """Send via the Telegram bot HTTP API. With dry_run=True, only capture.

        Raises:
            RuntimeError: when dry_run=False but bot_token/chat_id is missing.
            httpx.HTTPError: when the API call fails (caller retries).
        """
        sent = SentTelegram(
            text=text,
            chat_id=self._cfg.chat_id,
            sent_at_iso=datetime.now(timezone.utc).isoformat(),
        )

        if self._cfg.dry_run:
            self.last_sent.append(sent)
            logger.info("TelegramRenderer: DRY_RUN — captured (%d chars)", len(text))
            return

        if not (self._cfg.bot_token and self._cfg.chat_id):
            raise RuntimeError(
                "TelegramRenderer.send: bot_token/chat_id missing. "
                "Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID or use dry_run=True."
            )

        url = f"https://api.telegram.org/bot{self._cfg.bot_token}/sendMessage"
        payload = {
            "chat_id": self._cfg.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        async with httpx.AsyncClient(timeout=self._cfg.timeout_s) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram API returned not-ok: {data}")

        self.last_sent.append(sent)
        logger.info("TelegramRenderer: sent (%d chars) to chat %s", len(text), self._cfg.chat_id)


__all__ = ["TelegramRenderer", "TelegramConfig", "SentTelegram"]
