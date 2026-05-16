"""
core/config.py — Anomaly-subsystem-specific settings (load + validate).

Fully separate from the legacy src/config.py (for RAG). Built on Pydantic Settings.

────────────────────────────────────────────────────────────────────────
Secret loading policy (architecture §6.1):

  Local dev    → loaded from .env
  Cloud Run    → Google Secret Manager injects env vars → used automatically
                 (works with OS env vars even without .env)

────────────────────────────────────────────────────────────────────────
Structure:

  AnomalyConfig (top-level)
   ├─ env             # "local" | "cloud_run"
   ├─ secrets         # SMTP / Telegram / API key (.env or Secret Manager)
   ├─ channels        # per-channel enable / weight
   ├─ watchlist       # loaded from a YAML file (D10 — user-editable)
   ├─ cost            # D13 PAYG cap, thresholds, kill-switch override
   ├─ storage         # base_path, retention
   └─ alerts          # digest time, cooldown, heartbeat interval

────────────────────────────────────────────────────────────────────────
Usage:

  from anomaly_detection.core.config import load_config
  cfg = load_config()
  print(cfg.cost.payg_cap_usd)        # 1000.0
  print(cfg.watchlist.cme_symbols)    # ["CL", "GC", ...]

Architecture: §6.1 Configuration
Plan: §11 D10 (symbols universe), D13 (cost ceiling)
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .schemas import ALL_CHANNELS


# =====================================================================
# Default file paths (relative to project root)
# =====================================================================
# config.py lives at src/anomaly/core/config.py, so ../../../ is the project root.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DATA_PATH = _PROJECT_ROOT / "data" / "anomaly"
_DEFAULT_WATCHLIST_PATH = _PROJECT_ROOT / "config" / "watchlist.yaml"
_DEFAULT_ENV_FILE = _PROJECT_ROOT / ".env"


# =====================================================================
# 1. Secrets — loaded from .env / environment vars (BaseSettings)
# =====================================================================
class SecretsConfig(BaseSettings):
    """All external service credentials.

    Local dev → auto-loaded from .env.
    Cloud Run → injected by Secret Manager as OS env vars → used directly.

    The field name is the env var name (uppercased).
    Example: smtp_host  →  SMTP_HOST
    """

    model_config = SettingsConfigDict(
        env_file=str(_DEFAULT_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",  # ignore unrelated keys in .env (RAG OPENAI_API_KEY etc.)
        case_sensitive=False,
    )

    # ── Email (Gmail SMTP) ──
    smtp_host: str = Field(default="smtp.gmail.com")
    smtp_port: int = Field(default=587)
    smtp_user: str = Field(default="", description="Gmail account (e.g. me@gmail.com)")
    smtp_password: str = Field(default="", description="Gmail App Password (NOT the normal password)")
    smtp_from: str = Field(default="", description="From address (usually same as smtp_user)")
    smtp_to: str = Field(default="", description="To address — recipient for alerts")

    # ── Telegram bot ──
    telegram_bot_token: str = Field(default="", description="Issued by @BotFather")
    telegram_chat_id: str = Field(default="", description="Personal chat id (numeric)")

    # ── External API keys (used from P4+) ──
    databento_api_key: str = Field(default="", description="P4 — CME data (PAYG)")
    openai_api_key: str = Field(
        default="",
        description="P9.4 — X channel LLM classifier (GPT-5.4, PAYG). "
                    "If empty, the daemon auto-disables the X channel.",
    )
    unusualwhales_api_key: str = Field(default="", description="P4 — options flow (subscription)")
    dune_api_key: str = Field(default="", description="P2 — Polymarket backfill")
    x_api_bearer_token: str = Field(
        default="",
        description="P9.4 / EVT-1 — X (Twitter) API v2 Bearer token. "
                    "Real XCollector's single path (X API only). "
                    "If empty, X channel collector goes idle.",
    )
    # Credentials for X auto-post (EMERGENCY outbound).
    # When all 4 OAuth1 values are present, this is the preferred posting path.
    x_api_key: str = Field(
        default="",
        description="X API key (consumer key) — outbound posting OAuth1",
    )
    x_api_key_secret: str = Field(
        default="",
        description="X API key secret (consumer secret) — outbound posting OAuth1",
    )
    x_api_access_token: str = Field(
        default="",
        description="X API access token — outbound posting OAuth1",
    )
    x_api_access_token_secret: str = Field(
        default="",
        description="X API access token secret — outbound posting OAuth1",
    )
    # OAuth2 user-context bearer fallback. (App-only bearer cannot post.)
    x_post_bearer_token: str = Field(
        default="",
        description="Optional user-context bearer token for X posting fallback",
    )

    # ── TradingView webhook (incoming) ──
    tradingview_webhook_secret: str = Field(default="", description="Shared secret for webhook auth")


# =====================================================================
# 2. Per-channel settings
# =====================================================================
class _ChannelConfig(BaseModel):
    """Operational flag for one channel.

    `weight` is the base weight fed into the fusion engine's noisy-OR
    (the effective weight is `weight × health` at runtime — architecture §5.2).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    weight: float = Field(default=1.0, ge=0.0, le=1.0)


class ChannelsConfig(BaseModel):
    """Per-channel settings for the 5 channels. weights are tuned in P9."""

    model_config = ConfigDict(extra="forbid")

    polymarket: _ChannelConfig = Field(default_factory=_ChannelConfig)
    hyperliquid: _ChannelConfig = Field(default_factory=_ChannelConfig)
    cme: _ChannelConfig = Field(default_factory=_ChannelConfig)
    x: _ChannelConfig = Field(default_factory=_ChannelConfig)
    truth_social: _ChannelConfig = Field(default_factory=_ChannelConfig)


# =====================================================================
# 3. Watchlist (D10) — loaded from YAML (user-editable)
# =====================================================================
class WatchlistConfig(BaseModel):
    """Lists of interesting symbols / markets / wallets / accounts (architecture §6.1).

    Loaded from YAML. Users can edit config/watchlist.yaml anytime.
    The daemon reads it only at startup (a restart is required to pick up edits — v1).
    """

    model_config = ConfigDict(extra="forbid")

    polymarket_markets: list[str] = Field(
        default_factory=list,
        description='Polymarket market slug or condition_id (e.g. "iran-strike-by-feb28")',
    )
    hyperliquid_assets: list[str] = Field(
        default_factory=list,
        description='Hyperliquid asset name (e.g. "BTC", "ETH", "SOL")',
    )
    cme_symbols: list[str] = Field(
        default_factory=list,
        description='CME futures symbol (e.g. "CL" WTI, "GC" Gold, "ES" S&P futures)',
    )
    x_accounts: list[str] = Field(
        default_factory=list,
        description='X handle (without @, e.g. "Lookonchain")',
    )
    truth_social_accounts: list[str] = Field(
        default_factory=lambda: ["realDonaldTrump"],
        description=(
            "Truth Social account handle (without @). Default is Donald Trump only. "
            "v1 supports a single account only (P11(f) — multi-account is follow-up)."
        ),
    )


# =====================================================================
# 4. Cost ceiling & kill-switch (D13)
# =====================================================================
class CostConfig(BaseModel):
    """D13 — PAYG cost ceiling + threshold alerts + kill-switch."""

    model_config = ConfigDict(extra="forbid")

    payg_cap_usd: float = Field(
        default=1000.0, gt=0.0,
        description="Monthly combined PAYG cap. Subscription costs are not counted.",
    )
    alert_thresholds: list[float] = Field(
        default_factory=lambda: [0.10, 0.20, 0.40, 0.60, 0.80, 1.00],
        description="cumulative threshold ratios. Each cross sends a separate alert email.",
    )
    kill_switch_override: bool = Field(
        default=False,
        description="When True, do not auto-disable PAYG even at 100% (manual operation)",
    )
    x_api_basic_enabled: bool = Field(
        default=False,
        description="EVT-1 (D8) — whether X official API Basic ($200/month subscription) is enabled",
    )


# =====================================================================
# 5. Storage paths + retention
# =====================================================================
class StorageConfig(BaseModel):
    """Sub-paths under data/anomaly/ + retention policy (architecture §4.2)."""

    model_config = ConfigDict(extra="forbid")

    base_path: Path = Field(default=_DEFAULT_DATA_PATH)
    raw_retention_days: int = Field(default=7, gt=0)
    feature_retention_days: int = Field(default=30, gt=0)


# =====================================================================
# 6. Alerts — digest time, cooldown, heartbeat
# =====================================================================
class AlertsConfig(BaseModel):
    """Operational settings for push notifications (architecture §6.5)."""

    model_config = ConfigDict(extra="forbid")

    digest_time_local: str = Field(default="06:00", description="WATCH digest send time (24h)")
    digest_timezone: str = Field(
        default="America/Los_Angeles",
        description="Bay Area timezone (handles DST automatically)",
    )
    cooldown_minutes: int = Field(
        default=5, ge=0,
        description="Re-alert cooldown for the same symbol (§6.5.3)",
    )
    batch_window_minutes: int = Field(
        default=5, ge=1,
        description="Window for bundling alerts of multiple symbols into one message (§6.5.2)",
    )
    timeline_window_minutes: int = Field(
        default=30, ge=1,
        description="Timeline view window in the email body",
    )
    emergency_heartbeat_hours: int = Field(
        default=1, ge=1,
        description="Reminder if EMERGENCY persists for N hours (§6.5.5)",
    )


# =====================================================================
# 7. Top-level AnomalyConfig
# =====================================================================
class AnomalyConfig(BaseModel):
    """Single object holding every runtime setting of the anomaly subsystem.

    On daemon startup, load_config() is called once → injected into every component.
    """

    model_config = ConfigDict(extra="forbid")

    env: str = Field(default="local", description='"local" | "cloud_run"')
    secrets: SecretsConfig
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    watchlist: WatchlistConfig = Field(default_factory=WatchlistConfig)
    cost: CostConfig = Field(default_factory=CostConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)

    def channel_enabled(self, channel_name: str) -> bool:
        """Is the given channel enabled? Helper for avoiding typos.

        Args:
            channel_name: one of the schemas.CHANNEL_* constants.

        Returns:
            bool. False for unknown channel names.
        """
        if channel_name not in ALL_CHANNELS:
            return False
        return getattr(self.channels, channel_name).enabled


# =====================================================================
# Loader functions
# =====================================================================
def _load_watchlist_yaml(path: Path) -> WatchlistConfig:
    """Read watchlist from a YAML file. If the file is missing, return empty watchlist (warning)."""
    # No file → start with an empty watchlist — user is meant to populate it later.
    if not path.exists():
        return WatchlistConfig()

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    return WatchlistConfig(**data)


def load_config(
    env: str = "local",
    watchlist_path: Path | None = None,
) -> AnomalyConfig:
    """Load the AnomalyConfig appropriate for the environment.

    Args:
        env: "local" or "cloud_run". Local uses .env; Cloud Run uses OS env vars only.
        watchlist_path: watchlist YAML path. None → defaults to config/watchlist.yaml.

    Returns:
        AnomalyConfig: a validated, frozen-ish dataclass with every setting.

    Raises:
        ValidationError: on pydantic validation failure (bad env var, YAML schema error, etc.).
    """
    secrets = SecretsConfig()  # auto-load .env

    wl_path = watchlist_path or _DEFAULT_WATCHLIST_PATH
    watchlist = _load_watchlist_yaml(wl_path)

    return AnomalyConfig(
        env=env,
        secrets=secrets,
        watchlist=watchlist,
    )
