"""
alerts/renderer/channel_email.py — Per-channel alert email renderer
(P11(a).2 — production wiring entry point).

────────────────────────────────────────────────────────────────────────
Role:
  One ChannelSignal that passed cooldown (channel_alerts.py v2) = one email.
  Per user decision, system-level fusion emails are not used in v1 production;
  alerts are received per channel.

  flow:
    ChannelSignal (passed cooldown)
        ↓ render_email(signal, ...)
    RenderedEmail (subject + html + 3 inline images)
        ↓ send_email(rendered, smtp_config)
    Gmail SMTP   |  or dry-run capture

  Three inline images:
    1) `cid:logo_<channel>`  — channel brand logo (assets/anomaly/channel_logos/)
    2) `cid:plot_60m`         — last 1h timeline plot relative to alert_clock
    3) `cid:plot_360m`        — last 6h timeline plot relative to alert_clock

────────────────────────────────────────────────────────────────────────
HTML body structure (mobile / Gmail-compatible inline CSS):

  ┌─────────────────────────────────────────────────────┐
  │ [tier color bar] {logo_img} {tier_prefix} {CHANNEL_SHORT} │
  │                  · {symbol} → {TIER}                       │
  ├─────────────────────────────────────────────────────┤
  │ alert metadata table:                                       │
  │   alert_clock    : 2026-04-17 12:25 UTC (PT 05:25)          │
  │   tier           : EMERGENCY                                │
  │   fired_detectors: vol_z_v1, oi_z_v1                        │
  │   reason_codes   : VOL_Z=4.21, OI_DELTA=...                 │
  │   score          : 0.97                                     │
  │   direction      : down                                     │
  │   cooldown reason: initial / escalation(prev=RISK_OFF)      │
  │                                                             │
  │ ─── Last 1 hour timeline ───────────────────────────          │
  │ [inline plot_60m PNG]                                       │
  │                                                             │
  │ ─── Last 6 hours timeline ──────────────────────────          │
  │ [inline plot_360m PNG]                                      │
  │                                                             │
  │ footer: alert_id · cooldown 24h · sent_at                   │
  └─────────────────────────────────────────────────────────────┘

────────────────────────────────────────────────────────────────────────
Environment variables (Q4 lock = a):

  · `EMAIL_DRY_RUN`        — If "true" (default), capture without SMTP.
                             Send for real only when "false".
  · `SMTP_HOST`            — default "smtp.gmail.com"
  · `SMTP_PORT`            — default "587"
  · `SMTP_USER`            — Gmail address
  · `SMTP_PASSWORD`        — Gmail App Password
  · `SMTP_FROM`            — usually same as SMTP_USER
  · `SMTP_TO`              — notification recipient emails; comma-separated values are OK

  → In the P11(a).6 sample test step, put real SMTP credentials in .env and
    toggle dry_run=false once.
"""

from __future__ import annotations

# ── Standard library ─────────────────────────────────────────────────
import asyncio
import logging
import os
import smtplib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path

# ── Local ─────────────────────────────────────────────────────────────
from ..alert_ohlc_buffer import AlertOhlcBuffer
from ..llm_assessor import AlertAssessment
from ..subject_template import build_alert_subject, channel_short
from ...core.schemas import CHANNEL_TRUTH_SOCIAL, ChannelSignal, Tier
from ...replay.schemas import ReplayResult
from .channel_x_post import (
    friendly_detector_list,
    render_analysis,
    render_detector_lines,
    render_footnote_parts,
    symbol_with_friendly,
)
from .email_plot import (
    DEFAULT_VOLUME_UNIT,
    EMAIL_STACK_WINDOWS,
    infer_detector_bucket_minutes,
    write_alert_window_plot,
)
from .inline_assets import (
    build_inline_image_parts,
    channel_avatar_cid,
    channel_avatar_path,
    channel_logo_cid,
    channel_logo_path,
    plot_cid,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Tier → header bar color (same palette as the existing email.py).
# ─────────────────────────────────────────────────────────────────────
_TIER_COLOR: dict[Tier, str] = {
    Tier.NORMAL:    "#388e3c",   # green (unused because alerts do not arrive)
    Tier.WATCH:     "#fbc02d",   # yellow
    Tier.RISK_OFF:  "#f57c00",   # orange
    Tier.EMERGENCY: "#d32f2f",   # red
}


# ─────────────────────────────────────────────────────────────────────
# Config / output dataclass.
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SmtpConfig:
    """Minimum settings required for Gmail SMTP sending.

    In production, `SmtpConfig.from_env()` fills this automatically from .env.
    """
    host: str = "smtp.gmail.com"
    port: int = 587
    user: str = ""              # Gmail address
    password: str = ""          # Gmail App Password, not the normal password
    sender: str = ""            # Usually same as user
    recipients: tuple[str, ...] = ()   # Multiple recipients are OK, comma-separated
    dry_run: bool = True        # Default True for safety (user Q4 = a)
    timeout_s: int = 15

    @classmethod
    def from_env(cls) -> SmtpConfig:
        """Build SmtpConfig after environment variables are loaded from `.env`.

        Empty env vars become empty strings. dry_run is True unless
        EMAIL_DRY_RUN == "false", which is the safe default.
        """
        recipients_str = os.environ.get("SMTP_TO", "")
        recipients = tuple(
            x.strip() for x in recipients_str.split(",") if x.strip()
        )
        dry_run_env = os.environ.get("EMAIL_DRY_RUN", "true").strip().lower()
        return cls(
            host=os.environ.get("SMTP_HOST", "smtp.gmail.com"),
            port=int(os.environ.get("SMTP_PORT", "587")),
            user=os.environ.get("SMTP_USER", ""),
            password=os.environ.get("SMTP_PASSWORD", ""),
            sender=os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", "")),
            recipients=recipients,
            dry_run=(dry_run_env != "false"),
        )


@dataclass
class RenderedEmail:
    """Result of render_email; pass to send_email or dump as .eml.

    `inline_images`: (cid, png_path) pairs used to fill cid references inside
        the HTML. Includes both `plots/` and the logo. send_email converts them
        to MIMEImage parts.
    """
    subject: str
    html: str
    inline_images: list[tuple[str, Path]] = field(default_factory=list)
    alert_id: str = ""             # uuid4 for audit
    rendered_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass
class SentEmail:
    """Capture for both dry_run and real sends, used for test verification."""
    subject: str
    to: tuple[str, ...]
    sent_at_iso: str
    dry_run: bool


# ─────────────────────────────────────────────────────────────────────
# Render — pure and easy to test.
# ─────────────────────────────────────────────────────────────────────
def render_email(
    signal: ChannelSignal,
    *,
    replay_result: ReplayResult,
    plot_dir: Path,
    cooldown_reason: str = "initial",
    cooldown_minutes: int = 1440,
    llm_assessment: AlertAssessment | None = None,
    ohlc_buffer: AlertOhlcBuffer | None = None,
) -> RenderedEmail:
    """1 ChannelSignal → 1 RenderedEmail (subject + html + attachments).

    Args:
        signal: One alert that passed cooldown, with channel/symbol/tier/score
            already populated.
        replay_result: ReplayResult containing timeline data for the same channel.
            In production, the daemon's in-memory rolling buffer creates an
            equivalent object. This is the P11(a).4 dispatcher responsibility.
        plot_dir: Directory where the 1h / 6h plot PNGs are written. The
            dispatcher creates and passes a unique directory per alert, such as
            data/anomaly/alerts/<alert_id>/.
        cooldown_reason: Result from channel_alerts.py _decide_emit, such as:
            'initial' / 'escalation(prev=RISK_OFF)' / 'cooldown_expired').
            Rendered in the email body footer.
        cooldown_minutes: Cooldown duration in minutes shown in the footer.
            Defaults to 1440.
        llm_assessment: P11(b).4 result from LLMAlertAssessor, provided by the
            dispatcher only for EMERGENCY alerts. If present, it is rendered as
            the final metadata-table row ("LLM Assess"). If None, the row is
            omitted. RISK_OFF/WATCH always pass None to save cost.

    Returns:
        RenderedEmail. The caller, send_email, sends it through SMTP unchanged.

    Note:
        One side effect: writes 1h / 6h PNG files to plot_dir. Because
        write_alert_window_plot uses atomic writes (tmp → rename), partial output
        is not left behind.
    """
    plot_dir.mkdir(parents=True, exist_ok=True)
    alert_id = uuid.uuid4().hex[:12]

    # ── 1) Subject — emoji + tier prefix + channel short. ────────────
    # Add the friendly name to symbol so it appears in the subject too.
    subject = build_alert_subject(
        channel=signal.channel,
        symbol=symbol_with_friendly(signal.symbol),
        tier=signal.tier,
    )

    # ── 2) Two plots (1h / 6h) — Q1 = c, stacked in one email. ───────
    inline_images: list[tuple[str, Path]] = []

    # Logo: attach when available; inline_assets silently skips missing assets.
    logo_path = channel_logo_path(signal.channel)
    if logo_path is not None:
        inline_images.append((channel_logo_cid(signal.channel), logo_path))

    # Avatar: currently truth_social only, using a Trump face PNG.
    # User decision 2026-05-15: inline the Trump avatar next to the purple T logo
    # so the user can instantly identify the email as a Trump Truth Social post.
    avatar_path = channel_avatar_path(signal.channel)
    if avatar_path is not None:
        inline_images.append((channel_avatar_cid(signal.channel), avatar_path))

    # OHLCV bars / first_bar_ts. If there is no buffer, for tests or old callers,
    # pass None. The plot writer shows placeholders in Panel 1/2 for None or empty lists.
    volume_unit_label = DEFAULT_VOLUME_UNIT.get(
        signal.channel.lower(), "size",
    )
    first_bar_ts = (
        ohlc_buffer.first_bar_ts(channel=signal.channel, symbol=signal.symbol)
        if ohlc_buffer is not None else None
    )

    # v0.7.17: group plot bars to match the fired detector's evaluation window.
    # CME insider_v1 uses INSIDER_V1_BUCKET=Xmin (1/2/5) in reason_codes.
    # Polymarket / Hyperliquid map detector names to 1 or 5 minutes.
    # If several detectors fire at once, the longest window is selected.
    bucket_minutes = infer_detector_bucket_minutes(
        fired_detectors=list(signal.fired_detectors),
        reason_codes=list(signal.reason_codes),
    )

    for win in EMAIL_STACK_WINDOWS:  # (60, 360)
        plot_path = plot_dir / f"plot_{win}m.png"
        # Bar slice for this window; use an empty list when there is no buffer.
        bars = (
            ohlc_buffer.bars(
                channel=signal.channel,
                symbol=signal.symbol,
                since=signal.ts - timedelta(minutes=win),
                until=signal.ts,
            )
            if ohlc_buffer is not None else []
        )
        write_alert_window_plot(
            replay_result, plot_path,
            alert_clock=signal.ts,
            window_minutes=win,
            alert_tier=signal.tier,
            alert_channel=signal.channel,
            alert_symbol=signal.symbol,
            bars=bars,
            first_bar_ts=first_bar_ts,
            volume_unit_label=volume_unit_label,
            bucket_minutes=bucket_minutes,
        )
        inline_images.append((plot_cid(win), plot_path))

    # ── 2.5) truth_social branch: its own LLM scorer already computed
    # insider_concern_score, so do not call LLMAlertAssessor again. Convert
    # INSIDER_SUSPICION / INSIDER_NOTE / ANALYSIS from reason_codes directly into
    # AlertAssessment and render it in the same "Insider-trading suspicion" row.
    effective_assessment = llm_assessment
    if signal.channel == CHANNEL_TRUTH_SOCIAL and effective_assessment is None:
        effective_assessment = _assessment_from_truth_social_codes(
            list(signal.reason_codes),
        )

    # ── 3) HTML body. ─────────────────────────────────────────────────
    html = _build_html(
        signal=signal,
        cooldown_reason=cooldown_reason,
        cooldown_minutes=cooldown_minutes,
        alert_id=alert_id,
        logo_cid=channel_logo_cid(signal.channel),
        avatar_cid=(
            channel_avatar_cid(signal.channel)
            if avatar_path is not None else None
        ),
        plot_cid_1h=plot_cid(EMAIL_STACK_WINDOWS[0]),
        plot_cid_6h=plot_cid(EMAIL_STACK_WINDOWS[1]),
        replay_event_id=replay_result.event.event_id,
        llm_assessment=effective_assessment,
    )

    return RenderedEmail(
        subject=subject,
        html=html,
        inline_images=inline_images,
        alert_id=alert_id,
    )


# ─────────────────────────────────────────────────────────────────────
# HTML body builder — inline CSS only because Gmail ignores <style>.
# ─────────────────────────────────────────────────────────────────────
def _build_html(
    *,
    signal: ChannelSignal,
    cooldown_reason: str,
    cooldown_minutes: int,
    alert_id: str,
    logo_cid: str,
    avatar_cid: str | None,
    plot_cid_1h: str,
    plot_cid_6h: str,
    replay_event_id: str,
    llm_assessment: AlertAssessment | None = None,
) -> str:
    """HTML body using inline CSS only, safe for Gmail."""
    bar_color = _TIER_COLOR[signal.tier]
    # Do not put emoji in the body header because the inline PNG logo already
    # identifies the channel. In the subject, keep emoji because PNG cannot be embedded.
    # Channels with an empty CHANNEL_SHORT, such as X, display only the symbol.
    ch_short = channel_short(signal.channel)
    # User decision 2026-05-03: a ticker alone can be unclear externally, so show
    # the friendly name in parentheses too. If no mapping exists, show only ticker.
    symbol_display = symbol_with_friendly(signal.symbol)
    title_text = (
        f"{ch_short} · {symbol_display} → {signal.tier.value}"
        if ch_short
        else f"{symbol_display} → {signal.tier.value}"
    )

    # Logo image inside the header: user lock 2026-04-21, 36 × 36 px.
    # Some clients, such as Mail.app preview, can render the PNG at natural size
    # when only the width attribute is set, so force width/height/max-width in
    # inline style as well.
    logo_img_html = (
        f'<img src="cid:{escape(logo_cid)}" alt="{escape(signal.channel)}" '
        f'width="36" height="36" '
        f'style="width:36px; height:36px; max-width:36px; '
        f'vertical-align:middle; margin-right:10px; '
        f'border-radius:4px; background:white;">'
    )

    # Extra avatar: currently truth_social only, using Trump's face.
    # User decision 2026-05-15: show the Trump avatar next to the purple T logo.
    # Both images, logo (36px) and avatar (44px), appear on the left of the header bar.
    avatar_img_html = ""
    if avatar_cid:
        avatar_img_html = (
            f'<img src="cid:{escape(avatar_cid)}" alt="trump-avatar" '
            f'width="44" height="44" '
            f'style="width:44px; height:44px; max-width:44px; '
            f'vertical-align:middle; margin-right:12px; '
            f'border-radius:50%; background:white; '
            f'border:2px solid #ffffff;">'
        )

    # ── Same structure as X-post: Detectors block (plain language + formula),
    #    plus Analysis and Scale. Merge fired_detectors / reason_codes rows so
    #    external users and operators see the same representation. The single
    #    source of truth is the channel_x_post helpers.
    detector_lines_list = render_detector_lines(signal.reason_codes)
    detector_block_html = "<br>".join(
        f"- {escape(line)}" for line in detector_lines_list
    ) if detector_lines_list else "—"
    analysis_text = render_analysis(signal.reason_codes)
    # User decision 2026-05-03: split footnote labels by meaning.
    #   DIR_IMB → "Imbalance",  WC → "Concentration".
    footnote_label, footnote_body = render_footnote_parts(signal.reason_codes)
    # v0.7.12: convert fired_detectors to user-facing labels.
    # Internal codes, such as cme_insider_v1, stay unchanged in audit/log output;
    # only the email display uses expanded names such as
    # "multi-signal insider pattern (CME)".
    fired_names_text = friendly_detector_list(signal.fired_detectors)

    # User decision 2026-05-03:
    #   · Move fired_detectors row above Detectors (raw label → friendly order).
    #   · Keep font size/color the same as other rows (font-size:13px / color:#777).
    #   · Hide Analysis / Scale rows entirely when empty, so "—" is not shown.
    fired_detectors_row_html = (
        f"""<tr>
          <td style="padding:5px 8px; color:#777; \
border-bottom:1px solid #f0f0f0;">fired_detectors</td>
          <td style="padding:5px 8px; \
border-bottom:1px solid #f0f0f0;">{escape(fired_names_text)}</td>
        </tr>"""
    )
    analysis_row_html = (
        f"""<tr>
          <td style="padding:5px 8px; color:#777; \
border-bottom:1px solid #f0f0f0;">Analysis</td>
          <td style="padding:5px 8px; \
border-bottom:1px solid #f0f0f0;">{escape(analysis_text)}</td>
        </tr>"""
        if analysis_text
        else ""
    )
    scale_row_html = (
        f"""<tr>
          <td style="padding:5px 8px; color:#777; \
border-bottom:1px solid #f0f0f0;">{escape(footnote_label)}</td>
          <td style="padding:5px 8px; color:#666; \
border-bottom:1px solid #f0f0f0;">{escape(footnote_body)}</td>
        </tr>"""
        if footnote_body
        else ""
    )

    # alert_clock display uses PT only, matching the user's LA/SF location
    # (user decision 2026-04-23). ZoneInfo("America/Los_Angeles") handles PST/PDT
    # automatically, so the display label also becomes "PDT"/"PST" by timestamp
    # via strftime("%Z").
    alert_ts_pt = signal.ts.astimezone(ZoneInfo("America/Los_Angeles")).strftime(
        "%Y-%m-%d %H:%M %Z",
    )

    plot_img_style = (
        "display:block; max-width:100%; height:auto; "
        "margin:8px auto; border:1px solid #eee; border-radius:4px;"
    )

    # ── P11(b).4 + P12-C: add the LLM analysis row only for EMERGENCY.
    # Show only Insider-trading suspicion (3 to 5 bullets).
    # NOTE (changed 2026-05-14): previously this also showed a "Trader's view"
    # row, but it was removed across all channels (X / Email / Telegram) because
    # the user decided the insider analysis is sufficient. The LLM still produces
    # market_bullets because the cost is negligible and optionality is useful, but
    # they are not rendered.
    if llm_assessment is not None:
        if llm_assessment.score >= 7:
            assess_color = "#d32f2f"   # red — strong insider similarity
        elif llm_assessment.score >= 4:
            assess_color = "#f57c00"   # orange — partial
        else:
            assess_color = "#666666"   # gray — weak / unrelated

        insider_bullets_html = "<br>".join(
            f"- {escape(b)}" for b in llm_assessment.insider_bullets
        )
        assess_row_html = f"""
        <tr>
          <td style="padding:7px 8px; color:#777; \
vertical-align:top;">\
Insider-trading suspicion</td>
          <td style="padding:7px 8px; line-height:1.5;">
            <span style="color:{assess_color}; font-weight:bold;">\
{llm_assessment.score}/10</span><br>
            {insider_bullets_html}
          </td>
        </tr>"""
    else:
        assess_row_html = ""

    return f"""<!DOCTYPE html>
<html><body style="font-family:Arial,Helvetica,sans-serif; \
background:#f5f5f5; margin:0; padding:18px;">
  <table cellpadding="0" cellspacing="0" border="0" \
style="background:#ffffff; max-width:760px; width:100%; margin:0 auto; \
border-radius:8px; overflow:hidden; box-shadow:0 2px 10px rgba(0,0,0,0.08);">
    <tr>
      <td style="background:{bar_color}; color:#ffffff; padding:16px 22px; \
font-size:18px; font-weight:bold; line-height:1.3;">
        {logo_img_html}{avatar_img_html}
        <span style="vertical-align:middle;">{escape(title_text)}</span>
      </td>
    </tr>

    <tr><td style="padding:18px 22px;">
      <h3 style="margin:0 0 12px 0; font-size:14px; color:#333;">Alert metadata</h3>
      <table cellpadding="0" cellspacing="0" border="0" \
style="width:100%; font-size:13px; border-collapse:collapse;">
        <tr>
          <td style="padding:5px 8px; color:#777; width:160px; \
border-bottom:1px solid #f0f0f0;">alert_clock</td>
          <td style="padding:5px 8px; border-bottom:1px solid #f0f0f0;">
            {escape(alert_ts_pt)}
          </td>
        </tr>
        <tr>
          <td style="padding:5px 8px; color:#777; \
border-bottom:1px solid #f0f0f0;">tier</td>
          <td style="padding:5px 8px; color:{bar_color}; font-weight:bold; \
border-bottom:1px solid #f0f0f0;">{escape(signal.tier.value)}</td>
        </tr>
        <tr>
          <td style="padding:5px 8px; color:#777; \
border-bottom:1px solid #f0f0f0;">direction</td>
          <td style="padding:5px 8px; \
border-bottom:1px solid #f0f0f0;">{escape(signal.direction.value)}</td>
        </tr>
        <tr>
          <td style="padding:5px 8px; color:#777; \
border-bottom:1px solid #f0f0f0;">score</td>
          <td style="padding:5px 8px; \
border-bottom:1px solid #f0f0f0;">{signal.score:.3f}</td>
        </tr>
        <tr>
          <td style="padding:5px 8px; color:#777; \
border-bottom:1px solid #f0f0f0;">cooldown reason</td>
          <td style="padding:5px 8px; \
border-bottom:1px solid #f0f0f0;">{escape(cooldown_reason)}</td>
        </tr>
        {fired_detectors_row_html}
        <tr>
          <td style="padding:7px 8px; color:#777; vertical-align:top; \
border-bottom:1px solid #f0f0f0;">Detectors</td>
          <td style="padding:7px 8px; line-height:1.5; \
border-bottom:1px solid #f0f0f0;">{detector_block_html}</td>
        </tr>
        {analysis_row_html}
        {scale_row_html}{assess_row_html}
      </table>

      <h3 style="margin:22px 0 6px 0; font-size:14px; color:#333;">
        Last 1 hour (immediate context)
      </h3>
      <img src="cid:{escape(plot_cid_1h)}" alt="last 1h timeline" \
style="{plot_img_style}">

      <h3 style="margin:22px 0 6px 0; font-size:14px; color:#333;">
        Last 6 hours (broader context)
      </h3>
      <img src="cid:{escape(plot_cid_6h)}" alt="last 6h timeline" \
style="{plot_img_style}">

      <hr style="border:none; border-top:1px solid #eee; margin:20px 0 10px 0;">
      <p style="font-size:11px; color:#999; margin:0; line-height:1.5;">
        alert_id: {escape(alert_id)} &nbsp;|&nbsp;
        cooldown: {cooldown_minutes} min &nbsp;|&nbsp;
        replay_event: {escape(replay_event_id)} &nbsp;|&nbsp;
        sent: {datetime.now(ZoneInfo("America/Los_Angeles")).strftime(
            "%Y-%m-%d %H:%M:%S %Z",
        )}
      </p>
    </td></tr>
  </table>
</body></html>"""


# ─────────────────────────────────────────────────────────────────────
# Truth Social-specific helper: reason_codes → AlertAssessment.
# ─────────────────────────────────────────────────────────────────────
def _assessment_from_truth_social_codes(
    reason_codes: list[str],
) -> AlertAssessment | None:
    """Extract INSIDER_SUSPICION / INSIDER_NOTE / ANALYSIS from truth_social
    signal reason_codes and convert them into an AlertAssessment object.

    The LLM already performed its own assessment in truth_social/llm_scorer.py,
    so the dispatcher does not need to call another LLMAlertAssessor. Wrap the
    data in AlertAssessment only to reuse the same row format.

    Args:
        reason_codes: ChannelSignal.reason_codes, filled by the LLM scorer.

    Returns:
        AlertAssessment with score=insider_concern_score. Return None when there
        is no INSIDER_SUSPICION code.
    """
    suspicion_score: int | None = None
    insider_note = ""
    analysis = ""

    for code in reason_codes:
        if code.startswith("INSIDER_SUSPICION="):
            # Example: "INSIDER_SUSPICION=8/10" → 8.
            raw = code.split("=", 1)[1]
            try:
                suspicion_score = int(raw.split("/", 1)[0])
            except ValueError:
                pass
        elif code.startswith("INSIDER_NOTE="):
            insider_note = code.split("=", 1)[1]
        elif code.startswith("ANALYSIS="):
            analysis = code.split("=", 1)[1]

    if suspicion_score is None:
        return None

    # insider_bullets must contain at least one item for the row to render.
    bullets: list[str] = []
    if insider_note:
        bullets.append(insider_note)
    elif analysis:
        bullets.append(analysis)
    else:
        bullets.append(
            f"LLM scored this Trump post {suspicion_score}/10 on insider concern."
        )
    # market_bullets are unused for truth_social, but fill three values for dataclass compatibility.
    return AlertAssessment(
        score=suspicion_score,
        verdict=analysis or insider_note or "(see Analysis row)",
        insider_bullets=tuple(bullets),
        market_bullets=("", "", ""),
    )


# ─────────────────────────────────────────────────────────────────────
# SMTP send (or dry-run capture).
# ─────────────────────────────────────────────────────────────────────
async def send_email(
    rendered: RenderedEmail,
    config: SmtpConfig,
) -> SentEmail:
    """RenderedEmail + SmtpConfig → real send or dry_run capture.

    Args:
        rendered: Output from render_email.
        config: SmtpConfig, including the dry_run flag.

    Returns:
        SentEmail for the audit log.

    Raises:
        RuntimeError: dry_run=False but SMTP credentials / recipients are missing.
        smtplib.SMTPException: Real send failed; caller is responsible for retry.
    """
    sent_meta = SentEmail(
        subject=rendered.subject,
        to=config.recipients,
        sent_at_iso=datetime.now(timezone.utc).isoformat(),
        dry_run=config.dry_run,
    )

    if config.dry_run:
        logger.info(
            "channel_email DRY_RUN — subject=%s | to=%s | inline_imgs=%d",
            rendered.subject[:80], config.recipients, len(rendered.inline_images),
        )
        return sent_meta

    # Real send: validate credentials.
    if not (config.user and config.password and config.sender and config.recipients):
        raise RuntimeError(
            "send_email: SMTP credentials missing. Set SMTP_USER / SMTP_PASSWORD "
            "/ SMTP_FROM / SMTP_TO env vars, or use EMAIL_DRY_RUN=true.",
        )

    msg = _build_mime_message(rendered, config)
    # smtplib is sync, so wrap it with to_thread for async use.
    await asyncio.to_thread(_send_smtp_sync, msg, config)
    logger.info(
        "channel_email SENT — subject=%s | to=%s",
        rendered.subject[:80], config.recipients,
    )
    return sent_meta


def _build_mime_message(
    rendered: RenderedEmail, config: SmtpConfig,
) -> MIMEMultipart:
    """Build multipart/related (HTML + inline images) MIME message.

    Why multipart/related:
      For `<img src="cid:...">` to work, the same message must include an
      attached MIMEImage with the same Content-ID. multipart/related is the
      standard container for that purpose.
    """
    msg = MIMEMultipart("related")
    msg["Subject"] = rendered.subject
    msg["From"] = config.sender
    msg["To"] = ", ".join(config.recipients)
    msg["X-Alert-Id"] = rendered.alert_id  # debug helper

    # HTML body.
    msg.attach(MIMEText(rendered.html, "html", "utf-8"))

    # inline images (logo + 1h plot + 6h plot).
    parts: list[MIMEImage] = build_inline_image_parts(rendered.inline_images)
    for p in parts:
        msg.attach(p)

    return msg


def _send_smtp_sync(msg: MIMEMultipart, config: SmtpConfig) -> None:
    """Blocking SMTP send, called through async to_thread."""
    with smtplib.SMTP(config.host, config.port, timeout=config.timeout_s) as s:
        s.starttls()
        s.login(config.user, config.password)
        s.sendmail(config.sender, list(config.recipients), msg.as_string())


# ─────────────────────────────────────────────────────────────────────
# .eml dump for preview scripts and audit.
# ─────────────────────────────────────────────────────────────────────
def dump_eml(rendered: RenderedEmail, eml_path: Path, *, sender: str = "alerts@local",
             recipient: str = "you@local") -> Path:
    """RenderedEmail → standard .eml file in Gmail "Show original" format.

    Args:
        rendered: Output from render_email.
        eml_path: Output path; parent directory is created automatically.
        sender, recipient: Placeholder headers when SMTP is not sending.

    Returns:
        eml_path for caller convenience.

    Note:
        Importable into macOS Mail.app or Gmail Web by using "Show original" →
        save as .eml. Useful for visual checks.
    """
    eml_path.parent.mkdir(parents=True, exist_ok=True)
    msg = _build_mime_message(
        rendered,
        SmtpConfig(
            sender=sender, recipients=(recipient,), dry_run=True,
        ),
    )
    eml_path.write_bytes(msg.as_bytes())
    logger.info("channel_email .eml dump → %s", eml_path)
    return eml_path


__all__ = [
    "SmtpConfig",
    "RenderedEmail",
    "SentEmail",
    "render_email",
    "send_email",
    "dump_eml",
]
