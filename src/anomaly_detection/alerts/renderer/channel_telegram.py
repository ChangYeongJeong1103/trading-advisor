"""
alerts/renderer/channel_telegram.py — Per-channel alert Telegram renderer
(P11(d) — channel-level + EMERGENCY only).

────────────────────────────────────────────────────────────────────────
Role:
  Send to Telegram only when **tier == EMERGENCY** for one ChannelSignal
  that passed cooldown v2. The system-level URGENT path / heartbeat is
  fully OFF in P11(d) — only the channel-level Telegram is received
  (user decision locked 2026-04-21).

  flow (caller = ChannelAlertDispatcher):
    ChannelSignal (passed cooldown + EMERGENCY)
        ↓ render_channel_telegram(signal, plot_dir, ...)
    RenderedChannelTelegram (caption HTML + 1h/6h plot path)
        ↓ send_channel_telegram(rendered, telegram_config)
    Telegram bot HTTP API (sendMediaGroup) | or dry_run capture

  Send both 1h + 6h plots together, identical to email (user decision Q3).
  Reuse images from the same plot_dir produced right after the email
  dispatch (don't render the plot twice — saves cost + visual consistency
  between email/telegram).

────────────────────────────────────────────────────────────────────────
Telegram bot HTTP API (for channel-level alerts):

  · sendMediaGroup (https://core.telegram.org/bots/api#sendmediagroup)
    POST https://api.telegram.org/bot<TOKEN>/sendMediaGroup
    body: {
        "chat_id": "...",
        "media": [
            {"type": "photo", "media": "attach://1h", "caption": "<HTML>",
             "parse_mode": "HTML"},
            {"type": "photo", "media": "attach://6h"},
        ],
    }
    files: {"1h": <bytes>, "6h": <bytes>}

    Only the first photo's caption is shown (Telegram policy). So caption =
    subject + key metadata.

────────────────────────────────────────────────────────────────────────
Caption layout (HTML, ≤ 1024 chars — Telegram constraint):

    🚨🚨 📊 CME · BZ → EMERGENCY
    🕐 <PT time> (UTC <UTC time>)
    🎯 fired: vol_z_v1, oi_z_v1
    📊 score=0.97 · direction=down
    📝 reason: VOL_Z=4.21, OI_DELTA=large
    ⏱ cooldown: initial · 24h lock

    When cooldown reason is 'cooldown_expired', show as
    "🔔 24h elapsed reminder" so the user can intuit "risk still active".
"""

from __future__ import annotations

# ── Standard library ────────────────────────────────────────────────
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from pathlib import Path

# ── third-party ──────────────────────────────────────────────────────
import httpx

# ── Local ────────────────────────────────────────────────────────────
from ...core.schemas import ChannelSignal, Tier
from ..format_time import format_pdt
from ..llm_assessor import AlertAssessment
from ..subject_template import build_alert_subject
from .channel_x_post import (
    render_analysis,
    render_detector_lines,
    render_footnote_parts,
    symbol_with_friendly,
)
from .telegram import TelegramConfig

logger = logging.getLogger(__name__)


# Telegram caption limit (1024 chars including HTML, plain 4096 chars).
_TELEGRAM_CAPTION_MAX: int = 1024


@dataclass
class RenderedChannelTelegram:
    """Output of render_channel_telegram. For send_channel_telegram or dump_capture.

    Attributes:
        caption_html: HTML caption attached to the first photo (≤ 1024 chars).
        plot_60m_path: 1h plot PNG (already on disk — produced by email).
        plot_360m_path: 6h plot PNG (same).
        alert_id: For cross-linking to the email of the same alert (caller
            must pass the same value as email RenderedEmail.alert_id).
        rendered_at: render time (UTC).
    """
    caption_html: str
    plot_60m_path: Path
    plot_360m_path: Path
    alert_id: str = ""
    rendered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class SentChannelTelegram:
    """Capture for both dry_run / actual send (audit / test verification)."""
    caption_html: str
    chat_id: str
    sent_at_iso: str
    dry_run: bool
    plot_paths: tuple[Path, Path]


# ─────────────────────────────────────────────────────────────────────
# render
# ─────────────────────────────────────────────────────────────────────
def render_channel_telegram(
    signal: ChannelSignal,
    *,
    plot_60m_path: Path,
    plot_360m_path: Path,
    cooldown_reason: str = "initial",
    cooldown_minutes: int = 1440,
    alert_id: str = "",
    llm_assessment: AlertAssessment | None = None,
) -> RenderedChannelTelegram:
    """1 ChannelSignal → 1 RenderedChannelTelegram (caption + two plot paths).

    Args:
        signal: signal that passed cooldown + EMERGENCY (caller = dispatcher already verified).
        plot_60m_path: 1h plot PNG of the same alert (pre-built by email).
        plot_360m_path: 6h plot PNG of the same alert.
        cooldown_reason: cooldown.decide reason ('initial' /
            'escalation(prev=...)' / 'cooldown_expired').
        cooldown_minutes: current cooldown policy (shown in caption footer).
        alert_id: alert_id of the same alert's email RenderedEmail (for cross-link).
        llm_assessment: P11(b).4 — Result the dispatcher got from
            LLMAlertAssessor for EMERGENCY only. If present, append a single
            "🤖 LLM (x/10) {verdict}" line at the end of the caption. None →
            line is omitted entirely. The verdict auto-trims within the
            1024-char caption limit.

    Returns:
        RenderedChannelTelegram. The caller post-processes via
        send_channel_telegram or dump_capture.

    Raises:
        FileNotFoundError: When the plot PNGs are missing — the caller must
            invoke this right after rendering the email (the email creates the plots).
    """
    if not plot_60m_path.exists():
        raise FileNotFoundError(f"plot_60m PNG missing: {plot_60m_path}")
    if not plot_360m_path.exists():
        raise FileNotFoundError(f"plot_360m PNG missing: {plot_360m_path}")

    caption_html = _build_caption(
        signal=signal,
        cooldown_reason=cooldown_reason,
        cooldown_minutes=cooldown_minutes,
        alert_id=alert_id,
        llm_assessment=llm_assessment,
    )

    return RenderedChannelTelegram(
        caption_html=caption_html,
        plot_60m_path=plot_60m_path,
        plot_360m_path=plot_360m_path,
        alert_id=alert_id,
    )


def _build_caption(
    *,
    signal: ChannelSignal,
    cooldown_reason: str,
    cooldown_minutes: int,
    alert_id: str,
    llm_assessment: AlertAssessment | None = None,
) -> str:
    """Compress the alert's key info into HTML caption ≤ 1024 chars.

    Layout (same format as the X-post after P12-C):
      1) subject (heading, includes tier) — build_alert_subject().
      2) alert_clock (PT) + direction + score
      3) cooldown reason
      4) Detectors block (natural language + expression = value) — reuses render_detector_lines().
      5) One-line analysis
      6) One-line scale (only when DIR_IMB / WC are present)
      7) Insider-trading suspicion (EMERGENCY only, 3-5 bullets) — reuses render_bullet_summary.
      8) alert_id (small, cross-links with email)

    Drop priority when 1024 char is exceeded:
      Trim insider-trading suspicion bullets → Scale → alert_id → hard trim.

    NOTE (changed 2026-05-14): previously also rendered the "Trader's view"
    section, but per user decision the insider analysis alone is sufficient
    on every channel (X / Email / Telegram), so it was removed across the
    board. The LLM still produces market_bullets (negligible cost + keeps
    optionality for future reuse), but they are not displayed.
    """
    # 1) subject = "🚨🚨 📊 CME · BZQ5 (Brent Crude Oil) → EMERGENCY".
    #    Symbol is augmented with a friendly name (user decision 2026-05-03).
    subject = build_alert_subject(
        channel=signal.channel,
        symbol=symbol_with_friendly(signal.symbol),
        tier=signal.tier,
    )

    # 2) Time — PT only, since the user is in the Bay Area.
    pt_str = format_pdt(signal.ts)

    # 3) cooldown reason — translated for an intuitive read by the operator.
    if cooldown_reason == "initial":
        cd_label = "🆕 first alert"
    elif cooldown_reason == "cooldown_expired":
        cd_label = "🔔 24h elapsed reminder (risk still active)"
    elif cooldown_reason.startswith("escalation"):
        cd_label = f"⬆️ {cooldown_reason}"
    else:
        cd_label = cooldown_reason

    cooldown_h = cooldown_minutes // 60
    cd_lock = f"{cooldown_h}h lock" if cooldown_h else f"{cooldown_minutes}min lock"

    direction_str = signal.direction.value

    # 4-6) Same detector / analysis / scale lines as the X-post.
    detector_lines_list = render_detector_lines(signal.reason_codes)
    analysis_text = render_analysis(signal.reason_codes)
    footnote_label, footnote_body = render_footnote_parts(signal.reason_codes)

    # ── Build HTML caption ──
    parts: list[str] = []
    parts.append(f"<b>{escape(subject)}</b>")
    parts.append(f"🕐 {escape(pt_str)}")
    parts.append(f"🧭 direction: <code>{escape(direction_str)}</code>")
    parts.append(f"📊 score: <b>{signal.score:.2f}</b>")
    parts.append(f"⏱ {escape(cd_label)} · {escape(cd_lock)}")
    if detector_lines_list:
        det_lines_html = "\n".join(f"- {escape(line)}" for line in detector_lines_list)
        parts.append(f"🎯 Detectors:\n{det_lines_html}")
    if analysis_text:
        parts.append(f"📝 Analysis: {escape(analysis_text)}")

    # 7) LLM Assess: show only Insider-trading suspicion (3-5, variable).
    # Per user decision 2026-05-14, "Trader's view" is omitted on every channel.
    insider_bullets_full: tuple[str, ...] = ()
    score_emoji = ""
    if llm_assessment is not None:
        if llm_assessment.score >= 7:
            score_emoji = "🔴"
        elif llm_assessment.score >= 4:
            score_emoji = "🟠"
        else:
            score_emoji = "⚪"
        insider_bullets_full = llm_assessment.insider_bullets

    def _insider_html(n_bullets: int) -> str:
        """Render the insider block as N bullets (assumes ≥3)."""
        if llm_assessment is None or not insider_bullets_full:
            return ""
        bullets = insider_bullets_full[:n_bullets]
        block = "\n".join(f"- {escape(b)}" for b in bullets)
        return (
            f"🤖 Insider-trading suspicion {score_emoji} "
            f"<b>{llm_assessment.score}/10</b>\n{block}"
        )

    scale_html = (
        f"📐 {escape(footnote_label)}: {escape(footnote_body)}"
        if footnote_body else ""
    )
    tail_html = f"<i>id: {escape(alert_id)}</i>" if alert_id else ""

    def join_with(extras: list[str]) -> str:
        all_parts = parts + [e for e in extras if e]
        return "\n".join(all_parts)

    n_insider_full = len(insider_bullets_full)

    # ── Cascade priority (drop order) ────────────────────────────────────
    # 1) Full (insider N + scale + id)
    # 2) drop Scale
    # 3) Insider N → N-1 → … → 3
    # 4) drop Scale (Insider 3 + id)
    # 5) drop id (Insider 3 only)
    # 6) hard trim
    cascades: list[list[str]] = []
    # 1
    cascades.append([_insider_html(n_insider_full), scale_html, tail_html])
    # 2
    cascades.append([_insider_html(n_insider_full), "", tail_html])
    # 3 — insider N-1, N-2, ..., 3 (try both keep/drop scale)
    for n in range(n_insider_full - 1, 2, -1):
        cascades.append([_insider_html(n), scale_html, tail_html])
        cascades.append([_insider_html(n), "", tail_html])
    # 4-5 — even after shrinking insider to 3, also drop id if needed
    cascades.append([_insider_html(3), scale_html, tail_html])
    cascades.append([_insider_html(3), "", tail_html])
    cascades.append([_insider_html(3), "", ""])

    caption = ""
    for extras in cascades:
        candidate = join_with(extras)
        if len(candidate) <= _TELEGRAM_CAPTION_MAX:
            caption = candidate
            break
    if not caption:
        # Last resort hard trim — every cascade failed.
        caption = join_with(cascades[-1])[: _TELEGRAM_CAPTION_MAX - 6] + "\n..."
    return caption


# ─────────────────────────────────────────────────────────────────────
# send (real or dry-run)
# ─────────────────────────────────────────────────────────────────────
async def send_channel_telegram(
    rendered: RenderedChannelTelegram,
    config: TelegramConfig,
) -> SentChannelTelegram:
    """sendMediaGroup via the Telegram bot (2 photos + caption). Supports dry_run.

    Args:
        rendered: output of render_channel_telegram.
        config: Reuses the existing TelegramConfig (bot_token / chat_id /
            dry_run / timeout_s).

    Returns:
        SentChannelTelegram (capture).

    Raises:
        RuntimeError: when dry_run=False but bot_token/chat_id is empty.
        httpx.HTTPError: API call failure.
    """
    sent = SentChannelTelegram(
        caption_html=rendered.caption_html,
        chat_id=config.chat_id,
        sent_at_iso=datetime.now(timezone.utc).isoformat(),
        dry_run=config.dry_run,
        plot_paths=(rendered.plot_60m_path, rendered.plot_360m_path),
    )

    if config.dry_run:
        logger.info(
            "channel_telegram: DRY_RUN — captured (caption %d chars, alert_id=%s)",
            len(rendered.caption_html), rendered.alert_id,
        )
        return sent

    if not (config.bot_token and config.chat_id):
        raise RuntimeError(
            "send_channel_telegram: bot_token/chat_id missing. "
            "Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID or use dry_run=True."
        )

    # sendMediaGroup — multipart/form-data format.
    # File references inside the media array via attach://<key>.
    media_descriptors = [
        {
            "type": "photo",
            "media": "attach://plot60m",
            # Only the first photo's caption is visible (Telegram policy).
            "caption": rendered.caption_html,
            "parse_mode": "HTML",
        },
        {
            "type": "photo",
            "media": "attach://plot360m",
        },
    ]

    url = f"https://api.telegram.org/bot{config.bot_token}/sendMediaGroup"
    data = {
        "chat_id": config.chat_id,
        "media": json.dumps(media_descriptors),
        "disable_notification": "false",
    }
    # httpx multipart — pass files as streams.
    with rendered.plot_60m_path.open("rb") as f60, \
         rendered.plot_360m_path.open("rb") as f360:
        files = {
            "plot60m": (rendered.plot_60m_path.name, f60, "image/png"),
            "plot360m": (rendered.plot_360m_path.name, f360, "image/png"),
        }
        async with httpx.AsyncClient(timeout=config.timeout_s) as client:
            resp = await client.post(url, data=data, files=files)
            resp.raise_for_status()
            payload = resp.json()
            if not payload.get("ok"):
                raise RuntimeError(f"Telegram sendMediaGroup not-ok: {payload}")

    logger.info(
        "channel_telegram: sent (caption %d chars, alert_id=%s) to chat %s",
        len(rendered.caption_html), rendered.alert_id, config.chat_id,
    )
    return sent


# ─────────────────────────────────────────────────────────────────────
# dry-run audit dump
# ─────────────────────────────────────────────────────────────────────
def dump_telegram_capture(
    rendered: RenderedChannelTelegram,
    out_path: Path,
) -> Path:
    """RenderedChannelTelegram → human-readable .txt file.

    Dumps the plot paths + caption HTML as-is. In dry_run the operator can
    open .eml + .txt side by side in the directory to compare the same
    alert's email/telegram.

    Args:
        rendered: RenderedChannelTelegram.
        out_path: output path (parent created automatically).

    Returns:
        out_path (for convenience).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f"=== Channel Telegram (dry_run capture) ===\n"
        f"alert_id    : {rendered.alert_id}\n"
        f"rendered_at : {rendered.rendered_at.isoformat()}\n"
        f"plot_60m    : {rendered.plot_60m_path}\n"
        f"plot_360m   : {rendered.plot_360m_path}\n"
        f"\n--- caption (HTML) ---\n{rendered.caption_html}\n"
    )
    out_path.write_text(body, encoding="utf-8")
    return out_path


__all__ = [
    "RenderedChannelTelegram",
    "SentChannelTelegram",
    "dump_telegram_capture",
    "render_channel_telegram",
    "send_channel_telegram",
]
