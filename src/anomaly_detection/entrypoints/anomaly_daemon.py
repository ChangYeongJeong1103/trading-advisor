"""
anomaly_daemon.py — production entry point (Cloud Run + local).

────────────────────────────────────────────────────────────────────────
Run:
  Local:       python -m anomaly.entrypoints.anomaly_daemon
  Cloud Run:   same (ENTRYPOINT of Dockerfile.anomaly invokes this)

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §6.1, §6.5):
  1) Load AnomalyConfig (.env or OS env vars)
  2) Wire all components (registry, stores, alerts, orchestrator)
  3) Run 4 background tasks concurrently:
       a. orchestrator.fusion_loop       — fusion cycle every 5s
       b. heartbeat_loop                  — every 60s, EMERGENCY reminder if persists 1h
       c. digest_loop                     — WATCH digest flush at Bay Area 06:00
      d. http_server                     — health/ready + /metrics + /snapshot
  4) On SIGTERM/SIGINT: graceful shutdown (stop all tasks, close stores)

────────────────────────────────────────────────────────────────────────
Cloud Run requirements (https://cloud.google.com/run/docs/reference/container-contract):
  - Listen on $PORT env var (default 8080)
  - Bind to 0.0.0.0
  - Exit within 10s of SIGTERM
  - Container is killed if it does not start listening on PORT within 4 minutes of startup

────────────────────────────────────────────────────────────────────────
Env vars (injected via Cloud Run env / Secret Manager):
  ANOMALY_ENV        = "cloud_run" | "local"  (default "local")
  PORT               = HTTP server port      (auto-injected by Cloud Run, default 8080)
  ANOMALY_DATA_PATH  = data/ override        (on Cloud Run: /tmp/anomaly-data or GCS FUSE mount)
  ANOMALY_DRY_RUN    = "true" / "false"      (force dry-run; if unset, auto — dry when no secrets)
  + all of SecretsConfig: SMTP_*, TELEGRAM_*, *_API_KEY (see config/config.py)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from aiohttp import web

from ..alerts.alert_ohlc_buffer import AlertOhlcBuffer
from ..alerts.channel_dispatcher import ChannelAlertDispatcher
from ..alerts.cooldown import ChannelAlertCooldown
from ..alerts.live_timeline import LiveTimelineBuffer
from ..alerts.llm_assessor import LLMAlertAssessor
from ..alerts.renderer.channel_email import SmtpConfig as ChannelSmtpConfig
from ..alerts.renderer.email import EmailConfig, EmailRenderer
from ..alerts.renderer.telegram import TelegramConfig, TelegramRenderer
from ..alerts.router import AlertRouter
from ..alerts.throttle import AlertThrottle, ThrottleConfig
from ..alerts.x_publisher import XCredentials, XPostConfig
from pydantic import ValidationError

from ..channels.cme import CMEChannel
from ..channels.cme.cme_insider_scanner import InsiderScannerConfig
from ..channels.cme.enricher import CMEEnricher, EnrichmentConfig
from ..channels.cme.streamer_health_monitor import (
    HealthMonitorConfig,
    StreamerHealthMonitor,
)
from ..channels.cme.tradingview_adapter import TradingViewAdapter
from ..channels.hyperliquid import HyperliquidChannel
from ..channels.polymarket import PolymarketChannel
from ..channels.truth_social import TruthSocialChannel
from ..channels.x import MockXCollector, XChannel, XCollector
from ..core.config import AnomalyConfig, load_config
from ..core.orchestrator import AnomalyOrchestrator
from ..core.registry import ChannelRegistry
from ..core.schemas import (
    ALL_CHANNELS,
    CHANNEL_CME,
    CHANNEL_HYPERLIQUID,
    CHANNEL_POLYMARKET,
    CHANNEL_TRUTH_SOCIAL,
    CHANNEL_X,
    Tier,
)
from ..core.state_manager import StateManager
from ..monitoring.health import HealthRegistry
from ..monitoring.metrics import MetricRegistry
from ..storage.decision_store import DecisionStore
from ..storage.feature_store import FeatureStore
from ..storage.hl_wallet_store import HLWalletStore
from ..storage.polymarket_baseline_store import PolymarketBaselineStore
from ..storage.raw_store import RawStore
from ..storage.signal_store import SignalStore

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Logging — Cloud Logging-friendly JSON (structlog)
# ─────────────────────────────────────────────────────────────────────
def setup_logging(env: str) -> None:
    """Choose logging format based on env.

    - "cloud_run" : JSON line per log → Cloud Logging parses automatically
    - "local"     : human-readable color console
    """
    level = logging.INFO

    # Also capture stdlib logging (3rd-party libraries)
    logging.basicConfig(
        level=level,
        format="%(message)s",
        stream=sys.stdout,
    )

    if env == "cloud_run":
        # JSON for Cloud Logging — format_exc_info is compatible with JSON renderer
        processors = [
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # ConsoleRenderer pretty-prints exceptions itself — drop format_exc_info
        # to avoid the "Remove `format_exc_info` from your processor chain" warning
        processors = [
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


# ─────────────────────────────────────────────────────────────────────
# Daemon — bundles all components + lifecycle management
# ─────────────────────────────────────────────────────────────────────
class AnomalyDaemon:
    """24/7 daemon — wires all components + manages 4 background tasks."""

    def __init__(self, config: AnomalyConfig, *, dry_run_alerts: bool | None = None) -> None:
        self.config = config
        self._stop_event: asyncio.Event | None = None  # created in main() (inside asyncio loop)
        self._tasks: list[asyncio.Task] = []
        self._http_runner: web.AppRunner | None = None

        # Storage paths — apply env override (on Cloud Run: /tmp or GCS FUSE)
        data_root = Path(os.getenv("ANOMALY_DATA_PATH", str(config.storage.base_path)))
        data_root.mkdir(parents=True, exist_ok=True)
        (data_root / "signals").mkdir(exist_ok=True)
        (data_root / "decisions").mkdir(exist_ok=True)
        (data_root / "raw").mkdir(exist_ok=True)
        (data_root / "features").mkdir(exist_ok=True)
        (data_root / "polymarket_baseline").mkdir(exist_ok=True)
        (data_root / "hl_wallet").mkdir(exist_ok=True)

        self.signal_store = SignalStore(data_root / "signals" / "signals.db")
        self.decision_store = DecisionStore(data_root / "decisions" / "decisions.db")
        # Raw + Feature stores — used by channels to write audit trail (P2~P5).
        # register_atexit=False: the daemon's graceful stop() flushes explicitly.
        self.raw_store = RawStore(data_root / "raw", register_atexit=False)
        self.feature_store = FeatureStore(data_root / "features", register_atexit=False)
        # P9.1.C — Polymarket time-of-day volume baseline (5-min buckets).
        # 14d retention: queries on (weekday, hour, minute_bucket) match ~2 samples.
        # A meaningful baseline forms after ~1 week of operation (n>=tod_min_n=5 → vol_burst_v2 activates).
        self.polymarket_baseline_store = PolymarketBaselineStore(
            data_root / "polymarket_baseline" / "pbs.db",
            retention_days=14,
        )
        # P9.2.P2 — Hyperliquid wallet/trade tracker (based on recentTrades).
        # cold-start: started_at_ms is persisted to SQLite so warmup accumulates across restarts.
        self.hl_wallet_store = HLWalletStore(
            data_root / "hl_wallet" / "hl_wallet.db",
            trade_retention_hours=1,
            wallet_retention_days=7,
        )

        # Alerts — auto-decide dry_run (dry if no creds, real if creds present)
        explicit_dry = self._parse_bool(os.getenv("ANOMALY_DRY_RUN"))
        smtp_ok = bool(config.secrets.smtp_user and config.secrets.smtp_password)
        tg_ok = bool(config.secrets.telegram_bot_token and config.secrets.telegram_chat_id)

        if explicit_dry is True:
            email_dry, tg_dry = True, True
        elif explicit_dry is False:
            email_dry, tg_dry = False, False
        else:
            email_dry, tg_dry = (not smtp_ok), (not tg_ok)

        if dry_run_alerts is not None:  # code-level override (for tests)
            email_dry = tg_dry = dry_run_alerts

        self.email_renderer = EmailRenderer(EmailConfig(
            smtp_host=config.secrets.smtp_host,
            smtp_port=config.secrets.smtp_port,
            smtp_user=config.secrets.smtp_user,
            smtp_password=config.secrets.smtp_password,
            smtp_from=config.secrets.smtp_from or config.secrets.smtp_user,
            smtp_to=config.secrets.smtp_to,
            dry_run=email_dry,
        ))
        self.telegram_renderer = TelegramRenderer(TelegramConfig(
            bot_token=config.secrets.telegram_bot_token,
            chat_id=config.secrets.telegram_chat_id,
            dry_run=tg_dry,
        ))
        self.throttle = AlertThrottle(ThrottleConfig(
            cooldown_minutes=config.alerts.cooldown_minutes,
        ))

        # P9.3.P3 Step D — CME enrichment (GCS Live Parquet + post-analysis).
        # Databento Historical API is no longer used (replaced by Live streamer → GCS).
        # Wire the enricher only when CME_GCS_BUCKET is set AND the CME channel is enabled.
        # Otherwise enricher=None → router dispatches with primary signal only.
        self.cme_enricher: CMEEnricher | None = None
        self.streamer_health: StreamerHealthMonitor | None = None
        # P12-B: keep cme_bucket on self — _register_channels re-references it
        # for the insider scanner gating.
        self.cme_bucket: str = (os.getenv("CME_GCS_BUCKET") or "").strip()
        if self.cme_bucket and config.channel_enabled(CHANNEL_CME):
            try:
                self.cme_enricher = CMEEnricher(
                    config=EnrichmentConfig(gcs_bucket=self.cme_bucket),
                )
                # Periodic monitor for streamer heartbeat (5-min cycle / 6-min stale / 1h throttle)
                self.streamer_health = StreamerHealthMonitor(
                    config=HealthMonitorConfig(gcs_bucket=self.cme_bucket),
                    email_renderer=self.email_renderer,
                    telegram_renderer=self.telegram_renderer,
                )
                logger.info(
                    "cme_enricher_enabled",
                    mode="gcs_live",
                    bucket=self.cme_bucket,
                )
            except Exception as e:
                logger.warning("cme_enricher_disabled", reason=str(e))
                self.cme_enricher = None
                self.streamer_health = None
        else:
            logger.info(
                "cme_enricher_disabled",
                reason="CME_GCS_BUCKET unset or CME channel disabled",
            )

        # P11(a).4.5 + P11(d) (user-decided lock 2026-04-21):
        #   system-level fusion email + URGENT telegram + heartbeat reminder
        #   all disabled. The per-channel dispatcher owns email/telegram.
        #   - email_enabled=False  → AlertRouter.dispatch skips the email block
        #   - telegram_enabled=False → skip both URGENT push and heartbeat
        #   AlertRouter only handles cross-tag bookkeeping (state_change tracking) +
        #   digest (digest is also a no-op when email_enabled=False).
        self.router = AlertRouter(
            throttle=self.throttle,
            email_renderer=self.email_renderer,
            telegram_renderer=self.telegram_renderer,
            signal_store=self.signal_store,
            emergency_heartbeat_hours=config.alerts.emergency_heartbeat_hours,
            cme_enricher=self.cme_enricher,
            email_enabled=False,
            telegram_enabled=False,
        )

        # Core
        self.registry = ChannelRegistry()
        self.state_manager = StateManager()
        self.metrics = MetricRegistry()
        self.health = HealthRegistry()

        # Channel weights — extract each _ChannelConfig.weight from config.channels
        weights = {
            name: getattr(config.channels, name).weight
            for name in ALL_CHANNELS
        }

        # ── P11(a).4 — per-channel email path (cooldown + plot + SMTP) ──
        # buffer: orchestrator pushes a snapshot every cycle; dispatcher uses
        # it as plot data when an alert fires (in-memory, no replay needed).
        self.timeline_buffer = LiveTimelineBuffer(max_age_hours=24)

        # P12-D — data source for alert PNG (price/volume panel). Each channel's
        # collector calls push_trade per trade → 1-min OHLCV + buy/sell-split bars.
        # Default retention 420 min (= 7h) safely covers the 6h email/telegram plot.
        self.alert_ohlc_buffer = AlertOhlcBuffer()

        # cooldown v2: per-(channel, symbol, tier) × 24h.
        self.channel_cooldown = ChannelAlertCooldown(
            cooldown_minutes=int(config.alerts.cooldown_minutes)
            if config.alerts.cooldown_minutes >= 1440
            else 1440  # P10.5 lock — production does not allow < 24h
        )

        # SMTP — reuse the daemon's existing dry_run decision (auto/explicit creds).
        # Do not use SmtpConfig.from_env(); the daemon is the single source of truth.
        recipients = tuple(
            r.strip()
            for r in (config.secrets.smtp_to or "").split(",")
            if r.strip()
        )
        self.channel_smtp_config = ChannelSmtpConfig(
            host=config.secrets.smtp_host,
            port=config.secrets.smtp_port,
            user=config.secrets.smtp_user,
            password=config.secrets.smtp_password,
            sender=config.secrets.smtp_from or config.secrets.smtp_user,
            recipients=recipients,
            dry_run=email_dry,
        )

        # Directory for saving alert PNG / .eml (.eml also lands here when dry_run).
        alerts_live_dir = data_root / "alerts_live"
        alerts_live_dir.mkdir(parents=True, exist_ok=True)

        # P11(d) — channel-level telegram dispatcher (EMERGENCY only).
        # Same dry_run policy as email (auto-dry if no creds; forced dry if
        # ANOMALY_DRY_RUN=true). cooldown shares the same self.channel_cooldown
        # instance → one alert stays silent on both email/telegram for 24h.
        self.channel_telegram_config = TelegramConfig(
            bot_token=config.secrets.telegram_bot_token,
            chat_id=config.secrets.telegram_chat_id,
            dry_run=tg_dry,
        )

        # P11(b).4 (lock 2026-04-23): add LLM similarity assessment to
        # EMERGENCY alerts. If OPENAI_API_KEY is missing, assessor.enabled=False
        # auto-disables → email still sends, just without the "LLM Assess" row.
        # RISK_OFF / WATCH are skipped by the dispatcher (cost saving).
        openai_key_for_llm = self.config.secrets.openai_api_key
        self.llm_alert_assessor: LLMAlertAssessor | None
        if openai_key_for_llm:
            self.llm_alert_assessor = LLMAlertAssessor(
                api_key=openai_key_for_llm,
                # P11(b).5 v0.4.6 — promote to frontier (user decision 2026-04-23):
                # EMERGENCY frequency is low (~2/day) so monthly cost diff < $0.20.
                # Better reasoning on borderline insider cases than mini, and
                # the comparison reasoning against 6 historical events is more refined.
                model="gpt-5.4",
            )
        else:
            logger.warning(
                "llm_alert_assessor_disabled",
                reason="OPENAI_API_KEY not set — EMERGENCY email will be sent "
                "without LLM Assess row (non-fatal)",
            )
            self.llm_alert_assessor = None

        # EMERGENCY -> X auto-post (opt-in).
        # Default OFF (conservative). Enabled only when ENABLE_X_AUTO_POST=true.
        # X_POST_DRY_RUN=true logs the thread text without calling the API (validation).
        x_post_enabled_env = self._parse_bool(os.getenv("ENABLE_X_AUTO_POST"))
        x_post_enabled = bool(x_post_enabled_env) if x_post_enabled_env is not None else False
        x_post_dry_env = self._parse_bool(os.getenv("X_POST_DRY_RUN"))
        x_post_dry = bool(x_post_dry_env) if x_post_dry_env is not None else True
        self.x_post_config = XPostConfig(enabled=x_post_enabled, dry_run=x_post_dry)
        self.x_post_credentials = XCredentials(
            api_key=config.secrets.x_api_key,
            api_key_secret=config.secrets.x_api_key_secret,
            access_token=config.secrets.x_api_access_token,
            access_token_secret=config.secrets.x_api_access_token_secret,
            # fallback: the existing x_api_bearer_token is also usable if it is a user-context token.
            bearer_token=config.secrets.x_post_bearer_token or config.secrets.x_api_bearer_token,
        )
        if self.x_post_config.enabled and not (
            self.x_post_credentials.has_oauth1 or self.x_post_credentials.has_bearer
        ):
            logger.warning(
                "x_auto_post_disabled",
                reason=(
                    "ENABLE_X_AUTO_POST=true but no credentials found "
                    "(need OAuth1 or user-context bearer token)"
                ),
            )
            self.x_post_config = XPostConfig(enabled=False, dry_run=True)
        else:
            logger.info(
                "x_auto_post_configured",
                enabled=self.x_post_config.enabled,
                dry_run=self.x_post_config.dry_run,
                auth_mode=(
                    "oauth1"
                    if self.x_post_credentials.has_oauth1
                    else "bearer" if self.x_post_credentials.has_bearer else "none"
                ),
            )

        self.channel_dispatcher = ChannelAlertDispatcher(
            cooldown=self.channel_cooldown,
            buffer=self.timeline_buffer,
            smtp_config=self.channel_smtp_config,
            out_root=alerts_live_dir,
            telegram_config=self.channel_telegram_config,
            telegram_emergency_only=True,  # P11(d) lock
            # P11(b).2 lock (2026-04-22): restore D11 design — do NOT send WATCH
            # emails immediately. WATCH is only tracked via GCS audit + weekly_review.sh.
            # Only RISK_OFF and above go to inbox. WATCH routes to the 06:00 PT digest if enabled later.
            email_min_tier=Tier.RISK_OFF,
            # P11(b).4 (lock 2026-04-23) — similarity score only on EMERGENCY.
            llm_assessor=self.llm_alert_assessor,
            x_post_config=self.x_post_config,
            x_credentials=self.x_post_credentials,
            # P12-D — price/volume panel data.
            ohlc_buffer=self.alert_ohlc_buffer,
        )

        self.orchestrator = AnomalyOrchestrator(
            registry=self.registry,
            signal_store=self.signal_store,
            decision_store=self.decision_store,
            state_manager=self.state_manager,
            weights=weights,
            alert_router=self.router,
            metrics=self.metrics,
            fusion_interval_s=5.0,
            timeline_buffer=self.timeline_buffer,
            channel_dispatcher=self.channel_dispatcher,
        )

        # Metadata (for reporting)
        self._started_at: datetime | None = None
        self._alert_modes = {
            "email_dry_run": email_dry,
            "telegram_dry_run": tg_dry,
            "x_post_enabled": self.x_post_config.enabled,
            "x_post_dry_run": self.x_post_config.dry_run,
        }

        # P9.3.P0.C — TradingView webhook adapter (enabled only when secret is present).
        #   Without secret the endpoint is still registered but rejects with 503 (safe when config is missing).
        tv_secret = (config.secrets.tradingview_webhook_secret or "").strip()
        self.tradingview_adapter: TradingViewAdapter | None
        if len(tv_secret) >= 8:
            self.tradingview_adapter = TradingViewAdapter(webhook_secret=tv_secret)
            self._tv_webhook_enabled = True
        else:
            self.tradingview_adapter = None
            self._tv_webhook_enabled = False
        # Counters — webhook ops monitoring (exposed via snapshot)
        self._tv_webhook_counts: dict[str, int] = {
            "received": 0, "accepted": 0, "rejected_secret": 0,
            "rejected_validation": 0, "rejected_unknown_symbol": 0,
            "rejected_channel_off": 0,
        }

    @staticmethod
    def _parse_bool(s: str | None) -> bool | None:
        if s is None:
            return None
        return s.strip().lower() in ("1", "true", "yes", "on")

    # ─────────────────────────────────────────────────────────────────
    # Channel registration (P2~P5)
    # ─────────────────────────────────────────────────────────────────
    def _register_channels(self) -> None:
        """Register enabled channels into the registry based on config.channels.* / watchlist.*.

        v1 walking-skeleton — only polymarket is implemented today. Other channels are PnP:
            if config.channel_enabled(CHANNEL_HYPERLIQUID):
                self.registry.register(HyperliquidChannel(...))

        If the watchlist is empty the channel is still registered but runs idle
        (PolymarketChannel.start logs an empty-slug message and the polling loop idles).
        """
        # ── Polymarket ──
        if self.config.channel_enabled(CHANNEL_POLYMARKET):
            slugs = self.config.watchlist.polymarket_markets
            ch = PolymarketChannel(
                market_slugs=slugs,
                raw_store=self.raw_store,
                feature_store=self.feature_store,
                baseline_store=self.polymarket_baseline_store,
                ohlc_buffer=self.alert_ohlc_buffer,
            )
            self.registry.register(ch)
            logger.info("channel_registered", channel=CHANNEL_POLYMARKET,
                        slugs=len(slugs))
        else:
            logger.info("channel_disabled", channel=CHANNEL_POLYMARKET)

        # ── Hyperliquid ──
        if self.config.channel_enabled(CHANNEL_HYPERLIQUID):
            coins = self.config.watchlist.hyperliquid_assets
            ch_hl = HyperliquidChannel(
                coins=coins,
                raw_store=self.raw_store,
                feature_store=self.feature_store,
                wallet_store=self.hl_wallet_store,  # P9.2.P2
                ohlc_buffer=self.alert_ohlc_buffer,
            )
            self.registry.register(ch_hl)
            logger.info("channel_registered", channel=CHANNEL_HYPERLIQUID,
                        coins=len(coins))
        else:
            logger.info("channel_disabled", channel=CHANNEL_HYPERLIQUID)

        # ── CME (P4 walking-skeleton — mock collector + env-gated spike) ──
        # P12-B: add insider scanner (24/7 GCS poll → cme_insider_v1).
        # Enabled only when ENABLE_CME_INSIDER_SCANNER=true AND CME_GCS_BUCKET is set.
        # Runs ES (vol_z 6σ) / BZ / CL detection.
        # GC is excluded from DEFAULT_INSIDER_THRESHOLDS so it automatically returns NORMAL.
        if self.config.channel_enabled(CHANNEL_CME):
            symbols = self.config.watchlist.cme_symbols

            # insider scanner gate — env + bucket must both be present to enable.
            insider_env = (os.getenv("ENABLE_CME_INSIDER_SCANNER", "false")
                           .strip().lower())
            insider_enabled = (insider_env in ("1", "true", "yes")
                               and bool(self.cme_bucket))
            insider_cfg = None
            if insider_enabled:
                # Use the same bucket as the enricher.
                insider_cfg = InsiderScannerConfig(gcs_bucket=self.cme_bucket)

            ch_cme = CMEChannel(
                symbols=symbols,
                raw_store=self.raw_store,
                feature_store=self.feature_store,
                enable_insider_scanner=insider_enabled,
                insider_scanner_config=insider_cfg,
                ohlc_buffer=self.alert_ohlc_buffer,
            )
            self.registry.register(ch_cme)
            logger.info(
                "channel_registered", channel=CHANNEL_CME,
                symbols=len(symbols),
                mock_spike=ch_cme._collector.spike_enabled,
                insider_scanner=insider_enabled,
                insider_bucket=self.cme_bucket if insider_enabled else None,
            )
        else:
            logger.info("channel_disabled", channel=CHANNEL_CME)

        # ── X (P9.4 — Stage1Filter + LLMClassifier pipeline) ──
        # The new pipeline fully replaces the v0 keyword detector (architecture §EVT-1).
        #
        # Collector selection policy:
        #   - X_USE_MOCK_COLLECTOR=true  → MockXCollector (default; safe, $0)
        #   - X_USE_MOCK_COLLECTOR=false → XCollector (X API v2 Bearer single-path, PAYG)
        #
        # If OPENAI_API_KEY is empty the channel is auto-disabled
        # (fail-fast is safer than letting LLMClassifier crash on lazy init).
        if self.config.channel_enabled(CHANNEL_X):
            x_accounts = self.config.watchlist.x_accounts
            openai_key = self.config.secrets.openai_api_key

            if not openai_key:
                logger.warning(
                    "channel_x_skipped",
                    reason="OPENAI_API_KEY not set — X channel requires LLM classifier",
                )
            elif not x_accounts:
                logger.warning(
                    "channel_x_skipped",
                    reason="watchlist.x_accounts is empty",
                )
            else:
                # env flag — real X API calls must be explicitly enabled to incur cost
                use_mock_env = os.getenv("X_USE_MOCK_COLLECTOR", "true").strip().lower()
                use_mock = use_mock_env in ("1", "true", "yes", "on")

                if use_mock:
                    collector = MockXCollector(accounts=x_accounts)
                    collector_label = "mock"
                else:
                    collector = XCollector(
                        accounts=x_accounts,
                        x_api_bearer_token=self.config.secrets.x_api_bearer_token or None,
                    )
                    collector_label = "real"

                ch_x = XChannel(
                    collector=collector,
                    accounts=x_accounts,
                    openai_api_key=openai_key,
                    raw_store=self.raw_store,
                )
                self.registry.register(ch_x)
                logger.info(
                    "channel_registered",
                    channel=CHANNEL_X,
                    accounts=len(x_accounts),
                    collector=collector_label,
                )
        else:
            logger.info("channel_disabled", channel=CHANNEL_X)

        # ── Truth Social (Channel 5, Step 3 — LLM scorer pipeline) ────
        # Polls new Trump Truth Social posts every 5 minutes; ReferenceDB
        # hybrid retrieval (keyword + embedding) + GPT-5.4 scorer compute the
        # market-impact score (0-10) → emit ChannelSignal.
        #
        # Auto-disabled without OPENAI_API_KEY (same policy as X channel).
        # If watchlist.truth_social_accounts is empty, default to ["realDonaldTrump"].
        if self.config.channel_enabled(CHANNEL_TRUTH_SOCIAL):
            openai_key = self.config.secrets.openai_api_key
            if not openai_key:
                logger.warning(
                    "channel_truth_social_skipped",
                    reason="OPENAI_API_KEY not set — Truth Social requires LLM scorer",
                )
            else:
                # v1 is single-account (Trump) only — use the collector default as-is.
                # Other watchlist accounts arrive in v2 when multi-account is supported.
                ts_accounts = self.config.watchlist.truth_social_accounts or ["realDonaldTrump"]
                ch_truth = TruthSocialChannel(
                    openai_api_key=openai_key,
                    raw_store=self.raw_store,
                    enable_embedding=True,
                )
                self.registry.register(ch_truth)
                logger.info(
                    "channel_registered",
                    channel=CHANNEL_TRUTH_SOCIAL,
                    accounts=len(ts_accounts),
                    model="gpt-5.4",
                )
        else:
            logger.info("channel_disabled", channel=CHANNEL_TRUTH_SOCIAL)

    # ─────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Spin up all background tasks. Returns immediately."""
        self._stop_event = asyncio.Event()
        self._started_at = datetime.now(timezone.utc)

        logger.info("daemon_starting", env=self.config.env, alert_modes=self._alert_modes)

        # ── Register channels (P2~P5) ─────────────────────────────────
        self._register_channels()

        # 1) Orchestrator (fusion loop spawned internally)
        await self.orchestrator.start()

        # 2) Heartbeat loop — disabled since P11(d). AlertRouter.telegram_enabled
        #    is False, so emit_heartbeat_if_due returns [] immediately. We do not
        #    even spawn the task, eliminating polling overhead. The 24h cooldown_expired
        #    acts as a natural reminder (per-channel dispatcher re-sends the same alert).
        # self._tasks.append(asyncio.create_task(
        #     self._heartbeat_loop(), name="heartbeat-loop",
        # ))

        # 3) Digest loop (flush WATCH digest at Bay Area 06:00)
        self._tasks.append(asyncio.create_task(
            self._digest_loop(), name="digest-loop",
        ))

        # 4) Streamer health monitor — checks GCS heartbeat every 5 min.
        #    Wired as a pair with the enricher — exists only when bucket is set on both.
        if self.streamer_health is not None:
            self._tasks.append(asyncio.create_task(
                self.streamer_health.run(), name="streamer-health",
            ))

        # 5) HTTP health server (Cloud Run requires PORT listen)
        await self._start_http_server()

        logger.info("daemon_started",
                    channels_registered=len(self.registry.names()),
                    bg_tasks=len(self._tasks))

    async def stop(self) -> None:
        """Called on SIGTERM — graceful shutdown."""
        if self._stop_event is None:
            return
        if self._stop_event.is_set():
            return

        logger.info("daemon_stopping")
        self._stop_event.set()

        # 0) Streamer health monitor — signal stop first so task cancellation is graceful
        if self.streamer_health is not None:
            try:
                self.streamer_health.stop()
            except Exception as e:
                logger.warning("streamer_health_stop_failed", error=str(e))

        # 1) Orchestrator stop (fusion loop + registered channels)
        try:
            await self.orchestrator.stop()
        except Exception as e:
            logger.warning("orchestrator_stop_failed", error=str(e))

        # 2) Cancel + wait for background tasks
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        # 3) HTTP server stop
        if self._http_runner is not None:
            await self._http_runner.cleanup()

        # 4) Flush Parquet stores — in-memory buffer → disk (raw + feature)
        try:
            n_raw = self.raw_store.flush()
            if n_raw:
                logger.info("raw_store_flushed", per_channel=n_raw)
        except Exception as e:
            logger.warning("raw_store_flush_failed", error=str(e))
        try:
            n_feat = self.feature_store.flush()
            if n_feat:
                logger.info("feature_store_flushed", per_channel=n_feat)
        except Exception as e:
            logger.warning("feature_store_flush_failed", error=str(e))

        # 5) Close SQLite stores
        try:
            self.signal_store.close()
        except Exception as e:
            logger.warning("signal_store_close_failed", error=str(e))
        try:
            self.decision_store.close()
        except Exception as e:
            logger.warning("decision_store_close_failed", error=str(e))
        try:
            # 14d retention cutoff — must match retention_days from store init
            cutoff = datetime.now(timezone.utc) - timedelta(days=14)
            n_pruned = self.polymarket_baseline_store.prune_older_than(cutoff)
            self.polymarket_baseline_store.close()
            if n_pruned:
                logger.info("polymarket_baseline_pruned", rows=n_pruned)
        except Exception as e:
            logger.warning("polymarket_baseline_close_failed", error=str(e))

        try:
            # P9.2.P2 — wallet store: trades 1h, wallets 7d retention
            now_utc = datetime.now(timezone.utc)
            n_trades_pruned = self.hl_wallet_store.prune_trades_older_than(
                now_utc - timedelta(hours=1)
            )
            n_wallets_pruned = self.hl_wallet_store.prune_wallets_older_than(
                now_utc - timedelta(days=7)
            )
            self.hl_wallet_store.close()
            if n_trades_pruned or n_wallets_pruned:
                logger.info(
                    "hl_wallet_pruned",
                    trades=n_trades_pruned,
                    wallets=n_wallets_pruned,
                )
        except Exception as e:
            logger.warning("hl_wallet_close_failed", error=str(e))

        logger.info("daemon_stopped")

    async def run_until_stop(self) -> None:
        """Called from main() — start, await stop_event, then stop."""
        await self.start()
        assert self._stop_event is not None
        try:
            await self._stop_event.wait()
        finally:
            await self.stop()

    def request_stop(self) -> None:
        """Called from SIGTERM/SIGINT handlers — sets stop_event only (sync-safe)."""
        if self._stop_event is not None and not self._stop_event.is_set():
            logger.info("stop_requested")
            self._stop_event.set()

    # ─────────────────────────────────────────────────────────────────
    # Background loops
    # ─────────────────────────────────────────────────────────────────
    async def _heartbeat_loop(self) -> None:
        """Calls router.emit_heartbeat_if_due() every 60s."""
        assert self._stop_event is not None
        interval_s = 60.0
        while not self._stop_event.is_set():
            try:
                reminded = await self.router.emit_heartbeat_if_due()
                if reminded:
                    logger.info("heartbeat_reminded", symbols=reminded)
                    self.metrics.counter("heartbeat_reminders_total").inc(len(reminded))
            except Exception as e:
                logger.exception("heartbeat_loop_error", error=str(e))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                pass  # normal — proceed to next cycle

    async def _digest_loop(self) -> None:
        """Calls router.flush_digest_now() at Bay Area `digest_time_local`."""
        assert self._stop_event is not None
        tz = ZoneInfo(self.config.alerts.digest_timezone)
        try:
            hh, mm = (int(p) for p in self.config.alerts.digest_time_local.split(":"))
        except Exception:
            logger.warning("digest_time_parse_failed",
                           value=self.config.alerts.digest_time_local,
                           fallback="06:00")
            hh, mm = 6, 0

        while not self._stop_event.is_set():
            now_local = datetime.now(tz)
            next_run = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if next_run <= now_local:
                next_run += timedelta(days=1)
            wait_s = (next_run - now_local).total_seconds()

            logger.info("digest_scheduled", next_run_iso=next_run.isoformat(),
                        wait_seconds=int(wait_s))
            try:
                # exit immediately if stop_event fires (graceful)
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_s)
                break  # stop_event fired → end loop
            except asyncio.TimeoutError:
                pass

            try:
                n = await self.router.flush_digest_now()
                logger.info("digest_flushed", entries=n)
                if n:
                    self.metrics.counter("digest_flushes_total").inc()
            except Exception as e:
                logger.exception("digest_flush_failed", error=str(e))

    # ─────────────────────────────────────────────────────────────────
    # HTTP health server (Cloud Run probe + /metrics + /snapshot)
    # ─────────────────────────────────────────────────────────────────
    async def _start_http_server(self) -> None:
        port = int(os.getenv("PORT", "8080"))
        host = "0.0.0.0"

        app = web.Application()
        app.router.add_get("/", self._h_root)
        app.router.add_get("/health", self._h_healthz)
        app.router.add_get("/healthz", self._h_healthz)
        # NOTE:
        #   Cloud Run run.app edge occasionally intercepts exact "/healthz".
        #   Keep compatibility aliases so operators can use a stable live check.
        app.router.add_get("/healthz/", self._h_healthz)
        app.router.add_get("/livez", self._h_healthz)
        app.router.add_get("/ready", self._h_readyz)
        app.router.add_get("/readyz", self._h_readyz)
        app.router.add_get("/metrics", self._h_metrics)
        app.router.add_get("/snapshot", self._h_snapshot)
        # P9.3.P0.C — TradingView webhook (primary CME trigger).
        # POST: external alerts → CMEChannel.ingest_external_event → detector evaluates next cycle.
        app.router.add_post("/webhook/tradingview", self._h_tv_webhook)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        self._http_runner = runner
        logger.info("http_server_listening", host=host, port=port)

    async def _h_root(self, _req: web.Request) -> web.Response:
        return web.Response(text="anomaly-daemon ok\n")

    async def _h_healthz(self, _req: web.Request) -> web.Response:
        # liveness: is the orchestrator alive?
        ok = self.orchestrator.is_running
        return web.json_response(
            {
                "ok": ok,
                "uptime_s": self._uptime_s(),
                "cycles_run": self.orchestrator.cycles_run,
            },
            status=200 if ok else 503,
        )

    async def _h_readyz(self, _req: web.Request) -> web.Response:
        # readiness: are stores + HTTP server ready? (always OK once start finishes)
        ready = self._started_at is not None
        return web.json_response({"ready": ready}, status=200 if ready else 503)

    async def _h_metrics(self, _req: web.Request) -> web.Response:
        return web.json_response(self.metrics.snapshot())

    async def _h_snapshot(self, _req: web.Request) -> web.Response:
        # P9.1 — also expose baseline_store progress (to verify it is filling in operation)
        try:
            pbs_total = self.polymarket_baseline_store.total_rows()
            pbs_latest = self.polymarket_baseline_store.latest_bucket_end()
            pbs_info: dict[str, Any] = {
                "total_rows": pbs_total,
                "latest_bucket_end": pbs_latest.isoformat() if pbs_latest else None,
            }
        except Exception as e:
            pbs_info = {"error": str(e)}

        # P9.2.P2 — hl_wallet_store progress
        try:
            hlw_info: dict[str, Any] = {
                "total_wallets": self.hl_wallet_store.total_wallets(),
                "total_trades": self.hl_wallet_store.total_trades(),
                "started_at_ms": self.hl_wallet_store.started_at_ms,
                "latest_trade_ms": self.hl_wallet_store.latest_trade_ms(),
            }
        except Exception as e:
            hlw_info = {"error": str(e)}

        # P9.3.P3 Step D — CME enricher + streamer health status (GCS-live mode)
        cme_info: dict[str, Any] = {
            "enricher_enabled": self.cme_enricher is not None,
            "mode": "gcs_live" if self.cme_enricher else "disabled",
        }
        if self.streamer_health is not None:
            try:
                cme_info["streamer_health"] = self.streamer_health.snapshot()
            except Exception as e:
                cme_info["streamer_health"] = {"error": str(e)}

        # P11(a/d).4 — per-channel alert dispatcher stats (for ops health)
        # email + telegram unified — telegram_emitted / telegram_skipped_tier /
        # telegram_errors are all together inside stats.
        ch_dispatch_info: dict[str, Any] = {
            "stats": self.channel_dispatcher.stats,
            "buffer_size": self.timeline_buffer.size,
            "buffer_max_age_h": self.timeline_buffer.max_age_hours,
            "cooldown_minutes": self.channel_cooldown.cooldown_minutes,
            "active_cooldown_keys": self.channel_cooldown.snapshot_active_keys(),
            "smtp_dry_run": self.channel_smtp_config.dry_run,
            "smtp_recipients": list(self.channel_smtp_config.recipients),
            "telegram_enabled": self.channel_dispatcher.telegram_enabled,
            "telegram_dry_run": self.channel_telegram_config.dry_run,
            "telegram_emergency_only": True,  # P11(d) lock
        }

        return web.json_response({
            "router": self.router.snapshot(),
            "state": {
                "current": self.state_manager.current_state.value,
                "pending": self._pending_to_dict(),
            },
            "channels": {
                "registered": self.registry.names(),
                "running": [n for n in self.registry.names()
                            if self.registry.get(n) and self.registry.get(n).is_running],
            },
            "polymarket_baseline": pbs_info,
            "hl_wallet": hlw_info,
            "tradingview_webhook": {
                "enabled": self._tv_webhook_enabled,
                "counts": dict(self._tv_webhook_counts),
            },
            "cme_enricher": cme_info,
            "channel_dispatcher": ch_dispatch_info,
            "uptime_s": self._uptime_s(),
            "cycles_run": self.orchestrator.cycles_run,
            "alert_modes": self._alert_modes,
        })

    # ─────────────────────────────────────────────────────────────────
    # TradingView webhook handler (P9.3.P0.C)
    # ─────────────────────────────────────────────────────────────────
    async def _h_tv_webhook(self, req: web.Request) -> web.Response:
        """POST /webhook/tradingview — receive TradingView alert.

        Flow (option A):
          1) JSON parse → adapter.parse_webhook() (secret + schema validation)
          2) CMEChannel.ingest_external_event(raw, normalized)
          3) Detector evaluates on next polling cycle (≤ 5s) → fusion → router

        Response codes:
          200  — accepted (detector will evaluate next cycle)
          400  — payload violates our schema (TV alert message authored incorrectly)
          401  — secret mismatch
          404  — unknown symbol root (outside watchlist)
          503  — webhook disabled (TRADINGVIEW_WEBHOOK_SECRET unset) or CME channel off
        """
        # (a) is the webhook itself disabled? (no secret in config)
        if not self._tv_webhook_enabled or self.tradingview_adapter is None:
            return web.json_response(
                {"error": "tradingview_webhook_disabled",
                 "hint": "set TRADINGVIEW_WEBHOOK_SECRET env var"},
                status=503,
            )

        # (b) is the CME channel present in the registry?
        cme_channel = self.registry.get(CHANNEL_CME)
        if cme_channel is None or not isinstance(cme_channel, CMEChannel):
            self._tv_webhook_counts["rejected_channel_off"] += 1
            return web.json_response(
                {"error": "cme_channel_not_registered"},
                status=503,
            )

        self._tv_webhook_counts["received"] += 1

        # (c) JSON parse — ignore content-type and try the body as-is (TV often sends text/plain)
        try:
            payload = await req.json()
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
        except Exception as e:
            self._tv_webhook_counts["rejected_validation"] += 1
            logger.warning("tv_webhook_bad_json", error=str(e))
            return web.json_response(
                {"error": "invalid_json", "detail": str(e)[:200]},
                status=400,
            )

        # (d) adapter.parse_webhook — validates secret + Pydantic + ticker root all at once.
        #     Note: pydantic.ValidationError inherits from ValueError → catch
        #     ValidationError first to distinguish from the ticker-root ValueError below.
        try:
            raw, normalized = self.tradingview_adapter.parse_webhook(payload)
        except PermissionError:
            self._tv_webhook_counts["rejected_secret"] += 1
            return web.json_response({"error": "secret_mismatch"}, status=401)
        except ValidationError as e:
            # payload schema violation (missing required field, type mismatch, unknown trigger, etc.)
            self._tv_webhook_counts["rejected_validation"] += 1
            logger.warning("tv_webhook_validation_failed", error=str(e))
            return web.json_response(
                {"error": "validation_failed", "detail": str(e)[:300]},
                status=400,
            )
        except ValueError as e:
            # raised in _parse_root_symbol — ticker root is outside our watchlist
            self._tv_webhook_counts["rejected_unknown_symbol"] += 1
            return web.json_response(
                {"error": "unknown_symbol", "detail": str(e)[:200]},
                status=404,
            )
        except Exception as e:
            # unexpected error — return 500 and log carefully
            logger.exception("tv_webhook_parse_unexpected", error=str(e))
            return web.json_response(
                {"error": "internal_parse_error", "detail": str(e)[:200]},
                status=500,
            )

        # (e) inject into the channel — at this point the event is trusted
        try:
            cme_channel.ingest_external_event(raw, normalized)
        except ValueError as e:
            # symbol is not in the watchlist (adapter validates root; channel validates self._symbols)
            self._tv_webhook_counts["rejected_unknown_symbol"] += 1
            return web.json_response(
                {"error": "symbol_not_in_watchlist", "detail": str(e)[:200]},
                status=404,
            )
        except Exception as e:
            logger.exception("tv_webhook_ingest_failed", error=str(e))
            return web.json_response(
                {"error": "internal_ingest_error", "detail": str(e)[:200]},
                status=500,
            )

        self._tv_webhook_counts["accepted"] += 1
        self.metrics.counter("tv_webhook_accepted_total").inc()
        return web.json_response(
            {
                "ok": True,
                "symbol": normalized.symbol,
                "trigger": normalized.meta.get("trigger"),
                "ts_source": normalized.ts_source.isoformat(),
                "size_usd": normalized.size_usd,
                "raw_id": raw.id,
            },
            status=200,
        )

    def _pending_to_dict(self) -> dict | None:
        p = self.state_manager.pending
        if p is None:
            return None
        tier, since = p
        return {"tier": tier.value, "since": since.isoformat()}

    def _uptime_s(self) -> float:
        if self._started_at is None:
            return 0.0
        return (datetime.now(timezone.utc) - self._started_at).total_seconds()


# ─────────────────────────────────────────────────────────────────────
# main()
# ─────────────────────────────────────────────────────────────────────
async def main() -> int:
    env = os.getenv("ANOMALY_ENV", "local")
    setup_logging(env)
    logger.info("startup_begin", env=env, port=os.getenv("PORT", "8080"))

    try:
        config = load_config(env=env)
    except Exception as e:
        logger.exception("config_load_failed", error=str(e))
        return 1

    daemon = AnomalyDaemon(config)

    # SIGTERM (Cloud Run) / SIGINT (Ctrl-C) → graceful stop
    #
    # Note: asyncio.add_signal_handler silently no-ops on some platforms
    # (notably macOS) — registration succeeds but the callback never fires.
    # So we use the classic signal.signal() as primary and asyncio's API
    # only as a fallback. signal.signal handlers run on the main thread,
    # so use loop.call_soon_threadsafe to wake the coroutine safely.
    loop = asyncio.get_running_loop()

    def _sync_signal_handler(signum: int, _frame: Any) -> None:
        try:
            loop.call_soon_threadsafe(daemon.request_stop)
        except RuntimeError:
            # the loop may already have shut down — ignore (we'll die soon anyway)
            pass

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _sync_signal_handler)
        except (ValueError, OSError):
            # not on main thread or platform-unsupported — skip
            pass
        # fallback: also try the asyncio API (harmless when both work, e.g. on Linux)
        try:
            loop.add_signal_handler(sig, daemon.request_stop)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await daemon.run_until_stop()
        return 0
    except Exception as e:
        logger.exception("daemon_unhandled_exception", error=str(e))
        # attempt graceful stop
        try:
            await daemon.stop()
        except Exception:
            pass
        return 1


def entrypoint() -> None:
    """sync wrapper — invoked by `python -m anomaly.entrypoints.anomaly_daemon`."""
    sys.exit(asyncio.run(main()))


if __name__ == "__main__":
    entrypoint()
