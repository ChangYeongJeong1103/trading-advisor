"""
alerts/channel_dispatcher.py — Per-channel alert dispatcher for the production
daemon (P11(a).4.3 → P11(d) refactor).

────────────────────────────────────────────────────────────────────────
Role:
  Called by the orchestrator on every fusion cycle. For each channel
  signal in the registry snapshot:

    1) Decide whether cooldown v2 lets it through (`ChannelAlertCooldown.decide`)
       — when it does, cooldown state is updated exactly once (email and
         telegram share the same cooldown — user decision locked 2026-04-21).
    2) For passing signals → synthesize a ReplayResult from the live timeline buffer
    3) `channel_email.render_email()` → write 2 plots (1h / 6h) + HTML body
    4) `channel_email.send_email()` to send via SMTP (or capture in dry_run)
    5) For tier == EMERGENCY only, also send via telegram (P11(d))
       · Reuse the two PNGs the email already produced — don't render plots twice
       · Include the same alert_id in the caption so email/telegram cross-link
    6) For EMERGENCY only, also auto-post to X (best-effort)

  Failures of one channel / medium are isolated with try/except so they do
  not block other sends.

────────────────────────────────────────────────────────────────────────
Usage (inside the daemon):

    self._channel_dispatcher = ChannelAlertDispatcher(
        cooldown=ChannelAlertCooldown(cooldown_minutes=1440),
        buffer=self._timeline_buffer,
        smtp_config=SmtpConfig(...),
        telegram_config=TelegramConfig(...),       # None → telegram OFF
        telegram_emergency_only=True,              # P11(d) lock
        out_root=Path("data/anomaly/alerts_live"),
    )
    ...
    await self._channel_dispatcher.maybe_dispatch(signals, sim_clock=now)

────────────────────────────────────────────────────────────────────────
Policy (locked 2026-04-21, P11(b).2 update 2026-04-22):
  · system-level FusedAnomalyEvent email / telegram are no longer sent.
    AlertRouter has email_enabled=False, telegram_enabled=False.
  · per-channel email: tier ≥ `email_min_tier` (D11 default RISK_OFF —
    WATCH does not arrive in the inbox. The original D11 design had
    WATCH = 06:00 PT digest, but the P11(a) implementation accidentally
    dropped the email_min_tier filter, causing WATCH to be emailed
    immediately. Restored to D11 behavior in P11(b).2.)
  · per-channel telegram: tier == EMERGENCY only (`telegram_emergency_only`)
  · cooldown state is shared by email/telegram — within 24h the same alert
    is silent on both.
"""

from __future__ import annotations

# ── Standard library ────────────────────────────────────────────────
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ── Local ────────────────────────────────────────────────────────────
from ..calendar import (
    DEFAULT_SCHEDULED_WINDOWS,
    ScheduledReleaseWindow,
    is_in_scheduled_window,
)
from ..core.schemas import ChannelSignal, Tier
from .alert_ohlc_buffer import AlertOhlcBuffer
from .cooldown import ChannelAlertCooldown
from .health_alert import (
    HealthAlertCooldown,
    SystemHealthAlert,
    render_health_email,
)
from .live_timeline import LiveTimelineBuffer
from .llm_assessor import AlertAssessment, LLMAlertAssessor
from .renderer.channel_email import (
    SentEmail,
    SmtpConfig,
    dump_eml,
    render_email,
    send_email,
)
from .renderer.channel_telegram import (
    SentChannelTelegram,
    dump_telegram_capture,
    render_channel_telegram,
    send_channel_telegram,
)
from .renderer.channel_x_post import render_channel_x_thread
from .renderer.email_plot import EMAIL_STACK_WINDOWS
from .renderer.telegram import TelegramConfig
from .x_publisher import SentXPost, XCredentials, XPostConfig, send_x_thread

logger = logging.getLogger(__name__)


@dataclass
class DispatchResult:
    """Dispatch result for one cycle (test + ops monitoring).

    Attributes:
        emails: emails sent (or dry_run captured) in this cycle.
        telegrams: telegrams sent (or dry_run captured) in this cycle.
            Always ≤ length of emails (EMERGENCY only).
    """
    emails: list[SentEmail] = field(default_factory=list)
    telegrams: list[SentChannelTelegram] = field(default_factory=list)
    x_posts: list[SentXPost] = field(default_factory=list)


class ChannelAlertDispatcher:
    """Per-channel alert dispatch — cooldown + plot + email + telegram.

    Attributes:
        cooldown: shared cooldown state (same instance for email + telegram).
        buffer: timeline rolling buffer (plot data source).
        smtp_config: SMTP credentials + dry_run flag.
        telegram_config: Telegram bot credentials + dry_run flag.
            None → the telegram path is disabled entirely (email only).
        telegram_emergency_only: True (P11(d) lock) sends to telegram only at
            EMERGENCY tier. False applies the same tier policy as email.
        email_min_tier: skip email send below this tier (D11: RISK_OFF default).
            Added in P11(b).2. Evaluated right after the cooldown passes, so
            the cooldown state is still updated (= even if the same
            (channel, symbol, WATCH) returns within 24h, it is folded into
            the cooldown so plot/render cost is 0).
        scheduled_windows: P12-A. Pre-announced economic release windows.
            None → use `DEFAULT_SCHEDULED_WINDOWS`. Match _before_ the cooldown
            check → suppress (don't dirty cooldown state).
            Pass an empty tuple () to turn the calendar filter OFF.
        out_root: root dir where plot PNG / .eml / .txt are saved per alert.
    """

    def __init__(
        self,
        *,
        cooldown: ChannelAlertCooldown,
        buffer: LiveTimelineBuffer,
        smtp_config: SmtpConfig,
        out_root: Path,
        telegram_config: TelegramConfig | None = None,
        telegram_emergency_only: bool = True,
        email_min_tier: Tier = Tier.RISK_OFF,
        scheduled_windows: tuple[ScheduledReleaseWindow, ...] | None = None,
        llm_assessor: LLMAlertAssessor | None = None,
        x_post_config: XPostConfig | None = None,
        x_credentials: XCredentials | None = None,
        ohlc_buffer: AlertOhlcBuffer | None = None,
    ) -> None:
        self._cooldown = cooldown
        self._buffer = buffer
        self._smtp = smtp_config
        # P12-D — data source for the alert PNG's price/volume panels. None →
        # the plot writer shows a placeholder (compatible with old callers).
        self._ohlc_buffer = ohlc_buffer
        self._telegram = telegram_config
        self._telegram_emergency_only = telegram_emergency_only
        self._email_min_tier = email_min_tier
        self._scheduled_windows = (
            scheduled_windows
            if scheduled_windows is not None
            else DEFAULT_SCHEDULED_WINDOWS
        )
        self._llm_assessor = llm_assessor
        self._x_post_config = x_post_config or XPostConfig(enabled=False, dry_run=True)
        self._x_credentials = x_credentials or XCredentials()
        self._out_root = out_root
        self._out_root.mkdir(parents=True, exist_ok=True)

        # ── P12-F: System health alert (email-only) ────────────────
        # Completely separate from the market ChannelSignal path. Never
        # reaches Telegram / X — dispatch_health_alert() only calls send_email.
        # 24h self-managed cooldown to prevent storms of identical alerts.
        self._health_cooldown = HealthAlertCooldown()

        # Audit counters — to be exposed via a future health endpoint.
        # email/telegram are counted separately (telegram is always ≤ email).
        self._stats: dict[str, int] = {
            "considered": 0,
            "emitted": 0,         # passed cooldown + tier filter (= email attempted)
            "suppressed": 0,
            "suppressed_scheduled": 0,    # P12-A: count blocked by calendar filter
            "email_skipped_tier": 0,     # passed cooldown but below email_min_tier
            "email_errors": 0,
            "telegram_emitted": 0,
            "telegram_skipped_tier": 0,  # passed cooldown but skipped by tier filter
            "telegram_errors": 0,
            "x_post_emitted": 0,
            "x_post_skipped_tier": 0,
            "x_post_errors": 0,
            # P11(b).4: LLM assessment stats. "attempted" = number of EMERGENCY
            # alerts where an LLM call was attempted, "succeeded" = number that
            # actually got a result and made it into the email.
            "llm_assess_attempted": 0,
            "llm_assess_succeeded": 0,
            # P12-F: System health alert path (email-only).
            "health_considered": 0,        # dispatch_health_alert() call count
            "health_emitted": 0,           # email actually sent
            "health_suppressed_cooldown": 0,
            "health_errors": 0,
        }

    @property
    def stats(self) -> dict[str, int]:
        """Cumulative stats (counts) so far. For ops health monitoring."""
        return dict(self._stats)

    @property
    def telegram_enabled(self) -> bool:
        """True if telegram_config was injected (env tokens are checked at send time)."""
        return self._telegram is not None

    @property
    def x_post_enabled(self) -> bool:
        """Whether X auto-post is enabled by configuration."""
        return bool(self._x_post_config.enabled)

    # ─────────────────────────────────────────────────────────────────
    # Main API — called by the orchestrator every cycle.
    # ─────────────────────────────────────────────────────────────────
    async def maybe_dispatch(
        self,
        signals: dict[str, ChannelSignal | None],
        *,
        sim_clock: datetime,
    ) -> DispatchResult:
        """signals snapshot → email + (optional) telegram for those that pass cooldown.

        Args:
            signals: result of registry.snapshot_signals().
            sim_clock: this cycle's time (UTC).

        Returns:
            DispatchResult — captured emails + telegrams.
            Failed / suppressed channels are not added to either list.
        """
        result = DispatchResult()

        for channel_name, sig in signals.items():
            if sig is None:
                continue
            self._stats["considered"] += 1

            # ── P12-A: calendar filter (before cooldown — to keep state clean) ──
            # Pre-announced economic release windows (EIA / API / NFP) are
            # usually normal macro flow. But genuine insider leaks (like the
            # 4/21 BZ historical event) also occur in the same windows
            # (positioning right before release).
            # → policy varies by tier:
            #
            #   · WATCH / RISK_OFF in window → suppress (absorbs false positives).
            #   · EMERGENCY in window     → let through, but add a
            #     "SCHEDULED_<label>" tag in reason_codes. The user gets it
            #     with the caveat known.
            #
            # NORMAL tier is normal_skip in the cooldown stage anyway.
            sched_label = ""
            if sig.tier != Tier.NORMAL:
                in_sched, sched_label = is_in_scheduled_window(
                    sig.ts, channel_name, sig.symbol,
                    windows=self._scheduled_windows,
                )
                if in_sched and sig.tier != Tier.EMERGENCY:
                    self._stats["suppressed_scheduled"] += 1
                    logger.info(
                        "channel_dispatcher: SCHEDULED suppress channel=%s "
                        "symbol=%s tier=%s release=%s ts=%s",
                        channel_name, sig.symbol, sig.tier.value,
                        sched_label, sig.ts,
                    )
                    continue
                if in_sched:
                    # EMERGENCY in window — let through but tag in reason_codes.
                    # ChannelSignal is frozen Pydantic, so create a new object via model_copy.
                    new_reasons = list(sig.reason_codes) + [
                        f"SCHEDULED_{sched_label.replace(' ', '_').upper()}",
                    ]
                    sig = sig.model_copy(update={"reason_codes": new_reasons})
                    logger.info(
                        "channel_dispatcher: SCHEDULED tag (EMERGENCY through) "
                        "channel=%s symbol=%s release=%s ts=%s",
                        channel_name, sig.symbol, sched_label, sig.ts,
                    )

            # ── cooldown decision (called once — shared by email/telegram) ──
            emit, reason = self._cooldown.decide(sig, channel=channel_name)
            if not emit:
                self._stats["suppressed"] += 1
                logger.debug(
                    "channel_dispatcher: suppress channel=%s symbol=%s tier=%s reason=%s",
                    channel_name, sig.symbol, sig.tier.value, reason,
                )
                continue

            # ── email tier filter (P11(b).2 — D11: only RISK_OFF+ emails immediately) ──
            # Cooldown was already updated above (if the same (channel, symbol, WATCH)
            # arrives again within 24h, it stays folded into the cooldown so
            # plot/render cost is 0). The tier filter therefore simply skips
            # the email send. WATCH is not stored in GCS audit and only occupies
            # cooldown state — when WATCH later escalates to RISK_OFF, the
            # cooldown's escalation rule will let it through normally.
            if sig.tier.rank() < self._email_min_tier.rank():
                self._stats["email_skipped_tier"] += 1
                logger.info(
                    "channel_dispatcher: email skip (tier<min) channel=%s "
                    "symbol=%s tier=%s min=%s",
                    channel_name, sig.symbol, sig.tier.value,
                    self._email_min_tier.value,
                )
                continue

            # ── P11(b).4: LLM assessment (EMERGENCY only). ──────────
            # best-effort — failures still allow the email to go out. The
            # assessor itself returns None on failure, so no extra try here.
            llm_assessment = await self._maybe_run_llm_assessment(
                sig, channel_name,
            )

            # ── email render + send (telegram still attempted on failure) ──
            email_meta, plot_dir, alert_id = await self._dispatch_email(
                sig, channel_name, reason, llm_assessment,
            )
            if email_meta is not None:
                self._stats["emitted"] += 1
                result.emails.append(email_meta)

            # ── telegram (optional, EMERGENCY-only filter) ──
            # If plot_dir / alert_id is None (email render failed), telegram is skipped.
            if (
                plot_dir is not None
                and alert_id is not None
                and self.telegram_enabled
            ):
                if self._telegram_emergency_only and sig.tier != Tier.EMERGENCY:
                    self._stats["telegram_skipped_tier"] += 1
                else:
                    tg_capture = await self._dispatch_telegram(
                        sig=sig,
                        channel_name=channel_name,
                        reason=reason,
                        plot_dir=plot_dir,
                        alert_id=alert_id,
                        llm_assessment=llm_assessment,
                    )
                    if tg_capture is not None:
                        self._stats["telegram_emitted"] += 1
                        result.telegrams.append(tg_capture)

            # ── X auto-post (optional, EMERGENCY only) ──
            # Best-effort, independent of email/telegram.
            if self.x_post_enabled:
                if sig.tier != Tier.EMERGENCY:
                    self._stats["x_post_skipped_tier"] += 1
                else:
                    x_alert_id = alert_id or (
                        f"{channel_name}-{sig.symbol}-{sig.ts.strftime('%Y%m%dT%H%M%S')}"
                    )
                    # Reuse the 1h plot prepared during the email step for X
                    # as well (user decision 2026-05-04). If plot_dir is None
                    # (email failed), post text-only without an image.
                    x_plot_path: Path | None = None
                    if plot_dir is not None:
                        win_60, _ = EMAIL_STACK_WINDOWS  # (60, 360)
                        candidate = plot_dir / f"plot_{win_60}m.png"
                        if candidate.exists():
                            x_plot_path = candidate
                    x_post = await self._dispatch_x_post(
                        sig=sig,
                        channel_name=channel_name,
                        reason=reason,
                        alert_id=x_alert_id,
                        llm_assessment=llm_assessment,
                        media_path=x_plot_path,
                    )
                    if x_post is not None:
                        self._stats["x_post_emitted"] += 1
                        result.x_posts.append(x_post)

        return result

    # ─────────────────────────────────────────────────────────────────
    # Internal — LLM assessment (EMERGENCY only, best-effort)
    # ─────────────────────────────────────────────────────────────────
    async def _maybe_run_llm_assessment(
        self, sig: ChannelSignal, channel_name: str,
    ) -> AlertAssessment | None:
        """Call LLMAlertAssessor only on EMERGENCY. Returns None for any
        other tier / no assessor / call failure."""
        if self._llm_assessor is None:
            return None
        if sig.tier != Tier.EMERGENCY:
            return None
        self._stats["llm_assess_attempted"] += 1
        try:
            assessment = await self._llm_assessor.assess(sig)
        except Exception as exc:  # noqa: BLE001 — safety net
            logger.warning(
                "channel_dispatcher: LLM assess unexpected error "
                "channel=%s symbol=%s — %s",
                channel_name, sig.symbol, exc,
            )
            return None
        if assessment is not None:
            self._stats["llm_assess_succeeded"] += 1
        return assessment

    # ─────────────────────────────────────────────────────────────────
    # Internal — email render + send
    # ─────────────────────────────────────────────────────────────────
    async def _dispatch_email(
        self,
        sig: ChannelSignal,
        channel_name: str,
        reason: str,
        llm_assessment: AlertAssessment | None,
    ) -> tuple[SentEmail | None, Path | None, str | None]:
        """One ChannelSignal → render + send email. Also returns plot_dir / alert_id
        (the telegram step reuses PNGs in the same directory).

        Returns:
            (sent_email_meta, plot_dir, alert_id) — when render fails, returns
            (None, None, None) (telegram is also skipped).
        """
        try:
            # 1) plot data — synthetic ReplayResult from the buffer.
            replay_result = self._buffer.to_replay_result_at(sig.ts)

            # 2) Per-alert isolated directory for plots/eml.
            ts_tag = sig.ts.strftime("%Y%m%dT%H%M")
            plot_dir = (
                self._out_root
                / f"{channel_name}_{sig.symbol}_{sig.tier.value}_{ts_tag}"
            )

            # 3) render — 2 plot PNGs + HTML body.
            rendered = render_email(
                signal=sig,
                replay_result=replay_result,
                plot_dir=plot_dir,
                cooldown_reason=reason,
                cooldown_minutes=self._cooldown.cooldown_minutes,
                llm_assessment=llm_assessment,
                ohlc_buffer=self._ohlc_buffer,
            )
        except Exception as exc:  # noqa: BLE001
            self._stats["email_errors"] += 1
            logger.exception(
                "channel_dispatcher: email RENDER failed channel=%s symbol=%s "
                "tier=%s — %s",
                channel_name, sig.symbol, sig.tier.value, exc,
            )
            return (None, None, None)

        # 4) send (or dry_run).
        try:
            meta = await send_email(rendered, self._smtp)
        except Exception as exc:  # noqa: BLE001
            self._stats["email_errors"] += 1
            logger.exception(
                "channel_dispatcher: email SEND failed channel=%s symbol=%s "
                "tier=%s alert_id=%s — %s",
                channel_name, sig.symbol, sig.tier.value, rendered.alert_id, exc,
            )
            # Still pass plot_dir / alert_id to telegram — render succeeded
            # and telegram is a separate medium that's worth retrying.
            return (None, plot_dir, rendered.alert_id)

        # 5) On dry_run only, automatically dump audit .eml.
        if self._smtp.dry_run:
            try:
                dump_eml(rendered, plot_dir / "channel_alert.eml")
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "channel_dispatcher: dump_eml failed (non-fatal): %s", e,
                )

        logger.info(
            "channel_dispatcher: emit EMAIL channel=%s symbol=%s tier=%s reason=%s "
            "alert_id=%s dry_run=%s",
            channel_name, sig.symbol, sig.tier.value, reason,
            rendered.alert_id, self._smtp.dry_run,
        )
        return (meta, plot_dir, rendered.alert_id)

    # ─────────────────────────────────────────────────────────────────
    # Internal — telegram render + send (EMERGENCY only)
    # ─────────────────────────────────────────────────────────────────
    async def _dispatch_telegram(
        self,
        *,
        sig: ChannelSignal,
        channel_name: str,
        reason: str,
        plot_dir: Path,
        alert_id: str,
        llm_assessment: AlertAssessment | None = None,
    ) -> SentChannelTelegram | None:
        """One ChannelSignal → render + send telegram. Reuses the email's plots.

        If llm_assessment is provided, append a "🤖 LLM (x/10) ..." line at
        the end of the caption (P11(b).4). The caller fills this only on EMERGENCY.
        """
        # The email step must have created plot_dir/plot_60m.png and plot_dir/plot_360m.png.
        win_60, win_360 = EMAIL_STACK_WINDOWS  # (60, 360)
        plot_60 = plot_dir / f"plot_{win_60}m.png"
        plot_360 = plot_dir / f"plot_{win_360}m.png"

        assert self._telegram is not None  # caller already verified telegram_enabled.

        try:
            rendered_tg = render_channel_telegram(
                signal=sig,
                plot_60m_path=plot_60,
                plot_360m_path=plot_360,
                cooldown_reason=reason,
                cooldown_minutes=self._cooldown.cooldown_minutes,
                alert_id=alert_id,
                llm_assessment=llm_assessment,
            )
            tg_capture = await send_channel_telegram(rendered_tg, self._telegram)
        except Exception as exc:  # noqa: BLE001
            self._stats["telegram_errors"] += 1
            logger.exception(
                "channel_dispatcher: telegram FAILED channel=%s symbol=%s "
                "alert_id=%s — %s",
                channel_name, sig.symbol, alert_id, exc,
            )
            return None

        # On dry_run, dump audit .txt (operator opens .eml + .txt side by side).
        if self._telegram.dry_run:
            try:
                dump_telegram_capture(
                    rendered_tg, plot_dir / "channel_telegram.txt",
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "channel_dispatcher: dump_telegram_capture failed "
                    "(non-fatal): %s", e,
                )

        logger.info(
            "channel_dispatcher: emit TELEGRAM channel=%s symbol=%s tier=%s "
            "alert_id=%s dry_run=%s",
            channel_name, sig.symbol, sig.tier.value, alert_id,
            self._telegram.dry_run,
        )
        return tg_capture

    async def _dispatch_x_post(
        self,
        *,
        sig: ChannelSignal,
        channel_name: str,
        reason: str,
        alert_id: str,
        llm_assessment: AlertAssessment | None = None,
        media_path: Path | None = None,
    ) -> SentXPost | None:
        """Auto-upload an EMERGENCY alert as an X thread (best-effort).

        Args:
            media_path: PNG path to attach (currently the same 1h plot as
                email/Telegram). None → text-only post. If upload fails,
                automatically falls back to text-only.
        """
        try:
            rendered = render_channel_x_thread(
                signal=sig,
                cooldown_reason=reason,
                llm_assessment=llm_assessment,
            )
            sent = await send_x_thread(
                tweets=rendered.tweets,
                config=self._x_post_config,
                creds=self._x_credentials,
                media_path=media_path,
            )
        except Exception as exc:  # noqa: BLE001
            self._stats["x_post_errors"] += 1
            logger.exception(
                "channel_dispatcher: X post FAILED channel=%s symbol=%s "
                "alert_id=%s — %s",
                channel_name, sig.symbol, alert_id, exc,
            )
            return None

        logger.info(
            "channel_dispatcher: emit X_POST channel=%s symbol=%s tier=%s "
            "alert_id=%s dry_run=%s tweet_count=%d",
            channel_name,
            sig.symbol,
            sig.tier.value,
            alert_id,
            sent.dry_run,
            len(sent.tweets),
        )
        return sent

    # ─────────────────────────────────────────────────────────────────
    # P12-F: System health alert — email-only path
    # ─────────────────────────────────────────────────────────────────
    async def dispatch_health_alert(
        self,
        alert: SystemHealthAlert,
    ) -> SentEmail | None:
        """SystemHealthAlert → 1 email.

        Completely separate from the ChannelSignal pipeline:
            · Own cooldown (HealthAlertCooldown).
            · Never reaches Telegram / X branches at all.
            · No heavy LLM assessment / plot rendering — health email is plain HTML.

        Args:
            alert: the health event to dispatch.

        Returns:
            SentEmail (or dry-run capture). None if cooldown blocked or rendering
            / sending failed.
        """
        self._stats["health_considered"] += 1

        if not self._health_cooldown.should_send(alert):
            self._stats["health_suppressed_cooldown"] += 1
            logger.info(
                "channel_dispatcher: health SUPPRESS (cooldown) "
                "component=%s kind=%s",
                alert.component, alert.kind.value,
            )
            return None

        try:
            rendered = render_health_email(alert)
        except Exception as exc:  # noqa: BLE001
            self._stats["health_errors"] += 1
            logger.exception(
                "channel_dispatcher: health RENDER failed component=%s kind=%s — %s",
                alert.component, alert.kind.value, exc,
            )
            return None

        try:
            meta = await send_email(rendered, self._smtp)
        except Exception as exc:  # noqa: BLE001
            self._stats["health_errors"] += 1
            logger.exception(
                "channel_dispatcher: health SEND failed component=%s kind=%s "
                "alert_id=%s — %s",
                alert.component, alert.kind.value, rendered.alert_id, exc,
            )
            return None

        self._stats["health_emitted"] += 1
        logger.info(
            "channel_dispatcher: emit HEALTH component=%s kind=%s dry_run=%s "
            "alert_id=%s",
            alert.component,
            alert.kind.value,
            meta.dry_run,
            rendered.alert_id,
        )
        return meta


# ── Backward-compat alias (preserves the P11(a) ChannelEmailDispatcher import) ──
# New code should import ChannelAlertDispatcher directly.
ChannelEmailDispatcher = ChannelAlertDispatcher


__all__ = [
    "ChannelAlertDispatcher",
    "ChannelEmailDispatcher",  # backward-compat
    "DispatchResult",
]
