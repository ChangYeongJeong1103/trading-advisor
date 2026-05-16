"""
cme/tradingview_adapter.py — TradingView webhook → RawEvent + NormalizedEvent.

────────────────────────────────────────────────────────────────────────
Responsibilities (P9.3.P0.B):

  When TradingView fires an alert, it POSTs to our daemon's /webhook/tradingview.
  We convert that JSON into our schema (RawEvent + NormalizedEvent) and inject
  it into the CMEChannel buffer.

────────────────────────────────────────────────────────────────────────
TradingView alert message format (filled into the alert's message field by the user):

  {
    "secret":   "<TRADINGVIEW_WEBHOOK_SECRET>",   ← matches env var
    "ticker":   "{{ticker}}",                      ← e.g. "NYMEX:CL1!"
    "exchange": "{{exchange}}",                    ← e.g. "NYMEX"
    "interval": "{{interval}}",                    ← e.g. "1" (minutes)
    "close":    {{close}},                         ← e.g. 79.32
    "volume":   {{volume}},                        ← 1-min volume (contracts)
    "time":     "{{time}}",                        ← ISO8601 UTC
    "trigger":  "vol_spike_1m"                     ← alert name we defined
  }

  TradingView auto-replaces placeholders like {{ticker}} with the actual values.

────────────────────────────────────────────────────────────────────────
Security design:

  TradingView itself does not send an HMAC signature (unfortunate limitation).
  Instead, we use two layers:

    Layer 1 (required): constant-time compare of payload's "secret" field with
                        env var TRADINGVIEW_WEBHOOK_SECRET.
                        → Even if someone knows our endpoint URL, no pass without the secret.

    Layer 2 (recommended, the endpoint's responsibility in P0.C): TradingView's official IP allowlist.
            (52.89.214.238 / 34.212.75.30 / 54.218.53.128 / 52.32.178.7)

────────────────────────────────────────────────────────────────────────
USD notional computation:

  TradingView sends 1-min cumulative volume (contracts). Our schema needs
  size_usd, so we convert:

    size_usd = close × volume × CME_CONTRACT_MULT[root_symbol]

    CL, BZ: 1 contract = 1000 barrels      → size_usd = price × 1000 × volume
    ES:     1 contract = $50 × index       → size_usd = price × 50   × volume
    GC:     1 contract = 100 troy oz       → size_usd = price × 100  × volume

────────────────────────────────────────────────────────────────────────
Architecture: §4.1 NormalizedEvent, §6.5 alert layer
P9.3 design: hybrid TradingView (primary trigger) + Databento (precise verification)
"""

# ── stdlib ─────────────────────────────────────────────────────────────
from __future__ import annotations

import hmac                           # constant-time compare
import logging                        # structured logging
from datetime import datetime         # ts parsing
from typing import Any                # generic dict typing

# ── 3rd-party ──────────────────────────────────────────────────────────
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,                  # re-exported so callers can catch it
    field_validator,
)

# ── internal ──────────────────────────────────────────────────────────
from ...core.schemas import (
    CHANNEL_CME,
    NormalizedEvent,
    RawEvent,
    Source,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Constants — per-CME-contract multiplier (for USD notional)
# =====================================================================
# Only the 4 symbols in our watchlist are defined.
# When adding a new symbol, update here + watchlist.yaml + tests.
CME_CONTRACT_MULT: dict[str, float] = {
    "CL": 1_000.0,    # WTI 1 lot = 1000 barrels
    "BZ": 1_000.0,    # Brent 1 lot = 1000 barrels
    "ES": 50.0,       # E-mini S&P 1 lot = $50 × index
    "GC": 100.0,      # Gold 1 lot = 100 troy oz
    # P9.3 follow-ups: add NQ (20), ZB (1000), 6E (125000), etc. as needed
}

# Trigger names our daemon recognizes (1:1 match with TV alert names).
# When adding a new trigger, update here + golden_smoke scenario + detector mapping.
KNOWN_TRIGGERS: frozenset[str] = frozenset({
    "vol_spike_1m",      # 1-min volume > X (fastest primary trigger)
    "vol_spike_5m",      # 5-min cumulative volume > X (slower confirmation)
    "price_jump_1m",     # 1-min |close-open|/open > Y%
    "manual",            # for manual testing (user can trigger directly)
})


# =====================================================================
# Pydantic model — incoming webhook payload validation
# =====================================================================
class TradingViewWebhookPayload(BaseModel):
    """Our schema for the TradingView alert message JSON.

    extra="forbid" → unknown fields raise ValidationError → prevents schema drift.
    """

    # extra="forbid": reject unknown fields. The payload schema must remain under our control.
    model_config = ConfigDict(extra="forbid")

    # ── security ──
    # secret: constant-time compared with env var TRADINGVIEW_WEBHOOK_SECRET.
    secret: str = Field(min_length=8, description="shared secret")

    # ── identifiers ──
    # TV ticker formats: "NYMEX:CL1!", "CME:ES1!", "COMEX:GC1!", "NYMEX:BZ1!"
    # or just "CL1!" when exchange is omitted.
    ticker: str = Field(min_length=1, max_length=64)

    # exchange: TradingView's {{exchange}} placeholder. Usually "NYMEX", "CME", "COMEX".
    # Optional (alert created without the placeholder).
    exchange: str | None = Field(default=None, max_length=32)

    # ── time / price / volume ──
    # interval: minutes (TV {{interval}} arrives as a string like "1", "5", "60").
    interval: str = Field(default="1", max_length=8)

    # close: close price of the 1-minute (or interval-minute) bar.
    close: float = Field(gt=0.0, description="bar close price")

    # volume: volume of that bar (contracts). Reject if 0 (abnormal alert).
    volume: float = Field(gt=0.0, description="bar volume (contracts)")

    # time: ISO8601 timestamp TV sends ({{time}} placeholder).
    # e.g. "2026-04-17T12:47:00Z"
    time: datetime = Field(description="bar close time (UTC)")

    # ── trigger ──
    # trigger: must be one of our KNOWN_TRIGGERS. Unknown triggers are rejected.
    trigger: str = Field(description="trigger name we defined")

    @field_validator("trigger")
    @classmethod
    def _check_known_trigger(cls, v: str) -> str:
        """Validate that trigger is in our KNOWN_TRIGGERS."""
        if v not in KNOWN_TRIGGERS:
            raise ValueError(
                f"unknown trigger {v!r} (allowed: {sorted(KNOWN_TRIGGERS)})"
            )
        return v


# =====================================================================
# Helpers
# =====================================================================
def _parse_root_symbol(ticker: str) -> str:
    """TV ticker → our root symbol (e.g. "NYMEX:CL1!" → "CL").

    Examples:
        "NYMEX:CL1!"  → "CL"
        "CME:ES1!"    → "ES"
        "COMEX:GC1!"  → "GC"
        "NYMEX:BZ1!"  → "BZ"
        "CL1!"        → "CL"
        "CLM2026"     → "CL"
        "BZ"          → "BZ"

    Raises:
        ValueError: unknown format (failed to extract root symbol).
    """
    # Strip exchange prefix ("NYMEX:CL1!" → "CL1!")
    if ":" in ticker:
        ticker = ticker.split(":", 1)[1]

    # Match the longest prefix among possible roots first (so "BZ" wins over "B")
    # — sorted by length DESC so 2-char roots like "BZ" match first
    for root in sorted(CME_CONTRACT_MULT.keys(), key=len, reverse=True):
        if ticker.startswith(root):
            return root

    raise ValueError(
        f"unknown TV ticker format: {ticker!r} "
        f"(supported roots: {sorted(CME_CONTRACT_MULT.keys())})"
    )


# =====================================================================
# Main adapter
# =====================================================================
class TradingViewAdapter:
    """TradingView webhook payload → (RawEvent, NormalizedEvent) converter.

    Used directly by the daemon's /webhook/tradingview HTTP endpoint.
    """

    def __init__(self, *, webhook_secret: str) -> None:
        """
        Args:
            webhook_secret: env var TRADINGVIEW_WEBHOOK_SECRET. ValueError if empty.
                            (In production it is injected via GCP Secret Manager → env.)
        """
        if not webhook_secret or len(webhook_secret) < 8:
            raise ValueError(
                "TradingViewAdapter: webhook_secret must be ≥8 chars "
                "(set TRADINGVIEW_WEBHOOK_SECRET env var)"
            )
        # Encode secret to bytes for hmac.compare_digest
        self._secret_bytes: bytes = webhook_secret.encode("utf-8")

    # ─────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────
    def parse_webhook(
        self, payload: dict[str, Any]
    ) -> tuple[RawEvent, NormalizedEvent]:
        """webhook JSON → (RawEvent, NormalizedEvent).

        Args:
            payload: the HTTP POST body after json.loads().

        Returns:
            (RawEvent, NormalizedEvent): caller (daemon) pushes into the channel buffer.

        Raises:
            ValidationError: payload does not match our schema.
            PermissionError:  secret mismatch (timing-attack-safe compare).
            ValueError:       ticker has a root that is not in our watchlist.
        """
        # ──────────────────────────────────────────────────────────
        # Step 1: pydantic validation — prevent schema drift
        # ──────────────────────────────────────────────────────────
        # Raises ValidationError when payload is not a dict or required fields are missing.
        try:
            parsed = TradingViewWebhookPayload(**payload)
        except ValidationError as e:
            logger.warning(
                "TradingViewAdapter: payload validation failed: %s", e.errors()
            )
            raise

        # ──────────────────────────────────────────────────────────
        # Step 2: secret verification — constant-time compare (timing-attack safe)
        # ──────────────────────────────────────────────────────────
        # `==` leaks timing via string-length difference → use hmac.compare_digest.
        provided = parsed.secret.encode("utf-8")
        if not hmac.compare_digest(self._secret_bytes, provided):
            logger.warning(
                "TradingViewAdapter: secret mismatch (ticker=%s, ts=%s)",
                parsed.ticker, parsed.time.isoformat(),
            )
            # Never log the secret body itself.
            raise PermissionError("TradingView webhook secret mismatch")

        # ──────────────────────────────────────────────────────────
        # Step 3: ticker → root symbol mapping
        # ──────────────────────────────────────────────────────────
        # "NYMEX:CL1!" → "CL". ValueError on unknown root.
        try:
            root_symbol = _parse_root_symbol(parsed.ticker)
        except ValueError:
            logger.warning(
                "TradingViewAdapter: unknown ticker root, drop: %s", parsed.ticker
            )
            raise

        # ──────────────────────────────────────────────────────────
        # Step 4: USD notional computation
        # ──────────────────────────────────────────────────────────
        # contract multiplier × close × volume = USD notional, 1-min cumulative.
        contract_mult = CME_CONTRACT_MULT[root_symbol]
        size_usd = float(parsed.close) * float(parsed.volume) * contract_mult

        # ──────────────────────────────────────────────────────────
        # Step 5: build schema objects
        # ──────────────────────────────────────────────────────────
        # ts_source: bar close time TV sent (UTC).
        # ts_ingest: time we received it (filled by RawEvent default_factory).
        raw = RawEvent(
            channel=CHANNEL_CME,
            source=Source.WEBHOOK,         # WEBHOOK = inbound HTTP push
            symbol=root_symbol,
            ts_source=parsed.time,
            payload=payload,                # preserve the original — for audit / replay
        )

        # NormalizedEvent — TV alert is a bar-level signal, not a single trade.
        # → side is None (no sided trade info).
        # → meta carries extra context like trigger / interval / tv_ticker.
        normalized = NormalizedEvent(
            channel=CHANNEL_CME,
            symbol=root_symbol,
            ts_source=parsed.time,
            ts_ingest=raw.ts_ingest,
            side=None,                      # no side on a bar-level signal
            size_usd=size_usd,
            price=float(parsed.close),
            meta={
                # source identifier — features / detector distinguish mock vs TV
                "source_label": "tradingview_webhook_v1",
                "tv_ticker": parsed.ticker,
                "tv_exchange": parsed.exchange or "",
                "tv_interval_min": int(parsed.interval) if parsed.interval.isdigit() else 1,
                "tv_volume_contracts": float(parsed.volume),
                "trigger": parsed.trigger,
            },
            raw_ref=raw.id,
        )

        logger.info(
            "TradingViewAdapter: parsed ticker=%s root=%s trigger=%s "
            "close=%.4f vol=%.0f size_usd=%.0f ts=%s",
            parsed.ticker, root_symbol, parsed.trigger,
            parsed.close, parsed.volume, size_usd,
            parsed.time.isoformat(),
        )

        return raw, normalized
