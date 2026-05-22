"""
cme/post_analysis.py — post-processing of Databento trades DataFrames + trigger filter.

────────────────────────────────────────────────────────────────────────
Responsibilities (P9.3.P1.B):

  When the primary detectors (vol_z_v1, price_jump_v1) emit RISK_OFF or
  EMERGENCY from a TradingView webhook, the Daemon fetches raw trade data for
  ±15 minutes around the event timestamp from Databento (databento_client.fetch_historical_range).

  This module takes that DataFrame and computes 4 additional metrics that
  quantify "why this is an anomaly", then filters to only those that exceed
  pre-defined thresholds (= triggered) and returns them in a form ready for
  the alert body.

  User decision (Q1=a): metrics that did not trigger are excluded from the
  alert (less noise).

────────────────────────────────────────────────────────────────────────
The 4 metrics computed:

  1. VPIN (Volume-Synchronized Probability of Informed Trading)
     · Split volume into N equal-size buckets, then average
       |buy_vol − sell_vol| / bucket_size across buckets. Range 0~1.
       Normal ~0.1-0.2, increases as informed trading rises.
     · trigger: VPIN ≥ 0.40
     · Reference: Easley, López de Prado, O'Hara (2012)

  2. side_imbalance_5m / side_imbalance_1m
     · buy_vol / (buy_vol + sell_vol) over the most recent 5m / 1m window.
     · 0.5 is balanced. 0.75+ is strong buy-side, 0.25- is strong sell-side.
     · trigger: 5m ≥ 0.75 or ≤ 0.25 / 1m ≥ 0.85 or ≤ 0.15

  3. block_trade_count_5m + block_trade_max_size
     · Count of single trades above a threshold ("block trade") and the max size.
     · Catches the pattern of informed traders pushing large single trades into the market.
     · trigger: count ≥ 5 (≥ 5 in 5 minutes) or max_size ≥ 500

  4. trade_count_5m / trade_count_1m
     · *Number of trades* in 5m/1m (count, not volume).
     · Helps distinguish whether a vol spike came from a flood of smaller-than-usual trades
       (≈ retail panic) or a few large trades (≈ informed).
     · trigger: 5m ≥ 1000 trades or 1m ≥ 300 trades

────────────────────────────────────────────────────────────────────────
Input DataFrame schema (Databento trades):

  index:    datetime64[ns, UTC]      ts_event (each trade's time)
  columns:
    symbol  str           e.g. "BZK6", "BZM6" (per-contract month)
    price   float
    size    int           number of contracts
    side    str ('A'/'B'/'N')
              'A' = Ask hit  → seller aggressor (down-tick)
              'B' = Bid hit  → buyer aggressor (up-tick)
              'N' = Unknown  → split half/half between sides (simple handling)

  Note: multiple contract months for one root (e.g. BZ) are mixed in. For analysis,
        select only the single front-month with the highest volume (consistency +
        better signal-to-noise).

────────────────────────────────────────────────────────────────────────
Output (PostAnalysisResult):

  metrics:    dict[str, float]   — all computed metrics (debug/audit)
  triggered:  dict[str, str]     — only metrics that crossed thresholds; value is
                                   a human-readable reason text (e.g. "VPIN=0.58 (>0.40)")
  warnings:   list[str]          — data-quality warnings (e.g. "side_unknown=42%")

  If `triggered` is empty there is no enrichment to add → caller marks it accordingly.

D7 (LOCKED): this module is a pure function — no external I/O. Databento fetch
              is the caller's (daemon's) responsibility, for easy testing /
              reproduction / backtesting.
"""

# ── stdlib ─────────────────────────────────────────────────────────────
from __future__ import annotations

import logging                                  # logs data-quality warnings
from dataclasses import dataclass, field        # result container
from datetime import datetime, timedelta        # window slicing
from typing import Optional                     # Python 3.10+ compatible union notation

# ── 3rd-party ──────────────────────────────────────────────────────────
import pandas as pd                             # core DataFrame tool

logger = logging.getLogger(__name__)


# =====================================================================
# Threshold settings — per-root calibration (P9.3.P1.B decision).
#
# All root-specific values are derived from event-based calibration:
#   - *minimum* of measured spikes for known events (3/23, 4/2, 4/9, 4/17) × 0.4
#   - 0.4 multiplier is intentional to catch "that, plus slightly smaller informed cases"
#     (user approved).
#   - Recalibrate each quarter (ops task) — absorbs market volume changes.
#
# Side imbalance / VPIN are ratios (% based) so root-independent — same for all roots.
# =====================================================================
@dataclass(frozen=True)
class RootThresholds:
    """Per-root absolute thresholds (unit = contracts).

    Meaning of each metric:
      trade_count_5m_min / 1m_min:
          5min/1min trade count ≥ this value → trigger.
          Catches the "many small trades" pattern of retail panic.

      block_trade_size:
          A single trade ≥ this value is classified as a "block trade" (definition).
          Around the P95~P99 of normal trade size distribution for the root.

      block_trade_count_5m_min:
          *Count* of block trades in 5min ≥ this → trigger.
          "Split buying" pattern (informed split a large order to reduce impact).

      block_trade_max_size_min:
          Max single trade size in 5min ≥ this → trigger.
          "One-shot buying" pattern (informed take it all in one execution).
    """
    trade_count_5m_min: int
    trade_count_1m_min: int
    block_trade_size: int
    block_trade_count_5m_min: int
    block_trade_max_size_min: int


# Per-root calibration — **pre-announcement informed peak × 0.5** (re-calibrated 2026-04-19)
#
# Measurements (all PDT, the informed minute -9~-23 min before the announcement):
#   CL  3/23 03:50 PDT (Trump Iran-pause -15min): 1m=1168, 5m=2265, max_trade=39
#   CL  4/17 05:25 PDT (Iran -21min):              1m=1072, 5m=2606, max_trade=62
#   BZ  3/23 03:50 PDT (-15min):                    1m=248,  5m=463,  max_trade=13
#   BZ  4/17 05:25 PDT (-22min):                    1m=268,  5m=626,  max_trade=29
#   ES  4/2  12:50 PDT (Liberation Day -9min):      1m=2772, 5m=5994, max_trade=88
#   ES  4/9  12:41 PDT (Truth Social -18min):       1m=1057, 5m=3719, max_trade=103
#   GC  4/17 05:25 PDT (-23min):                    1m=575,  5m=1222, max_trade=15
#
# Threshold = min(measurements) × 0.5 — catches the informed peak and somewhat
# smaller early-stage informed activity too. Post-announcement crowd spikes are
# already caught by the primary vol_z detector, so post-analysis is for
# *informed pattern enrichment*.
ROOT_THRESHOLDS: dict[str, RootThresholds] = {
    "CL": RootThresholds(
        trade_count_5m_min=1_132, trade_count_1m_min=536,
        block_trade_size=10, block_trade_count_5m_min=3, block_trade_max_size_min=19,
    ),
    "BZ": RootThresholds(
        trade_count_5m_min=231, trade_count_1m_min=124,
        block_trade_size=5, block_trade_count_5m_min=3, block_trade_max_size_min=6,
    ),
    "ES": RootThresholds(
        trade_count_5m_min=1_859, trade_count_1m_min=528,
        block_trade_size=20, block_trade_count_5m_min=3, block_trade_max_size_min=44,
    ),
    "GC": RootThresholds(
        trade_count_5m_min=611, trade_count_1m_min=287,
        block_trade_size=10, block_trade_count_5m_min=3, block_trade_max_size_min=7,
    ),
}

# Used when an unmapped root comes in — very conservative (never produces false positives)
_DEFAULT_ROOT_THRESHOLDS = RootThresholds(
    trade_count_5m_min=10_000, trade_count_1m_min=3_000,
    block_trade_size=100, block_trade_count_5m_min=5, block_trade_max_size_min=500,
)


@dataclass(frozen=True)
class PostAnalysisThresholds:
    """Root-agnostic (% based) thresholds + a per-root dict container.

    side_imbalance and VPIN are ratios so identical across roots.
    """

    # VPIN: 0~1 range. Normal 0.1~0.2, increases with informed trading.
    vpin_min: float = 0.40
    # Number of buckets for VPIN — 50 is appropriate for 30min ~ 1h of data.
    vpin_n_buckets: int = 50

    # side_imbalance: buy_vol / (buy_vol + sell_vol). 0.5 = balanced.
    side_imb_5m_high: float = 0.75
    side_imb_5m_low: float = 0.25                # 1 - high = sell-side dominated
    side_imb_1m_high: float = 0.85
    side_imb_1m_low: float = 0.15

    # Per-root absolute values — None uses the ROOT_THRESHOLDS dict.
    per_root: Optional[dict[str, RootThresholds]] = None

    def for_root(self, root: str) -> RootThresholds:
        """Return the RootThresholds for the root. Conservative default if unmapped."""
        table = self.per_root if self.per_root is not None else ROOT_THRESHOLDS
        return table.get(root.upper(), _DEFAULT_ROOT_THRESHOLDS)


# =====================================================================
# Result container
# =====================================================================
@dataclass
class PostAnalysisResult:
    """Post-analysis output. Dataclass instead of dict for type safety."""

    # All computed metrics (triggered or not — for debug/audit trail)
    metrics: dict[str, float] = field(default_factory=dict)
    # Only triggered metrics. key=metric_name, value=human-readable reason
    # (drops straight into the alert body — caller does not need further formatting)
    triggered: dict[str, str] = field(default_factory=dict)
    # Data-quality warnings (e.g. "side_unknown_pct=42% — VPIN reliability low")
    warnings: list[str] = field(default_factory=list)

    @property
    def has_trigger(self) -> bool:
        """Whether `triggered` has any entries. Caller decides alert inclusion."""
        return len(self.triggered) > 0


# =====================================================================
# Main entry point
# =====================================================================
def run_post_analysis(
    trades_df: pd.DataFrame,
    *,
    event_ts: datetime,
    root: str,
    thresholds: Optional[PostAnalysisThresholds] = None,
) -> PostAnalysisResult:
    """Compute 4 metrics from the trades DataFrame + apply trigger filter.

    Args:
        trades_df: Databento trades DataFrame.
                   index = datetime (UTC), columns = symbol/price/size/side.
        event_ts:  the time the detector emitted the anomaly (UTC).
                   Anchor for the "most recent 5m / 1m" windows.
        root:      CME root symbol ("CL"/"BZ"/"ES"/"GC"). For logging.
        thresholds: trigger thresholds. None → defaults.

    Returns:
        PostAnalysisResult — all metrics + only those that triggered.
    """
    th = thresholds or PostAnalysisThresholds()
    rt = th.for_root(root)                        # root-specific absolute values
    result = PostAnalysisResult()

    # 0) Pre-checks — return early if empty
    if trades_df is None or len(trades_df) == 0:
        result.warnings.append("trades_df_empty")
        logger.warning("post_analysis[%s]: empty trades_df, skipping", root)
        return result

    required_cols = {"size", "side"}              # minimum required columns
    missing = required_cols - set(trades_df.columns)
    if missing:
        result.warnings.append(f"missing_columns={sorted(missing)}")
        logger.warning("post_analysis[%s]: missing cols %s", root, missing)
        return result

    # 1) Extract front-month — pick the single most-traded contract among the root's months.
    #    Otherwise per-month noise mixes in and imbalance/VPIN signals weaken.
    df = _select_front_month(trades_df, root=root)
    if len(df) == 0:
        result.warnings.append("no_front_month_after_filter")
        return result

    # 2) Data quality check — warn if too many side='N' (degrades VPIN reliability)
    side_n_pct = (df["side"] == "N").mean() * 100
    if side_n_pct > 30:
        result.warnings.append(
            f"side_unknown_pct={side_n_pct:.0f}% (VPIN reliability degraded)"
        )

    # 3) Metric 1: VPIN — use the full data (narrow windows lack buckets)
    vpin = _calc_vpin(df, n_buckets=th.vpin_n_buckets)
    result.metrics["vpin"] = vpin
    if vpin >= th.vpin_min:
        result.triggered["vpin"] = (
            f"VPIN={vpin:.2f} (≥{th.vpin_min:.2f}) — "
            f"possible informed trading"
        )

    # 4) Metric 2: side_imbalance — computed separately for 5min / 1min windows
    df_5m = _slice_window(df, end_ts=event_ts, minutes=5)
    df_1m = _slice_window(df, end_ts=event_ts, minutes=1)

    imb_5m = _calc_side_imbalance(df_5m)
    result.metrics["side_imbalance_5m"] = imb_5m
    if imb_5m >= th.side_imb_5m_high:
        result.triggered["side_imbalance_5m"] = (
            f"Buy-side dominant: 5m buy {imb_5m*100:.0f}% (≥{th.side_imb_5m_high*100:.0f}%)"
        )
    elif imb_5m <= th.side_imb_5m_low:
        result.triggered["side_imbalance_5m"] = (
            f"Sell-side dominant: 5m sell {(1-imb_5m)*100:.0f}% (≥{(1-th.side_imb_5m_low)*100:.0f}%)"
        )

    imb_1m = _calc_side_imbalance(df_1m)
    result.metrics["side_imbalance_1m"] = imb_1m
    if imb_1m >= th.side_imb_1m_high:
        result.triggered["side_imbalance_1m"] = (
            f"Accelerating buy-side: 1m buy {imb_1m*100:.0f}% (≥{th.side_imb_1m_high*100:.0f}%)"
        )
    elif imb_1m <= th.side_imb_1m_low:
        result.triggered["side_imbalance_1m"] = (
            f"Accelerating sell-side: 1m sell {(1-imb_1m)*100:.0f}% (≥{(1-th.side_imb_1m_low)*100:.0f}%)"
        )

    # 5) Metric 3: block trade — inspect large single trades in the 5min window
    #    block_trade_size: a single trade ≥ N contracts is classified "block" (definition)
    #    count_5m: number of blocks in 5min → "split buying" pattern ("big ones keep coming")
    #    max_size: max single trade in 5min → "one-shot buying" pattern ("one big trade")
    if len(df_5m) > 0:
        block_mask = df_5m["size"] >= rt.block_trade_size
        block_count = int(block_mask.sum())
        block_max = int(df_5m["size"].max()) if len(df_5m) > 0 else 0
        result.metrics["block_trade_count_5m"] = float(block_count)
        result.metrics["block_trade_max_size"] = float(block_max)

        if block_count >= rt.block_trade_count_5m_min:
            result.triggered["block_trade_count_5m"] = (
                f"Block trades 5m: {block_count} "
                f"(≥{rt.block_trade_count_5m_min}, size≥{rt.block_trade_size} contracts) — "
                f"split trades (many large trades)"
            )
        if block_max >= rt.block_trade_max_size_min:
            result.triggered["block_trade_max_size"] = (
                f"Max single trade: {block_max:,} contracts "
                f"(≥{rt.block_trade_max_size_min}) — one-shot large trade"
            )

    # 6) Metric 4: trade count — auxiliary indicator for retail panic
    n_5m = len(df_5m)
    n_1m = len(df_1m)
    result.metrics["trade_count_5m"] = float(n_5m)
    result.metrics["trade_count_1m"] = float(n_1m)
    if n_5m >= rt.trade_count_5m_min:
        result.triggered["trade_count_5m"] = (
            f"Trade count 5m: {n_5m:,} (≥{rt.trade_count_5m_min}) — surge in trading activity"
        )
    if n_1m >= rt.trade_count_1m_min:
        result.triggered["trade_count_1m"] = (
            f"Trade count 1m: {n_1m:,} (≥{rt.trade_count_1m_min}) — suspected retail panic"
        )

    logger.info(
        "post_analysis[%s] event_ts=%s: %d/%d metrics triggered",
        root, event_ts.isoformat(),
        len(result.triggered), len(result.metrics),
    )
    return result


# =====================================================================
# Internal helpers
# =====================================================================
def _select_front_month(df: pd.DataFrame, *, root: str) -> pd.DataFrame:
    """Keep only the single highest-volume contract among the root's contract months.

    Front-month identification logic:
      - If a "symbol" column exists, pick the symbol starting with `root` that has the most trades.
      - If no "symbol" column (caller already fetched a single symbol), return as-is.
    """
    if "symbol" not in df.columns:
        return df.copy()

    mask = df["symbol"].str.startswith(root.upper())
    df_root = df[mask]
    if len(df_root) == 0:
        return df_root.copy()

    top_symbol = df_root["symbol"].value_counts().idxmax()
    return df_root[df_root["symbol"] == top_symbol].copy()


def _slice_window(df: pd.DataFrame, *, end_ts: datetime, minutes: int) -> pd.DataFrame:
    """Extract only the [end_ts - minutes, end_ts] window anchored at end_ts.

    Assumes df.index is datetime64[ns, UTC]. Treated as UTC if tz-naive.
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        return df.iloc[0:0]                       # empty

    # tz consistency — check whether the caller passed a tz-aware datetime
    end = pd.Timestamp(end_ts)
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    if df.index.tz is None:
        idx = df.index.tz_localize("UTC")
        df_aligned = df.copy()
        df_aligned.index = idx
    else:
        df_aligned = df

    start = end - pd.Timedelta(minutes=minutes)
    return df_aligned.loc[(df_aligned.index >= start) & (df_aligned.index <= end)]


def _calc_vpin(df: pd.DataFrame, *, n_buckets: int) -> float:
    """VPIN — Easley, López de Prado, O'Hara (2012).

    Formula:
      bucket_size = total_volume / n_buckets
      VPIN = mean over buckets of: |buy_vol_bucket − sell_vol_bucket| / bucket_size

    side='N' (unknown) is split half/half between sides (simple handling).
    """
    if len(df) == 0 or "size" not in df.columns or "side" not in df.columns:
        return 0.0

    total_vol = float(df["size"].sum())
    if total_vol <= 0:
        return 0.0

    bucket_size = total_vol / n_buckets
    if bucket_size <= 0:
        return 0.0

    # Databento MDP3 aggressor side convention (matches live_streamer.py L425
    # and cme_insider_scanner.py L590-593):
    #   side='A' = trade on Ask  → buyer was the aggressor   → buy_vol
    #   side='B' = trade on Bid  → seller was the aggressor  → sell_vol
    #   side='N' = unknown / non-trade                       → split 50/50 below
    buy_vol = df.loc[df["side"] == "A", "size"].astype(float)
    sell_vol = df.loc[df["side"] == "B", "size"].astype(float)
    unk_vol = df.loc[df["side"] == "N", "size"].astype(float)

    # 'N' is split half/half (simplification — a more rigorous approach is the Lee-Ready algorithm)
    half_unk = unk_vol * 0.5

    # Fill buckets in time order — new bucket whenever the cumulative volume crosses a bucket boundary
    df_sorted = df.sort_index()
    cum_vol = 0.0
    bucket_idx = 0
    bucket_b = 0.0                                # buy_vol per bucket
    bucket_s = 0.0                                # sell_vol per bucket
    abs_diffs = []                                # |B-S| per bucket

    for ts, row in df_sorted[["size", "side"]].iterrows():
        sz = float(row["size"])
        side = row["side"]

        # 'A' = buy-aggressor, 'B' = sell-aggressor (Databento MDP3 convention)
        if side == "A":
            bucket_b += sz                        # buy bucket
        elif side == "B":
            bucket_s += sz                        # sell bucket
        else:                                     # 'N' — half/half
            bucket_b += sz * 0.5
            bucket_s += sz * 0.5

        cum_vol += sz
        # bucket boundary reached → record + reset
        while cum_vol >= bucket_size * (bucket_idx + 1):
            abs_diffs.append(abs(bucket_b - bucket_s))
            bucket_b = 0.0
            bucket_s = 0.0
            bucket_idx += 1
            if bucket_idx >= n_buckets:
                break
        if bucket_idx >= n_buckets:
            break

    if not abs_diffs:
        return 0.0

    # VPIN = mean(|B-S|) / bucket_size — range 0~1
    return float(sum(abs_diffs) / len(abs_diffs) / bucket_size)


def _calc_side_imbalance(df: pd.DataFrame) -> float:
    """buy_vol / (buy_vol + sell_vol). 0.5 = balanced. 'N' split half/half.

    Defaults to 0.5 (balanced) when there is no data.
    """
    if len(df) == 0 or "size" not in df.columns or "side" not in df.columns:
        return 0.5

    # Databento MDP3 convention: 'A' = buy-aggressor, 'B' = sell-aggressor
    # (see live_streamer.py L425 and cme_insider_scanner.py L590-593).
    buy = float(df.loc[df["side"] == "A", "size"].sum())
    sell = float(df.loc[df["side"] == "B", "size"].sum())
    unk = float(df.loc[df["side"] == "N", "size"].sum())
    # 'N' half split
    buy += unk * 0.5
    sell += unk * 0.5
    total = buy + sell
    if total <= 0:
        return 0.5
    return buy / total


__all__ = [
    "PostAnalysisThresholds",
    "PostAnalysisResult",
    "RootThresholds",
    "ROOT_THRESHOLDS",
    "run_post_analysis",
]
