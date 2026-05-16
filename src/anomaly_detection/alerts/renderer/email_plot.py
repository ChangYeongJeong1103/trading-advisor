"""
alerts/renderer/email_plot.py — Per-channel alert email/telegram/X inline PNG plot
(P11(a) → P12-D rewrite — 2026-05-15).

────────────────────────────────────────────────────────────────────────
Role:
  At the moment one ChannelSignal that passed cooldown fires (= "now"), build
  one timeline PNG that the user will see in inbox / Telegram / X. One email
  stacks two images vertically (1h zoom + 6h zoom), so this function is called
  twice.

────────────────────────────────────────────────────────────────────────
Layout (P12-D, user decision lock 2026-05-15):
  ┌───────────────────────────────────────────┐
  │ Panel 1 — Price vs Minutes from NOW       │  ← 1-min OHLC close
  ├───────────────────────────────────────────┤
  │ Panel 2 — Volume vs Minutes from NOW      │  ← buy ↑ / sell ↓ diverging
  ├───────────────────────────────────────────┤
  │ Panel 3 — Per-channel anomaly level       │  ← step plot (cme/pm/hl)
  ├───────────────────────────────────────────┤
  │ Panel 4 — Anomaly level (tier_floor)      │  ← red step line
  └───────────────────────────────────────────┘

  Panel 1/2 use the same style as the historical event plot
  (scripts/generate_historical_event_data.py::plot_event_symbol). The user
  decided that plot shows "insider trading suspicion" most clearly.
  Email/Telegram/X all use the same plot representation.

  Panel 3/4 match the existing P11(b).4 view: a timeline of how the alert
  escalated.

────────────────────────────────────────────────────────────────────────
Data sources:
  · result: ReplayResult — Panel 3/4. LiveTimelineBuffer compresses each
    fusion-cycle snapshot and synthesizes it.
  · bars: list[OhlcBar] — Panel 1/2. AlertOhlcBuffer accumulates each trade
    into 1-min buckets. If None or an empty list, show a "no data" placeholder.
  · first_bar_ts: timestamp of the oldest bar. If it starts later than the
    window, show a "data starts: HH:MM PT" label to indicate partial coverage.

X-axis (4 panels sharex):
  · "Minutes from NOW (T = 0)" — minute offset from alert_clock.
  · Right edge = NOW, data range is [-window, 0].

────────────────────────────────────────────────────────────────────────
Call path (called by the renderer):

    write_alert_window_plot(
        result=replay_result,
        plot_path=Path("/tmp/alert_1h.png"),
        alert_clock=signal.ts,
        window_minutes=60,
        alert_tier=signal.tier,
        alert_channel=signal.channel,
        alert_symbol=signal.symbol,
        bars=ohlc_buffer.bars(
            channel=signal.channel, symbol=signal.symbol,
            since=signal.ts - timedelta(minutes=60),
            until=signal.ts,
        ),
        first_bar_ts=ohlc_buffer.first_bar_ts(
            channel=signal.channel, symbol=signal.symbol,
        ),
        volume_unit_label="contracts",   # per-channel unit (CME=contracts, ...)
    )
"""

from __future__ import annotations

# ── Standard library ─────────────────────────────────────────────────
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ── third-party ──────────────────────────────────────────────────────
# Fix the matplotlib backend before import so headless environments are safe.
import matplotlib
matplotlib.use("Agg")  # noqa: E402

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import MultipleLocator  # noqa: E402

# ── Local ─────────────────────────────────────────────────────────────
from ..alert_ohlc_buffer import OhlcBar
from ...core.schemas import Tier
from ...replay.reporters.plot import (
    CHANNEL_COLORS,
    CHANNEL_ORDER,
    TIER_COLORS,
    TIER_LABELS,
    _channel_series,
    _tier_floor_series,
)
from ...replay.schemas import ReplayResult

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Display config — prefer Pacific Time (user lock 2026-05-14).
# ─────────────────────────────────────────────────────────────────────
_DISPLAY_TZ = ZoneInfo("America/Los_Angeles")


# ─────────────────────────────────────────────────────────────────────
# Tier → default window mapping (P11 legacy — fallback / preview only).
#   email/telegram always use two images: EMAIL_STACK_WINDOWS = (60, 360).
# ─────────────────────────────────────────────────────────────────────
DEFAULT_WINDOW_MINUTES_BY_TIER: dict[Tier, int] = {
    Tier.EMERGENCY: 60,
    Tier.RISK_OFF:  360,
    Tier.WATCH:     1440,
}

# Standard windows for the two plots embedded in the email body (user decision Q1 = c).
EMAIL_STACK_WINDOWS: tuple[int, int] = (60, 360)

# Per-channel size-unit labels, rendered directly on the volume axis.
# CME uses NormalizedEvent.size_usd (price × contracts × multiplier), so label as USD.
# Polymarket converts through normalize_trade to size_usd. Hyperliquid uses base coin sz.
DEFAULT_VOLUME_UNIT: dict[str, str] = {
    "cme": "USD",
    "polymarket": "USD",
    "hyperliquid": "coin",
    # X / truth_social have no trading volume, so use placeholder labels.
    # The alert plot volume panel becomes empty automatically when the OHLC buffer is empty.
    "x": "events",
    "truth_social": "posts",
}


# ─────────────────────────────────────────────────────────────────────
# Detector → plot bucket size mapping (user decision lock 2026-05-15).
#
# User request: "The fired detector's actual evaluation window and the plot bar
# size must match. Just as CME puts the 1/2/5min winner bucket into reason_codes,
# Polymarket / Hyperliquid also have detector-specific windows, so map them as-is."
#
# CME insider_v1 explicitly writes "INSIDER_V1_BUCKET=Xmin" to reason_codes, so
# no separate lookup is needed; infer_detector_bucket_minutes() parses it first.
# Only the remaining detectors live here.
# ─────────────────────────────────────────────────────────────────────
_DETECTOR_BUCKET_MIN: dict[str, int] = {
    # Polymarket — 1-min jump detectors
    "price_jump_v1": 1,
    "odds_gap_v2": 1,
    # Polymarket — 5-min aggregation detectors
    "vol_burst_v2": 5,
    "vol_burst_abs_v1": 5,
    "odds_cusum_v1": 5,
    "directional_v1": 5,
    "wallet_concentration_v1": 5,
    "single_wallet_burst_v1": 5,
    # Hyperliquid — 5-min detectors (vol_z_v1 baseline = 6 chunks × 5min).
    "vol_z_v1": 5,
    "wc_boost": 5,
}

DEFAULT_BUCKET_MINUTES: int = 1


def infer_detector_bucket_minutes(
    *,
    fired_detectors: list[str] | None,
    reason_codes: list[str] | None,
) -> int:
    """Infer the plot bucket size in minutes from the fired ChannelSignal.

    Rules, in priority order:
      1) CME insider_v1 writes ``INSIDER_V1_BUCKET=Xmin`` to reason_codes.
         Use the winner bucket label as-is (1/2/5).
      2) Other detectors are mapped through ``_DETECTOR_BUCKET_MIN``. If several
         detectors fire at once, **choose the longest window** because slower
         accumulation is most visually clear with 5-min bars.
      3) If nothing matches, use ``DEFAULT_BUCKET_MINUTES`` (1 minute).

    Args:
        fired_detectors: ``ChannelSignal.fired_detectors``. None is allowed.
        reason_codes: ``ChannelSignal.reason_codes``. None is allowed.

    Returns:
        Bucket size in minutes for grouping the plot's 1-min bars. If 1,
        resampling is not needed.
    """
    # 1) CME insider explicit bucket label
    for code in (reason_codes or []):
        upper = code.upper()
        if "INSIDER_V1_BUCKET=" not in upper:
            continue
        if "1MIN" in upper:
            return 1
        if "2MIN" in upper:
            return 2
        if "5MIN" in upper:
            return 5

    # 2) Other detectors: choose the longest window.
    longest = 0
    for det in (fired_detectors or []):
        win = _DETECTOR_BUCKET_MIN.get(det.strip().lower())
        if win and win > longest:
            longest = win
    if longest > 0:
        return longest

    # 3) Fallback
    return DEFAULT_BUCKET_MINUTES


def _resample_bars(
    bars: list[OhlcBar],
    *,
    alert_clock: datetime,
    bucket_minutes: int,
) -> list[OhlcBar]:
    """Group a list of 1-min OhlcBar values into N-min buckets.

    Bucket k (0, 1, 2, ...) is the interval
    ``[alert_clock - (k+1)*B, alert_clock - k*B)``. B = bucket_minutes * 60s.
    The right edge of bucket 0 is exactly NOW.

    Args:
        bars: 1-min bar list returned by AlertOhlcBuffer.
        alert_clock: NOW (= alert firing timestamp), the right edge for bucket alignment.
        bucket_minutes: Bucket size in minutes. If <= 1 or the list is empty,
            return the bars unchanged.

    Returns:
        New OhlcBar list in ascending time order, with ts=bucket_start, merged
        OHLC values, and summed buy/sell/neutral/count fields.
    """
    if bucket_minutes <= 1 or not bars:
        return list(bars)
    bucket_sec = float(bucket_minutes * 60)
    groups: dict[int, list[OhlcBar]] = {}
    for b in bars:
        off_sec = (alert_clock - b.ts).total_seconds()
        if off_sec < 0:
            # Future bar: the buffer push step should block this, but ignore defensively.
            continue
        k = int(off_sec // bucket_sec)
        groups.setdefault(k, []).append(b)

    out: list[OhlcBar] = []
    for k in sorted(groups.keys(), reverse=True):
        bars_in = sorted(groups[k], key=lambda b: b.ts)
        first_bar = bars_in[0]
        last_bar = bars_in[-1]
        bucket_start = alert_clock - timedelta(seconds=(k + 1) * bucket_sec)
        out.append(OhlcBar(
            ts=bucket_start,
            open=first_bar.open,
            high=max(b.high for b in bars_in),
            low=min(b.low for b in bars_in),
            close=last_bar.close,
            buy_vol=sum(b.buy_vol for b in bars_in),
            sell_vol=sum(b.sell_vol for b in bars_in),
            neutral_vol=sum(b.neutral_vol for b in bars_in),
            trade_count=sum(b.trade_count for b in bars_in),
        ))
    out.sort(key=lambda b: b.ts)
    return out


# ─────────────────────────────────────────────────────────────────────
# Core function: one plot unit (4-panel stacked PNG).
# ─────────────────────────────────────────────────────────────────────
def write_alert_window_plot(
    result: ReplayResult,
    plot_path: Path,
    *,
    alert_clock: datetime,
    window_minutes: int,
    alert_tier: Tier,
    alert_channel: str,
    alert_symbol: str,
    bars: list[OhlcBar] | None = None,
    first_bar_ts: datetime | None = None,
    volume_unit_label: str | None = None,
    bucket_minutes: int = DEFAULT_BUCKET_MINUTES,
    figsize: tuple[float, float] = (12.0, 11.0),
    dpi: int = 110,
) -> Path:
    """One 4-panel alert PNG: price / volume / per-channel anomaly / anomaly level.

    Args:
        result: ReplayResult data for Panel 3/4 (per-channel anomaly + tier_floor).
            In production, this comes from
            `LiveTimelineBuffer.to_replay_result_at(alert_clock)`.
        plot_path: Absolute output PNG path; the parent directory is created automatically.
        alert_clock: Timestamp treated as "now" (= the moment the alert fired).
            This is the right edge of the X-axis (T = 0). Data after this timestamp
            is clipped from the figure.
        window_minutes: Number of minutes before alert_clock to show (= absolute value
            of the X-axis left edge). Example: 60 → last 1h, 360 → last 6h.
        alert_tier: Tier shown in the title/footer.
        alert_channel: Channel name shown in the title/footer.
        alert_symbol: Symbol shown in the title/footer.
        bars: Data for Panel 1/2, from AlertOhlcBuffer.bars(...). If None or empty,
            Panel 1/2 show "Price/Volume data not available" placeholders.
        first_bar_ts: Oldest bar timestamp in the buffer. If it starts later than
            the window, show a "data starts: HH:MM PT" label. If None, omit the label.
        volume_unit_label: Size-unit label rendered in the Panel 2 ylabel
            ("contracts" / "USD" / "coin"). If None, default-map from the channel
            name using DEFAULT_VOLUME_UNIT.
        bucket_minutes: Number of minutes grouped into each Panel 1/2 bar. This
            matches the fired detector's evaluation window
            (``infer_detector_bucket_minutes``). If 1, use the buffer's original
            1-min bars. If 2 or 5, resample 1-min bars leftward from NOW
            (``_resample_bars``).
        figsize: matplotlib figsize in inches. Default 12 × 11 fits the Gmail HTML
            body width while keeping the 4-panel vertical stack readable.
        dpi: Output resolution. Default 110 stays crisp in Retina inboxes.

    Returns:
        plot_path for caller convenience.

    Note:
        Pure side effect: writes one PNG to plot_path. On failure, do not create an
        empty PNG; only emit a WARNING log.
    """
    plot_path.parent.mkdir(parents=True, exist_ok=True)

    # ── X-axis range shared by all panels ────────────────────────────
    # data_xlim_max = data truncation cutoff (= NOW), so future data is not drawn.
    # view_xlim_max = visual right edge of the axes. Add a small padding so the
    # final step segment is not clipped by bbox_inches="tight".
    xlim_min: float = -float(window_minutes)
    data_xlim_max: float = 0.0
    view_xlim_max: float = max(window_minutes * 0.03, 1.5)

    # ── Per-channel panel layout branch ──────────────────────────────
    # User decision 2026-05-16: X and truth_social have no price/volume concept,
    # so omit Panel 1/2 (price/volume) entirely and show only per-channel anomaly
    # + system anomaly. CME/Polymarket/Hyperliquid keep the existing 4 panels.
    no_ohlc_channels: frozenset[str] = frozenset({"x", "truth_social"})
    use_ohlc = alert_channel.lower() not in no_ohlc_channels

    if use_ohlc:
        # 4 panel: price(3.0) + volume(2.0) + per-channel(2.0) + floor(2.0).
        fig, axes = plt.subplots(
            nrows=4,
            ncols=1,
            figsize=figsize,
            dpi=dpi,
            sharex=True,
            gridspec_kw={
                "height_ratios": [3.0, 2.0, 2.0, 2.0],
                "hspace": 0.18,
            },
        )
        ax_price, ax_vol, ax_tier, ax_state = axes
    else:
        # 2 panels: per-channel(1.0) + floor(1.0). Reduce figure height while
        # keeping 6.5 inches so the footer / x-axis label still have absolute space
        # (about 60% of the 4-panel 11-inch height).
        fig, axes = plt.subplots(
            nrows=2,
            ncols=1,
            figsize=(figsize[0], 6.5),
            dpi=dpi,
            sharex=True,
            gridspec_kw={
                "height_ratios": [1.0, 1.0],
                "hspace": 0.18,
            },
        )
        ax_tier, ax_state = axes
        ax_price = None
        ax_vol = None

    # ── Panel 1 + 2: Price + Volume (from OhlcBar) ─────────────────
    # The buffer always arrives as 1-min bars. If the detector window is 5-min,
    # group it here and plot as 5-min bars for visual alignment.
    # When use_ohlc=False (X / truth_social), those two panels do not exist, so skip.
    if ax_price is not None and ax_vol is not None:
        plot_bars: list[OhlcBar] = (
            _resample_bars(
                bars,
                alert_clock=alert_clock,
                bucket_minutes=bucket_minutes,
            )
            if bars
            else []
        )
        has_ohlc = bool(plot_bars)
        if has_ohlc:
            _plot_price_panel(
                ax_price,
                bars=plot_bars,
                alert_clock=alert_clock,
                alert_channel=alert_channel,
                xlim_min=xlim_min,
                data_xlim_max=data_xlim_max,
                bucket_minutes=bucket_minutes,
            )
            _plot_volume_panel(
                ax_vol,
                bars=plot_bars,
                alert_clock=alert_clock,
                alert_channel=alert_channel,
                xlim_min=xlim_min,
                data_xlim_max=data_xlim_max,
                volume_unit_label=(
                    volume_unit_label
                    or DEFAULT_VOLUME_UNIT.get(alert_channel.lower(), "size")
                ),
                bucket_minutes=bucket_minutes,
            )
        else:
            _placeholder_panel(
                ax_price,
                "Price data not available yet",
                ylabel="Price",
            )
            _placeholder_panel(
                ax_vol,
                "Volume data not available yet",
                ylabel=(
                    f"{bucket_minutes}-min volume "
                    f"({volume_unit_label or 'size'})"
                ),
            )

        # Partial-data notice when the series starts later than the window.
        _maybe_annotate_data_start(
            ax_price,
            first_bar_ts=first_bar_ts,
            alert_clock=alert_clock,
            window_minutes=window_minutes,
        )

    # ── Panel 3 + 4: Per-channel anomaly + tier_floor (ReplayResult) ──
    # Pass alert_channel / alert_tier so a star marker at T=0 makes it visually
    # explicit that the alert fired now (tier=...). User decision 2026-05-16.
    if result.minutes:
        _plot_per_channel_anomaly_panel(
            ax_tier,
            result=result,
            alert_clock=alert_clock,
            xlim_min=xlim_min,
            data_xlim_max=data_xlim_max,
            alert_channel=alert_channel,
            alert_tier=alert_tier,
        )
        _plot_anomaly_floor_panel(
            ax_state,
            result=result,
            alert_clock=alert_clock,
            xlim_min=xlim_min,
            data_xlim_max=data_xlim_max,
            alert_tier=alert_tier,
        )
    else:
        _placeholder_panel(
            ax_tier,
            "Per-channel anomaly timeline not available",
            ylabel="Per-channel anomaly level",
        )
        _placeholder_panel(
            ax_state,
            "System anomaly timeline not available",
            ylabel="Anomaly level",
        )

    # ── Right-edge NOW vline + label, shared by all panels ───────────
    for ax in axes:
        ax.axvline(
            data_xlim_max, color="black", linewidth=2.0, alpha=0.65, zorder=1,
        )
    # Large NOW label at the top of the first panel to emphasize the alert moment.
    # In 4-panel mode this is the price panel; in 2-panel mode it is the
    # per-channel panel.
    ax_top = axes[0]
    ymax_top = ax_top.get_ylim()[1]
    ax_top.text(
        data_xlim_max, ymax_top, " NOW ",
        color="white", fontsize=10, fontweight="bold",
        ha="right", va="top",
        bbox=dict(facecolor="black", alpha=0.80, edgecolor="none", pad=3.0),
    )

    # ── Top title above the first panel ──────────────────────────────
    alert_ts_pt = alert_clock.astimezone(_DISPLAY_TZ).strftime(
        "%Y-%m-%d %H:%M %Z",
    )
    ax_top.set_title(
        f"[{alert_tier.value}] {alert_channel} · {alert_symbol}  ·  "
        f"NOW = {alert_ts_pt}  ·  window = last {window_minutes} min",
        fontsize=12, loc="left", pad=12,
    )

    # ── X-axis shared by all panels; label/ticks only on the bottom panel ──
    ax_state.set_xlim(xlim_min, view_xlim_max)
    ax_state.set_xlabel("Minutes from NOW (T = 0)", fontsize=11)

    # Choose tick spacing by window length for readability.
    span_min = max(int(round(data_xlim_max - xlim_min)), 1)
    if span_min <= 60:
        major_step, minor_step = 5, 1
    elif span_min <= 180:
        major_step, minor_step = 15, 5
    elif span_min <= 360:
        major_step, minor_step = 30, 10
    elif span_min <= 720:
        major_step, minor_step = 60, 15
    elif span_min <= 1440:
        major_step, minor_step = 120, 30
    else:
        major_step, minor_step = 240, 60
    ax_state.xaxis.set_major_locator(MultipleLocator(major_step))
    ax_state.xaxis.set_minor_locator(MultipleLocator(minor_step))
    ax_state.tick_params(axis="x", labelsize=8)
    for ax in axes:
        ax.grid(True, which="minor", axis="x", alpha=0.10)
        ax.grid(True, which="major", axis="x", alpha=0.30)

    # Use explicit subplots_adjust instead of constrained layout: placeholder axes
    # differ from normal axes and can make tight_layout warn. Set margins directly.
    # The bottom margin must fit both footer text and the x-axis label, so branch by
    # panel count; 2-panel figures are shorter and need more absolute space.
    fig.subplots_adjust(
        left=0.08, right=0.97, top=0.96,
        bottom=0.12 if not use_ohlc else 0.08,
        hspace=0.22,
    )

    # ── Footer, one line of alert metadata, placed after subplots_adjust.
    # User decision 2026-05-15: all timestamps inside the plot use PT.
    # Use PT strftime for "now" instead of isoformat (+00:00), so the inbox time is
    # always in the user's timezone and requires no mental conversion.
    # Footer y-position also branches by panel count; the 2-panel figure is shorter
    # and needs more distance from the x-label.
    footer_now_pt = alert_clock.astimezone(_DISPLAY_TZ).strftime(
        "%Y-%m-%d %H:%M:%S %Z",
    )
    fig.text(
        0.01, 0.010 if use_ohlc else 0.018,
        f"alert_tier={alert_tier.value}  ·  channel={alert_channel}  ·  "
        f"symbol={alert_symbol}  ·  now={footer_now_pt}  ·  "
        f"window={window_minutes}min  ·  bucket={bucket_minutes}min  ·  "
        f"event_id={result.event.event_id}",
        fontsize=8, color="#555555",
    )

    # Atomic write: tmp → rename.
    tmp_path = plot_path.with_suffix(plot_path.suffix + ".tmp")
    fig.savefig(tmp_path, dpi=dpi, bbox_inches="tight", format="png")
    plt.close(fig)
    tmp_path.replace(plot_path)

    if use_ohlc:
        logger.info(
            "alert window plot → %s (window=%dm bucket=%dm bars=%d→%d)",
            plot_path, window_minutes, bucket_minutes,
            len(bars) if bars else 0,
            len(plot_bars),  # Created inside the if block above; use_ohlc=True is guaranteed.
        )
    else:
        # X / truth_social: 2-panel mode with no price/volume.
        logger.info(
            "alert window plot → %s (window=%dm 2-panel no-ohlc channel=%s)",
            plot_path, window_minutes, alert_channel,
        )
    return plot_path


# ─────────────────────────────────────────────────────────────────────
# Panel 1 — Price line.
# ─────────────────────────────────────────────────────────────────────
def _plot_price_panel(
    ax,
    *,
    bars: list[OhlcBar],
    alert_clock: datetime,
    alert_channel: str,
    xlim_min: float,
    data_xlim_max: float,
    bucket_minutes: int = 1,
) -> None:
    """N-min close price line plus the final price label.

    Same as the price panel in the historical event plot: line + dots (small
    markers). If the price range is tiny, add a little ylim padding automatically.

    Because each bar's ts is the bucket start time, adjust the X position to the
    bucket center (= ts + bucket_minutes/2). With bucket_minutes=1 this differs
    from the old behavior by only 0.5 minutes, so it is effectively compatible.
    """
    is_polymarket = _is_polymarket_channel(alert_channel)
    half_bucket = bucket_minutes / 2.0
    offs: list[float] = []
    closes: list[float] = []
    for b in bars:
        off_min = (b.ts - alert_clock).total_seconds() / 60.0 + half_bucket
        if off_min < xlim_min or off_min > data_xlim_max:
            continue
        offs.append(off_min)
        # Polymarket price is Yes probability in [0, 1]. Display it as percent.
        closes.append(b.close * 100.0 if is_polymarket else b.close)

    if offs:
        ax.plot(
            offs, closes,
            color="#1f77b4", linewidth=1.6, alpha=0.95,
            marker="o", markersize=2.5, markevery=max(1, len(offs) // 60),
        )
        # Small label for the final price (oxy).
        last_label = f"{closes[-1]:.1f}%" if is_polymarket else f"{closes[-1]:.4g}"
        ax.annotate(
            last_label,
            xy=(offs[-1], closes[-1]),
            xytext=(6, 0), textcoords="offset points",
            fontsize=9, color="#1f77b4",
            va="center", ha="left",
        )

        if is_polymarket:
            ax.set_ylim(-2.0, 102.0)
        else:
            # ylim padding so a flat line does not collapse into a single axis line.
            ymin = min(closes)
            ymax = max(closes)
            if ymax - ymin < 1e-9:
                pad = max(abs(ymax) * 0.001, 1e-6)
            else:
                pad = (ymax - ymin) * 0.10
            ax.set_ylim(ymin - pad, ymax + pad)

    if is_polymarket:
        ylabel = f"Yes probability ({bucket_minutes}-min close, %)"
    else:
        ylabel = f"Price ({bucket_minutes}-min close)"
    ax.set_ylabel(ylabel, fontsize=10)
    ax.grid(True, alpha=0.25)


# ─────────────────────────────────────────────────────────────────────
# Panel 2 — Volume diverging bars (buy ↑ / sell ↓).
# ─────────────────────────────────────────────────────────────────────
def _plot_volume_panel(
    ax,
    *,
    bars: list[OhlcBar],
    alert_clock: datetime,
    alert_channel: str,
    xlim_min: float,
    data_xlim_max: float,
    volume_unit_label: str,
    bucket_minutes: int = 1,
) -> None:
    """Diverging buy/sell volume bars (top=buy green, bottom=sell red).

    Uses the same visual language as the historical event plot
    (scripts/generate_historical_event_data.py), so it is easy to see whether the
    alert points long (buy pressure) or short (sell pressure).

    If bucket_minutes is N (>1), the bar width expands to N minutes. Center bars
    on the bucket center so the right edge aligns with NOW.
    """
    is_polymarket = _is_polymarket_channel(alert_channel)
    half_bucket = bucket_minutes / 2.0
    offs: list[float] = []
    buys: list[float] = []
    sells: list[float] = []
    neutrals: list[float] = []
    for b in bars:
        off_min = (b.ts - alert_clock).total_seconds() / 60.0 + half_bucket
        if off_min < xlim_min or off_min > data_xlim_max:
            continue
        offs.append(off_min)
        buys.append(b.buy_vol)
        sells.append(b.sell_vol)
        neutrals.append(b.neutral_vol)

    if offs:
        # Bar width = N minutes (= bucket). Multiply by 0.9 to leave a small gap.
        bar_width = max(bucket_minutes * 0.9, 0.5)
        if is_polymarket:
            # Polymarket BUY/SELL is not enough to infer market direction unless
            # we also know whether the traded token was YES or NO. For alerts,
            # probability movement carries direction; volume should be total USD.
            totals = [
                b + s + n
                for b, s, n in zip(buys, sells, neutrals, strict=False)
            ]
            ax.bar(
                offs, totals,
                width=bar_width, color="#777777", alpha=0.75,
                edgecolor="none", label="traded volume",
            )
            ymax = max(max(totals), 1.0)
            ax.set_ylim(0.0, ymax * 1.15)
            ax.legend(
                loc="upper left", framealpha=0.92, fontsize=9,
            )
            ax.set_ylabel(
                f"{bucket_minutes}-min traded volume ({volume_unit_label})",
                fontsize=10,
            )
            ax.grid(True, axis="y", alpha=0.25)
            return

        # Buy (top, green).
        ax.bar(
            offs, buys,
            width=bar_width, color="#2ca02c", alpha=0.85,
            edgecolor="none", label="buy",
        )
        # Neutral (top, stacked above buy; gray).
        ax.bar(
            offs, neutrals,
            width=bar_width, bottom=buys,
            color="#bbbbbb", alpha=0.55,
            edgecolor="none", label="neutral",
        )
        # Sell (bottom, red; plotted as negative values).
        ax.bar(
            offs, [-v for v in sells],
            width=bar_width, color="#d62728", alpha=0.85,
            edgecolor="none", label="sell",
        )

        # ylim based on the larger of the buy/neutral stack max and the sell max.
        top = max((b + n) for b, n in zip(buys, neutrals, strict=False))
        bot = max(sells) if sells else 0.0
        ymax = max(top, bot, 1.0)
        ax.set_ylim(-ymax * 1.10, ymax * 1.10)

        # Legend in the upper left avoids overlap with volume spikes near NOW.
        ax.legend(
            loc="upper left", ncols=3, framealpha=0.92, fontsize=9,
        )

    # Zero line: buy/sell boundary.
    ax.axhline(0.0, color="black", linewidth=0.8, alpha=0.5)
    ax.set_ylabel(
        f"{bucket_minutes}-min volume ({volume_unit_label})\n"
        "(buy ↑ / sell ↓)",
        fontsize=10,
    )
    ax.grid(True, axis="y", alpha=0.25)


def _is_polymarket_channel(channel: str) -> bool:
    """Return True for Polymarket-specific plot formatting."""
    return channel.strip().lower() == "polymarket"


# ─────────────────────────────────────────────────────────────────────
# Panel 3 — Per-channel anomaly level step plot (same as the existing Panel 2).
# ─────────────────────────────────────────────────────────────────────
def _plot_per_channel_anomaly_panel(
    ax,
    *,
    result: ReplayResult,
    alert_clock: datetime,
    xlim_min: float,
    data_xlim_max: float,
    alert_channel: str | None = None,
    alert_tier: Tier | None = None,
) -> None:
    """Per-channel tier-rank step plot (cme/polymarket/hyperliquid/...).

    Args:
        alert_channel/alert_tier: Channel/tier that fired the alert. Highlight it
            with a large star marker at T=0 (user request 2026-05-16: "When I get
            an Emergency email, EMERGENCY should be visually marked at NOW"). The
            last tier in minutes data may differ from the alert tier because of
            sticky windows or a short final step segment, so the marker makes it
            explicit. If None, skip the marker.
    """
    announce: datetime = result.event.announcement_ts
    shift_min: float = (announce - alert_clock).total_seconds() / 60.0

    def _to_now_offset(off_announce: float) -> float:
        return off_announce + shift_min

    def _truncate(offs_a, values):
        out_off, out_val = [], []
        for off_a, v in zip(offs_a, values, strict=False):
            off_now = _to_now_offset(off_a)
            if xlim_min <= off_now <= data_xlim_max:
                out_off.append(off_now)
                out_val.append(v)
        return out_off, out_val

    def _extend_step_to_now(offsets, values):
        if not offsets:
            return offsets, values
        if offsets[-1] < data_xlim_max - 1e-9:
            return (
                list(offsets) + [data_xlim_max],
                list(values) + [values[-1]],
            )
        return offsets, values

    # Track the last kept tier for alert_channel; used for the vertical connector
    # between the marker and the line.
    alert_ch_last_rank: int | None = None

    for ch in CHANNEL_ORDER:
        offs_a, _, ranks = _channel_series(result, ch)
        offs, ranks_t = _truncate(offs_a, ranks)
        offs, ranks_t = _extend_step_to_now(offs, ranks_t)
        if not offs:
            continue
        color = CHANNEL_COLORS[ch]
        ax.step(
            offs, ranks_t,
            where="post", color=color, linewidth=1.6, alpha=0.85, label=ch,
        )
        if ch == alert_channel:
            alert_ch_last_rank = ranks_t[-1]

    # Alert tier marker at T=0 so the user immediately sees the exact point where
    # the EMERGENCY alert fired now. Use the alert_channel color.
    if (
        alert_channel
        and alert_tier is not None
        and alert_channel in CHANNEL_COLORS
    ):
        marker_color = CHANNEL_COLORS[alert_channel]
        # If the marker and the step-line endpoint are different tiers, connect
        # them with a vertical line. Draw the same-color line from the data step's
        # final tier (= minute just before alert_clock) to the marker (=alert_tier).
        # User decision 2026-05-16: incorporates feedback that "the line and star
        # look disconnected."
        if (
            alert_ch_last_rank is not None
            and alert_ch_last_rank != alert_tier.rank()
        ):
            ax.plot(
                [data_xlim_max, data_xlim_max],
                [alert_ch_last_rank, alert_tier.rank()],
                color=marker_color, linewidth=1.6, alpha=0.85, zorder=5,
            )
        ax.scatter(
            [data_xlim_max], [alert_tier.rank()],
            marker="*", s=240, color=marker_color,
            edgecolors="black", linewidths=1.0,
            zorder=10, clip_on=False,
        )

    ax.set_ylabel("Per-channel anomaly level", fontsize=10)
    ax.set_yticks([0, 1, 2, 3])
    ax.set_yticklabels(TIER_LABELS, fontsize=9)
    # Top ylim padding leaves room for the legend box. This used to be 4.4, but in
    # larger 2-panel mode the gap between EMERGENCY and the legend looked too big,
    # so reduce it to 3.7.
    ax.set_ylim(-0.3, 3.7)
    ax.grid(True, axis="y", alpha=0.25)
    ax.legend(
        loc="upper left", ncols=5, framealpha=0.92,
        bbox_to_anchor=(0.005, 1.0),
        prop={"weight": "bold", "size": 9},
    )


# ─────────────────────────────────────────────────────────────────────
# Panel 4 — System anomaly level (tier_floor, same as the existing Panel 3).
# ─────────────────────────────────────────────────────────────────────
def _plot_anomaly_floor_panel(
    ax,
    *,
    result: ReplayResult,
    alert_clock: datetime,
    xlim_min: float,
    data_xlim_max: float,
    alert_tier: Tier | None = None,
) -> None:
    """tier_floor red step line plus shaded bands for each tier.

    Args:
        alert_tier: Tier that fired the alert. Mark it with a large red star at
            T=0. If None, skip it.
    """
    announce: datetime = result.event.announcement_ts
    shift_min: float = (announce - alert_clock).total_seconds() / 60.0

    def _to_now(off_a: float) -> float:
        return off_a + shift_min

    floor_off_a, floor_ranks = _tier_floor_series(result)
    floor_off = [_to_now(o) for o in floor_off_a]

    # truncate.
    keep_off, keep_rk = [], []
    for o, r in zip(floor_off, floor_ranks, strict=False):
        if xlim_min <= o <= data_xlim_max:
            keep_off.append(o)
            keep_rk.append(r)
    floor_off, floor_ranks_t = keep_off, keep_rk

    # extend last segment to NOW.
    if floor_off and floor_off[-1] < data_xlim_max - 1e-9:
        floor_off.append(data_xlim_max)
        floor_ranks_t.append(floor_ranks_t[-1])

    if floor_off:
        ax.step(
            floor_off, floor_ranks_t,
            where="post", color="#d62728", linewidth=2.4,
            linestyle="-", alpha=0.95,
            label="tier_floor (system reach)",
        )

    for tier in (Tier.WATCH, Tier.RISK_OFF, Tier.EMERGENCY):
        ax.axhspan(
            tier.rank() - 0.5, tier.rank() + 0.5,
            color=TIER_COLORS[tier], alpha=0.06,
        )

    # Alert tier marker at T=0 explicitly highlights the fired tier.
    # Match the tier_floor red step-line color for visual consistency.
    if alert_tier is not None:
        # If the tier_floor step-line endpoint and alert tier differ, connect them
        # with a vertical connector. User decision 2026-05-16: avoids an awkward
        # visual gap between the line and marker.
        if floor_ranks_t and floor_ranks_t[-1] != alert_tier.rank():
            ax.plot(
                [data_xlim_max, data_xlim_max],
                [floor_ranks_t[-1], alert_tier.rank()],
                color="#d62728", linewidth=2.4, alpha=0.95, zorder=5,
            )
        ax.scatter(
            [data_xlim_max], [alert_tier.rank()],
            marker="*", s=280, color="#d32f2f",
            edgecolors="black", linewidths=1.0,
            zorder=10, clip_on=False,
            label="ALERT (NOW)",
        )
        ax.legend(
            loc="upper left", fontsize=9, framealpha=0.92,
            bbox_to_anchor=(0.005, 1.0),
        )

    ax.set_ylabel("Anomaly level", fontsize=10)
    ax.set_yticks([0, 1, 2, 3])
    ax.set_yticklabels(TIER_LABELS, fontsize=9)
    ax.set_ylim(-0.3, 3.3)
    ax.grid(True, axis="y", alpha=0.25)


# ─────────────────────────────────────────────────────────────────────
# Empty-panel placeholder: show guidance instead of an ugly empty axes when data is missing.
# ─────────────────────────────────────────────────────────────────────
def _placeholder_panel(ax, message: str, *, ylabel: str) -> None:
    """Show only centered gray text, ylabel, and grid."""
    ax.text(
        0.5, 0.5, message,
        transform=ax.transAxes,
        fontsize=11, color="#888888", style="italic",
        ha="center", va="center",
    )
    ax.set_ylabel(ylabel, fontsize=10)
    ax.set_yticks([])
    ax.grid(True, alpha=0.15)


# ─────────────────────────────────────────────────────────────────────
# Partial-data anomaly annotation when the buffer has not filled the full window.
# ─────────────────────────────────────────────────────────────────────
def _maybe_annotate_data_start(
    ax_price,
    *,
    first_bar_ts: datetime | None,
    alert_clock: datetime,
    window_minutes: int,
) -> None:
    """Show a notice when the first bar timestamp falls inside the window.

    Example:
        "data starts: 2026-05-15 00:12 PT (38 min ago)"
    """
    if first_bar_ts is None:
        return
    if first_bar_ts.tzinfo is None:
        # Safety guard: all inputs should be timezone-aware.
        return

    start_off_min = (first_bar_ts - alert_clock).total_seconds() / 60.0
    # If it is already before the window, coverage is sufficient and no notice is needed.
    if start_off_min <= -window_minutes:
        return
    # A future timestamp (positive offset) is invalid.
    if start_off_min > 0.0:
        return

    pt_str = first_bar_ts.astimezone(_DISPLAY_TZ).strftime(
        "%Y-%m-%d %H:%M %Z",
    )
    minutes_ago = int(round(-start_off_min))
    label = f"data starts: {pt_str}  ({minutes_ago} min ago)"
    ax_price.text(
        0.005, 0.02, label,
        transform=ax_price.transAxes,
        fontsize=8, color="#888888", style="italic",
        ha="left", va="bottom",
        bbox=dict(
            facecolor="white", alpha=0.85, edgecolor="#ddd", pad=2.0,
        ),
    )


__all__ = [
    "DEFAULT_WINDOW_MINUTES_BY_TIER",
    "EMAIL_STACK_WINDOWS",
    "DEFAULT_VOLUME_UNIT",
    "write_alert_window_plot",
]
