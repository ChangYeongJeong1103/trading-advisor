"""
alerts/renderer/email.py — Email render + send (Gmail SMTP).

────────────────────────────────────────────────────────────────────────
Role:
  Take a DecisionRecord (+ context) and produce the HTML email body /
  subject, then send via Gmail SMTP. Render and send are separated —
  render is pure (easy to test).

  dry_run=True mode: skip SMTP entirely and just store into self.last_sent.
  → unit tests can run without SMTP credentials.

────────────────────────────────────────────────────────────────────────
Subject prefix (architecture §6.5.1):

  Enter EMERGENCY        → "🚨🚨 [EMERGENCY] {symbol} — {action}"
  Enter RISK_OFF         → "⚠️ [RISK_OFF] {symbol} — {action}"
  Enter WATCH (digest)   → "📊 [WATCH digest] {symbols...}"
  De-escalate to NORMAL  → "✅ [ALL CLEAR] System back to NORMAL"
  Partial de-escalation  → "📉 [DE-ESCALATED] {old}→{new} {symbol}"

  Cross-tag suffix (§6.5.5):
    If a higher tier is active — " [🚨 EMERGENCY (BTC) ACTIVE]"

────────────────────────────────────────────────────────────────────────
HTML body structure (architecture §6.5.2):

  Header bar  — tier color + one-line summary
  Action      — recommended_action
  Notes       — human-readable rationale
  Per-channel — each channel's tier / score / reason codes (table)
  Links       — link_builder output (visual check)
  Cross-tag   — box shown when other higher tiers are active
  Footer      — decision ID / policy version / ts (audit)

  Inline CSS — Gmail ignores <style>. Only style="..." is safe.

────────────────────────────────────────────────────────────────────────
Credentials (architecture §6.1):
  local      → SMTP_* variables in .env
  Cloud Run  → Secret Manager (injected as env vars)

  For Gmail you need an "App Password" (not your regular password).
  https://myaccount.google.com/apppasswords
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

from ..format_time import format_pdt
from ..intensity import intensity_label
from ...core.schemas import (
    ChannelSignal,
    DecisionRecord,
    DeliveryTier,
    FusedAnomalyEvent,
    Tier,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Config (small dataclass — only the SMTP fields from secrets)
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class EmailConfig:
    """Minimum config required to send via Gmail SMTP."""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""           # me@gmail.com
    smtp_password: str = ""       # Gmail App Password
    smtp_from: str = ""           # usually equals smtp_user
    smtp_to: str = ""             # destination for alerts
    dry_run: bool = False         # When True, capture instead of going through SMTP


# ─────────────────────────────────────────────────────────────────────
# Tier colors / emojis
# ─────────────────────────────────────────────────────────────────────
_TIER_COLOR = {
    Tier.NORMAL: "#388e3c",     # green
    Tier.WATCH: "#fbc02d",      # yellow
    Tier.RISK_OFF: "#f57c00",   # orange
    Tier.EMERGENCY: "#d32f2f",  # red
}

_TIER_EMOJI = {
    Tier.NORMAL: "✅",
    Tier.WATCH: "📊",
    Tier.RISK_OFF: "⚠️",
    Tier.EMERGENCY: "🚨🚨",
}


@dataclass
class SentEmail:
    """Captured email in dry_run mode (for tests)."""
    subject: str
    html: str
    to: str = ""
    sent_at_iso: str = ""


# ─────────────────────────────────────────────────────────────────────
# EmailRenderer
# ─────────────────────────────────────────────────────────────────────
class EmailRenderer:
    """SMTP-based email send + HTML render."""

    def __init__(self, config: EmailConfig) -> None:
        self._cfg = config
        # Accumulates emails sent in dry_run mode (for test verification)
        self.last_sent: list[SentEmail] = []

    # ─────────────────────────────────────────────────────────────────
    # Subject
    # ─────────────────────────────────────────────────────────────────
    def make_subject(
        self,
        decision: DecisionRecord,
        primary_symbol: str | None = None,
        active_higher_tier: dict[str, str] | None = None,
    ) -> str:
        """Build the one-line subject (architecture §6.5.1)."""
        active_higher_tier = active_higher_tier or {}
        sym = primary_symbol or "system"
        action = decision.recommended_action.value

        # 1) Base subject — driven by state_change
        if decision.state_change is None:
            base = f"📋 [AUDIT] No state change ({decision.notes[:60]})"
        else:
            old, new = decision.state_change
            if new.rank() > old.rank():
                # Escalation
                emoji = _TIER_EMOJI[new]
                if decision.delivery_tier == DeliveryTier.DIGEST:
                    base = f"{emoji} [WATCH digest] {sym}"
                else:
                    base = f"{emoji} [{new.value}] {sym} — {action}"
            elif new == Tier.NORMAL:
                base = f"✅ [ALL CLEAR] System back to NORMAL ({old.value} resolved)"
            else:
                base = f"📉 [DE-ESCALATED] {old.value}→{new.value} {sym}"

        # 2) Cross-tag suffix (§6.5.5) — always notify when another high-tier is active
        if active_higher_tier:
            tags = ", ".join(
                f"{tier.upper()} ({s})" for s, tier in active_higher_tier.items()
            )
            base += f"  [🚨 {tags} ACTIVE]"

        return base

    # ─────────────────────────────────────────────────────────────────
    # HTML body
    # ─────────────────────────────────────────────────────────────────
    def render_html(
        self,
        decision: DecisionRecord,
        fused_event: FusedAnomalyEvent,
        contributing_signals: list[ChannelSignal] | None = None,
        external_links: dict[str, str] | None = None,
        recent_30min: list | None = None,  # noqa: ARG002 — TODO P9 timeline view
        active_higher_tier: dict[str, str] | None = None,
    ) -> str:
        """Build the email HTML body. Pure function — easy to test."""
        contributing_signals = contributing_signals or []
        external_links = external_links or {}
        active_higher_tier = active_higher_tier or {}

        state = decision.state_change[1] if decision.state_change else fused_event.state
        color = _TIER_COLOR[state]
        emoji = _TIER_EMOJI[state]

        if decision.state_change:
            old, new = decision.state_change
            header_text = f"{emoji} {old.value} → {new.value}"
        else:
            header_text = f"{emoji} {state.value} (audit)"

        # ── Per-channel rows (P9.3.P2.B: sorted by tier rank desc → score desc) ──
        # After sorting, add an intensity-label column — strong signals like "way over" sit on top.
        sig_by_channel: dict[str, ChannelSignal] = {s.channel: s for s in contributing_signals}

        # Sort key: (tier rank, score) desc. Pull from fused.per_channel_tiers.
        ordered_channels = sorted(
            fused_event.per_channel_tiers.items(),
            key=lambda kv: (
                kv[1].rank(),
                fused_event.per_channel_scores.get(kv[0], 0.0),
            ),
            reverse=True,
        )

        rows = []
        for ch_name, ch_tier in ordered_channels:
            sig = sig_by_channel.get(ch_name)
            score = fused_event.per_channel_scores.get(ch_name, 0.0)
            tier_color = _TIER_COLOR[ch_tier]
            reason_codes = ", ".join(sig.reason_codes) if sig and sig.reason_codes else "—"
            symbol = sig.symbol if sig else "—"
            direction = sig.direction.value if sig else "—"
            # Intensity label — only meaningful for RISK_OFF+. Otherwise show "—".
            if ch_tier in (Tier.RISK_OFF, Tier.EMERGENCY):
                label = intensity_label(score)
            else:
                label = "—"
            rows.append(
                f"""
                <tr>
                  <td style="padding:6px 10px; border-bottom:1px solid #eee;">{escape(ch_name)}</td>
                  <td style="padding:6px 10px; border-bottom:1px solid #eee; color:{tier_color}; font-weight:bold;">{escape(ch_tier.value)}</td>
                  <td style="padding:6px 10px; border-bottom:1px solid #eee; text-align:right;">{score:.2f}</td>
                  <td style="padding:6px 10px; border-bottom:1px solid #eee; color:#555;"><i>{escape(label)}</i></td>
                  <td style="padding:6px 10px; border-bottom:1px solid #eee;">{escape(symbol)}</td>
                  <td style="padding:6px 10px; border-bottom:1px solid #eee;">{escape(direction)}</td>
                  <td style="padding:6px 10px; border-bottom:1px solid #eee; color:#666;">{escape(reason_codes)}</td>
                </tr>
                """
            )
        per_channel_table = (
            "<table style='width:100%; border-collapse:collapse; font-size:13px;'>"
            "<thead><tr>"
            "<th style='text-align:left; padding:6px 10px; background:#f5f5f5;'>Channel</th>"
            "<th style='text-align:left; padding:6px 10px; background:#f5f5f5;'>Tier</th>"
            "<th style='text-align:right; padding:6px 10px; background:#f5f5f5;'>Score</th>"
            "<th style='text-align:left; padding:6px 10px; background:#f5f5f5;'>Intensity</th>"
            "<th style='text-align:left; padding:6px 10px; background:#f5f5f5;'>Symbol</th>"
            "<th style='text-align:left; padding:6px 10px; background:#f5f5f5;'>Dir</th>"
            "<th style='text-align:left; padding:6px 10px; background:#f5f5f5;'>Reason codes</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )

        # ── Links ──
        if external_links:
            link_items = "".join(
                f'<li><a href="{escape(url, quote=True)}" style="color:#1565c0;">{escape(label)}</a></li>'
                for label, url in external_links.items()
            )
            links_html = f"<ul style='padding-left:20px;'>{link_items}</ul>"
        else:
            links_html = "<p style='color:#888; font-size:12px;'>No external links.</p>"

        # ── Cross-tag warning ──
        if active_higher_tier:
            warnings = "<br>".join(
                f"🚨 <strong>{escape(tier.upper())}</strong> active on <strong>{escape(s)}</strong>"
                for s, tier in active_higher_tier.items()
            )
            cross_tag_html = (
                f"<div style='background:#ffebee; border-left:4px solid #d32f2f; "
                f"padding:10px 14px; margin:14px 0; font-size:13px;'>{warnings}</div>"
            )
        else:
            cross_tag_html = ""

        # ── Boost note ──
        boost_html = ""
        if fused_event.boost_applied:
            boost_html = (
                f"<p style='margin:6px 0; color:#666; font-size:13px;'>"
                f"<strong>Boost:</strong> {escape(fused_event.boost_applied)}</p>"
            )

        return f"""<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif; background:#f5f5f5; margin:0; padding:20px;">
  <table cellpadding="0" cellspacing="0" style="background:white; max-width:680px; width:100%; margin:0 auto; border-radius:6px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.1);">
    <tr>
      <td style="background:{color}; color:white; padding:18px 24px; font-size:18px; font-weight:bold;">
        {escape(header_text)}
      </td>
    </tr>
    <tr><td style="padding:20px 24px;">
      <p style="margin:0 0 8px 0; font-size:15px;"><strong>Recommended action:</strong> {escape(decision.recommended_action.value)}</p>
      <p style="margin:6px 0; color:#444;">{escape(decision.notes)}</p>
      <p style="margin:6px 0; font-size:13px; color:#666;"><strong>Fused score:</strong> {fused_event.fused_score:.3f} &nbsp;|&nbsp; <strong>tier_floor:</strong> {escape(fused_event.tier_floor.value)}</p>
      {boost_html}
      {cross_tag_html}
      <h3 style="margin:18px 0 8px 0; font-size:14px; color:#333;">Per-channel</h3>
      {per_channel_table}
      <h3 style="margin:18px 0 8px 0; font-size:14px; color:#333;">Links</h3>
      {links_html}
      <hr style="border:none; border-top:1px solid #eee; margin:18px 0;">
      <p style="font-size:11px; color:#999; margin:0;">
        Decision ID: {escape(decision.id)} &nbsp;|&nbsp; Policy: {escape(decision.policy_version)} &nbsp;|&nbsp; {escape(format_pdt(decision.ts))}
      </p>
    </td></tr>
  </table>
</body></html>"""

    # ─────────────────────────────────────────────────────────────────
    # SMTP send
    # ─────────────────────────────────────────────────────────────────
    async def send(self, subject: str, html: str) -> None:
        """Send via SMTP. With dry_run=True, only capture into self.last_sent.

        Raises:
            RuntimeError: when dry_run=False but SMTP credentials are missing.
            smtplib.SMTPException: when the actual send fails (caller retries).
        """
        from datetime import datetime, timezone

        sent = SentEmail(
            subject=subject, html=html,
            to=self._cfg.smtp_to,
            sent_at_iso=datetime.now(timezone.utc).isoformat(),
        )

        if self._cfg.dry_run:
            self.last_sent.append(sent)
            logger.info("EmailRenderer: DRY_RUN — captured: %s", subject[:80])
            return

        # Actual send — verify credentials
        if not (self._cfg.smtp_user and self._cfg.smtp_password
                and self._cfg.smtp_from and self._cfg.smtp_to):
            raise RuntimeError(
                "EmailRenderer.send: SMTP credentials missing. "
                "Set smtp_user / smtp_password / smtp_from / smtp_to or use dry_run=True."
            )

        # smtplib is sync — wrap with to_thread for async
        await asyncio.to_thread(self._send_smtp_sync, subject, html)
        self.last_sent.append(sent)
        logger.info("EmailRenderer: sent — %s", subject[:80])

    def _send_smtp_sync(self, subject: str, html: str) -> None:
        """Blocking SMTP — invoked inside to_thread."""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self._cfg.smtp_from
        msg["To"] = self._cfg.smtp_to
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP(self._cfg.smtp_host, self._cfg.smtp_port, timeout=15) as s:
            s.starttls()
            s.login(self._cfg.smtp_user, self._cfg.smtp_password)
            s.sendmail(self._cfg.smtp_from, [self._cfg.smtp_to], msg.as_string())


__all__ = ["EmailRenderer", "EmailConfig", "SentEmail"]
