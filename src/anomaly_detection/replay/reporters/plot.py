"""
reporters/plot.py — ReplayResult → matplotlib timeline PNG.

────────────────────────────────────────────────────────────────────────
Plot layout (top → bottom):

   1) per-channel score (4 lanes, stem)  — "how much each channel detector spiked"
   2) per-channel tier  (4 lanes, step)  — "channel-level tier escalation"
   3) system_state      (1 lane,  step)  — "system-level aggregate (fusion + boost)"

  · X axis: minute offset with announcement at 0 (T-N min ~ T+N min).
  · A thick vertical line "ANNOUNCE" at the announcement (T=0).
  · A dashed vline + label at the first entry into each tier (WATCH / RISK_OFF / EMERGENCY).

────────────────────────────────────────────────────────────────────────
Design decisions:

  · Drawing fill_between(0, score) instead of stem is easier to read.
    → "spiked minutes" stand out at a glance. score=0 minutes are just the base line.

  · Tier step plots use tier.rank() (NORMAL=0..EMERGENCY=3) as integers.
    Y-axis ticks are labeled with the tier names → readable for non-engineers.

  · Consistent 4-channel colors (dict CHANNEL_COLORS).
    polymarket=blue, hyperliquid=orange, cme=green, x=purple.

  · Force the matplotlib 'Agg' backend → safe in headless / CI environments.

────────────────────────────────────────────────────────────────────────
Reference: docs/p10-replay-framework.md §6
"""

from __future__ import annotations

# --- standard library ---
import logging
from datetime import datetime
from pathlib import Path

# --- third-party ---
# Lock the backend before importing plt so it works headless (servers, CI).
import matplotlib
matplotlib.use("Agg")  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.ticker import MultipleLocator  # noqa: E402

# --- local ---
from ...core.schemas import (
    CHANNEL_CME,
    CHANNEL_HYPERLIQUID,
    CHANNEL_POLYMARKET,
    CHANNEL_TRUTH_SOCIAL,
    CHANNEL_X,
    Tier,
)
from ..schemas import ReplayResult

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Constants — color / order.
# ─────────────────────────────────────────────────────────────────────
# Channel display order (top → bottom in plot lanes).
CHANNEL_ORDER: list[str] = [
    CHANNEL_POLYMARKET,
    CHANNEL_HYPERLIQUID,
    CHANNEL_CME,
    CHANNEL_X,
    CHANNEL_TRUTH_SOCIAL,
]

# Consistent color palette — the same channel uses the same color in every plot.
# truth_social uses the Truth Social brand color (purple-ish — #5b48f0).
CHANNEL_COLORS: dict[str, str] = {
    CHANNEL_POLYMARKET:   "#1f77b4",  # blue
    CHANNEL_HYPERLIQUID:  "#ff7f0e",  # orange
    CHANNEL_CME:          "#2ca02c",  # green
    CHANNEL_X:            "#9467bd",  # purple
    CHANNEL_TRUTH_SOCIAL: "#5b48f0",  # Truth Social brand purple-blue
}

# Tier → color (system_state lane + vline).
TIER_COLORS: dict[Tier, str] = {
    Tier.NORMAL: "#7f7f7f",     # gray
    Tier.WATCH: "#bcbd22",      # olive/yellow
    Tier.RISK_OFF: "#ff7f0e",   # orange
    Tier.EMERGENCY: "#d62728",  # red
}

# Y-tick labels (tier rank 0..3).
TIER_LABELS: list[str] = ["NORMAL", "WATCH", "RISK_OFF", "EMERGENCY"]


# ─────────────────────────────────────────────────────────────────────
# Helpers — series extraction.
# ─────────────────────────────────────────────────────────────────────
def _offsets_minutes(
    sim_clocks: list[datetime],
    announce: datetime,
) -> list[float]:
    """sim_clock list → minute offset list (relative to announce)."""
    return [(t - announce).total_seconds() / 60.0 for t in sim_clocks]


def _channel_series(
    result: ReplayResult,
    channel: str,
) -> tuple[list[float], list[float], list[int]]:
    """Extract (offset, score, tier_rank) series for this channel.

    Minutes whose signal is None are filled with score=0, tier_rank=0 (NORMAL).
    """
    sim_clocks: list[datetime] = []
    scores: list[float] = []
    tier_ranks: list[int] = []
    for m in result.minutes:
        sim_clocks.append(m.sim_clock)
        sig = m.per_channel_signals.get(channel)
        if sig is None:
            scores.append(0.0)
            tier_ranks.append(Tier.NORMAL.rank())
        else:
            scores.append(sig.score)
            tier_ranks.append(sig.tier.rank())
    offsets = _offsets_minutes(sim_clocks, result.event.announcement_ts)
    return offsets, scores, tier_ranks


def _system_state_series(result: ReplayResult) -> tuple[list[float], list[int]]:
    """Actual system_state (state_manager output) (offset, tier_rank) series.

    Tracks "the tier state_manager actually believed this minute" by walking
    decision.state_change (not fused_event.state).
    Even if fusion shouts EMERGENCY, system_state stays put unless the dwell
    time is met.
    """
    sim_clocks: list[datetime] = []
    ranks: list[int] = []

    # Starting state — state_manager's default initial_state = NORMAL.
    current_state: Tier = Tier.NORMAL
    for m in result.minutes:
        # If decision.state_change exists, the new state takes effect from that minute.
        if m.decision is not None and m.decision.state_change is not None:
            _, new_state = m.decision.state_change
            current_state = new_state
        sim_clocks.append(m.sim_clock)
        ranks.append(current_state.rank())
    offsets = _offsets_minutes(sim_clocks, result.event.announcement_ts)
    return offsets, ranks


def _tier_floor_series(result: ReplayResult) -> tuple[list[float], list[int]]:
    """fused_event.tier_floor (offset, tier_rank) series — the "production-equivalent" line.

    Replay samples one bar per minute, so a single-minute EMERGENCY pulse fails
    the hysteresis dwell time (state_manager resets to a different tier on the
    next minute).
    Production runs fusion many times per minute, easily passing the same dwell
    → escalates.

    This series shows "the tier system_state would have reached without the
    state_manager hysteresis" — i.e. production's reachable ceiling. Plotted
    as a dotted line so the actual system_state (solid black) can be compared.
    """
    sim_clocks: list[datetime] = []
    ranks: list[int] = []
    for m in result.minutes:
        sim_clocks.append(m.sim_clock)
        if m.fused_event is None:
            ranks.append(Tier.NORMAL.rank())
        else:
            ranks.append(m.fused_event.tier_floor.rank())
    offsets = _offsets_minutes(sim_clocks, result.event.announcement_ts)
    return offsets, ranks


def _peak_cme_burst(result: ReplayResult) -> tuple[str, float, Tier] | None:
    """(symbol, offset_min, tier) of the first occurrence of the highest CME tier.

    If multiple minutes share the same tier, take the earliest one.
    If CME signal never fired, return None.
    """
    announce = result.event.announcement_ts
    best: tuple[str, float, Tier] | None = None
    best_rank = -1
    for m in result.minutes:
        sig = m.per_channel_signals.get("cme")
        if sig is None or sig.tier == Tier.NORMAL:
            continue
        if sig.tier.rank() > best_rank:
            best_rank = sig.tier.rank()
            offset_min = (m.sim_clock - announce).total_seconds() / 60.0
            best = (sig.symbol, offset_min, sig.tier)
    return best


def _first_entry_per_tier(
    offsets: list[float],
    ranks: list[int],
) -> dict[Tier, float]:
    """Collect the offset at which each tier first appears (for vline markers)."""
    seen: dict[Tier, float] = {}
    for off, r in zip(offsets, ranks, strict=False):
        for tier in (Tier.WATCH, Tier.RISK_OFF, Tier.EMERGENCY):
            if r >= tier.rank() and tier not in seen:
                seen[tier] = off
    return seen


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────
def write_timeline_plot(
    result: ReplayResult,
    plot_path: Path,
    *,
    title: str | None = None,
    figsize: tuple[float, float] = (14.0, 9.0),
    dpi: int = 110,
    xlim_min: float | None = None,
    xlim_max: float | None = None,
) -> None:
    """Save ReplayResult as a 3-section matplotlib PNG.

    Args:
        result: one event replay result.
        plot_path: output PNG path.
        title: top title (event_id if omitted).
        figsize: (width, height) in inches.
        dpi: resolution.
        xlim_min: x-axis minimum (minutes with announce=T0; negative = pre-announcement).
            None → the full event window (= -event.pre_event_window_minutes).
            e.g. -4000 = show only from 67h before announcement.
        xlim_max: x-axis maximum (minutes). None → through the end of the data.
            e.g. 60 = only up to 1h after announcement.
    """
    plot_path.parent.mkdir(parents=True, exist_ok=True)
    if not result.minutes:
        logger.warning("plot: empty timeline for %s, skip", result.event.event_id)
        return

    # 3-row layout; height_ratios makes the top (score) largest.
    fig, axes = plt.subplots(
        nrows=3,
        ncols=1,
        figsize=figsize,
        dpi=dpi,
        sharex=True,
        gridspec_kw={"height_ratios": [3.0, 2.0, 1.2], "hspace": 0.18},
    )
    ax_score, ax_tier, ax_state = axes

    # Zoom range — used by in-range checks so announcement / first-entry vlines
    # and labels don't spill outside the visible area. If xlim_* is None, use
    # (-inf, inf).
    in_xlim_min = xlim_min if xlim_min is not None else float("-inf")
    in_xlim_max = xlim_max if xlim_max is not None else float("inf")

    def _in_xlim(off: float) -> bool:
        """Is offset inside the (post-zoom) visible region?"""
        return in_xlim_min <= off <= in_xlim_max

    # Announcement = X=0. vline only if inside the zoom range.
    if _in_xlim(0.0):
        for ax in axes:
            ax.axvline(0.0, color="black", linewidth=2.0, alpha=0.55, zorder=1)
            ax.axvline(0.0, color="black", linewidth=0.5, alpha=0.15, zorder=1)

    # ── Panel 1: per-channel score (fill_between baseline 0). ──────
    for ch in CHANNEL_ORDER:
        offsets, scores, _ = _channel_series(result, ch)
        color = CHANNEL_COLORS[ch]
        ax_score.fill_between(
            offsets, 0.0, scores,
            color=color, alpha=0.30, linewidth=0,
        )
        ax_score.plot(
            offsets, scores,
            color=color, linewidth=1.2, label=ch, alpha=0.95,
        )

    ax_score.set_ylabel("Channel score [0,1]", fontsize=10)
    ax_score.set_ylim(-0.02, 1.05)
    ax_score.set_title(
        title or f"Replay timeline — {result.event.event_id}",
        fontsize=13, loc="left", pad=12,
    )
    ax_score.legend(loc="upper left", fontsize=9, ncols=5, framealpha=0.85)
    ax_score.grid(True, alpha=0.25)

    # ── Panel 2: per-channel tier (step plot). ──────────────────────
    # Step plot of tier ranks for the 4 channels. Overlay in the same lane,
    # use alpha to separate.
    for ch in CHANNEL_ORDER:
        offsets, _, tier_ranks = _channel_series(result, ch)
        color = CHANNEL_COLORS[ch]
        ax_tier.step(
            offsets, tier_ranks,
            where="post", color=color, linewidth=1.6, alpha=0.85, label=ch,
        )

    ax_tier.set_ylabel("Channel tier", fontsize=10)
    ax_tier.set_yticks([0, 1, 2, 3])
    ax_tier.set_yticklabels(TIER_LABELS, fontsize=9)
    ax_tier.set_ylim(-0.3, 3.3)
    ax_tier.grid(True, axis="y", alpha=0.25)

    # ── Panel 3: system_state (solid black) + tier_floor (gray dotted overlay). ────
    sys_off, sys_ranks = _system_state_series(result)
    floor_off, floor_ranks = _tier_floor_series(result)

    # tier_floor — production-equivalent line (visualizes replay-vs-production gap).
    # Slightly thicker gray dotted. May reach "above" system_state.
    ax_state.step(
        floor_off, floor_ranks,
        where="post", color="#555555", linewidth=1.5,
        linestyle=":", alpha=0.85,
        label="tier_floor (production reach)",
    )
    # Actual system_state — thick black solid.
    ax_state.step(
        sys_off, sys_ranks,
        where="post", color="black", linewidth=2.2,
        label="system_state (replay)",
    )

    # Per-tier color shading — additional shading on the system_state lane only.
    for tier in (Tier.WATCH, Tier.RISK_OFF, Tier.EMERGENCY):
        ax_state.axhspan(
            tier.rank() - 0.5, tier.rank() + 0.5,
            color=TIER_COLORS[tier], alpha=0.06,
        )

    # vline + label at the time of the first alert (based on system_state).
    # Skip first_entry outside the zoom — prevents layout from getting clobbered
    # by off-screen data.
    # (e.g. --plot-xlim-min=-4000 with first_alert at T-8724 can't render —
    # matching the user's zoom intent anyway.)
    first_entries = _first_entry_per_tier(sys_off, sys_ranks)
    for tier, off in first_entries.items():
        if not _in_xlim(off):
            continue
        for ax in axes:
            ax.axvline(
                off, color=TIER_COLORS[tier], linewidth=1.2,
                linestyle="--", alpha=0.70, zorder=1,
            )
        # Place the label in the system_state lane near the corresponding tier height.
        ax_state.text(
            off, tier.rank() + 0.18,
            f" first {tier.value} @ T{off:+.0f}m",
            color=TIER_COLORS[tier], fontsize=8,
            ha="left", va="bottom", fontweight="bold",
        )

    ax_state.set_ylabel("system_state", fontsize=10)
    ax_state.set_yticks([0, 1, 2, 3])
    ax_state.set_yticklabels(TIER_LABELS, fontsize=9)
    ax_state.set_ylim(-0.5, 3.5)
    ax_state.set_xlabel("Minutes from announcement (T = 0)", fontsize=11)
    ax_state.grid(True, axis="y", alpha=0.25)

    # ── X-axis zoom (xlim) ─────────────────────────────────────────
    # sharex means setting xlim once on ax_state propagates to score / tier panels.
    # Specifying only one side still works (the other is auto from data).
    if xlim_min is not None or xlim_max is not None:
        cur_xmin, cur_xmax = ax_state.get_xlim()
        eff_min = xlim_min if xlim_min is not None else cur_xmin
        eff_max = xlim_max if xlim_max is not None else cur_xmax
        ax_state.set_xlim(eff_min, eff_max)

    # ── X-axis tick — auto-scale based on visible (post-zoom) span ──
    # Goal: ~10–25 major ticks. Too few = imprecise, too many = unreadable.
    # Use the full event length if not zoomed, otherwise the zoomed span.
    xmin_eff, xmax_eff = ax_state.get_xlim()
    span_min = max(int(round(xmax_eff - xmin_eff)), 1)
    if span_min <= 60:           # ~1 hour : 2 min
        major_step, minor_step = 2, 1
    elif span_min <= 180:        # ~3 hours: 4 min
        major_step, minor_step = 4, 2
    elif span_min <= 360:        # ~6 hours: 15 min
        major_step, minor_step = 15, 5
    elif span_min <= 720:        # ~12 hours: 30 min
        major_step, minor_step = 30, 10
    elif span_min <= 1440:       # ~24 hours: 1 hour (60 min)
        major_step, minor_step = 60, 15
    elif span_min <= 2880:       # ~48 hours: 2 hours (120 min)
        major_step, minor_step = 120, 30
    elif span_min <= 6000:       # ~100 hours: 4 hours (240 min)
        major_step, minor_step = 240, 60
    else:                         # > 100h: 8 hours
        major_step, minor_step = 480, 120
    ax_state.xaxis.set_major_locator(MultipleLocator(major_step))
    ax_state.xaxis.set_minor_locator(MultipleLocator(minor_step))
    # Smaller tick labels — they get denser.
    ax_state.tick_params(axis="x", labelsize=8)
    # Show minor grids in every panel (x-axis sharex).
    for ax in axes:
        ax.grid(True, which="minor", axis="x", alpha=0.10)
        ax.grid(True, which="major", axis="x", alpha=0.30)

    # Announce label (only on the top panel, only when 0 is inside the zoom).
    if _in_xlim(0.0):
        ymax_score = ax_score.get_ylim()[1]
        ax_score.text(
            0.0, ymax_score * 0.98, " ANNOUNCE",
            color="black", fontsize=9, fontweight="bold",
            ha="left", va="top",
        )

    # Bottom metric + CME burst summary — two lines (readable).
    m = result.metrics
    burst = _peak_cme_burst(result)
    summary_bits: list[str] = [
        f"max_tier={m.max_tier_reached.value}",
        (f"first_alert={m.first_alert_tier.value} @ T{(m.first_alert_ts - result.event.announcement_ts).total_seconds()/60.0:+.0f}m"
         if m.first_alert_ts and m.first_alert_tier else "first_alert=-"),
        (f"warning={m.warning_time_s/60.0:+.1f}min"
         if m.warning_time_s is not None else "warning=-"),
        f"channels_fired={','.join(m.channels_fired) if m.channels_fired else '-'}",
    ]
    fig.text(
        0.01, 0.020, "  ·  ".join(summary_bits),
        fontsize=9, color="#333333",
    )
    # Show the CME burst symbol — one line on which contract carried the key signal.
    if burst is not None:
        sym, off_min, peak_tier = burst
        burst_line = (
            f"CME burst: {sym}  peak={peak_tier.value} @ T{off_min:+.0f}min"
        )
        fig.text(
            0.01, 0.005, burst_line,
            fontsize=9, color=TIER_COLORS[peak_tier], fontweight="bold",
        )

    # Custom legend for system_state (panel 3) — band meaning + meaning of the two lines.
    band_handles = [
        Line2D([0], [0], color=TIER_COLORS[t], linewidth=6, alpha=0.5, label=t.value)
        for t in (Tier.NORMAL, Tier.WATCH, Tier.RISK_OFF, Tier.EMERGENCY)
    ]
    line_handles = [
        Line2D([0], [0], color="black", linewidth=2.2, label="system_state (replay)"),
        Line2D([0], [0], color="#555555", linewidth=1.5, linestyle=":",
               label="tier_floor (production reach)"),
    ]
    ax_state.legend(
        handles=line_handles + band_handles,
        loc="upper left", fontsize=7, ncols=3,
        framealpha=0.85, title=None,
    )

    # Reserve a bit at the bottom — room for the 2-line footer (summary + CME burst).
    fig.tight_layout(rect=(0.0, 0.04, 1.0, 0.99))

    # Atomic write — tmp → rename. Specify format (extension .tmp confuses inference).
    tmp_path = plot_path.with_suffix(plot_path.suffix + ".tmp")
    fig.savefig(tmp_path, dpi=dpi, bbox_inches="tight", format="png")
    plt.close(fig)
    tmp_path.replace(plot_path)

    logger.info("plot: timeline → %s", plot_path)


__all__ = ["write_timeline_plot", "CHANNEL_COLORS", "TIER_COLORS"]
