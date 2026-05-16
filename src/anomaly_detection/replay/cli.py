"""
replay/cli.py — `python -m anomaly.replay <event_id>` entry point.

────────────────────────────────────────────────────────────────────────
Usage:

    # Default usage — replay 1 event and emit CSV/YAML/PNG
    python -m anomaly.replay 2025-04-09_liberation_day

    # Override the output path
    python -m anomaly.replay 2025-04-09_liberation_day \
        --out-dir data/anomaly/replay_results

    # Write every cycle to the YAML timeline (debug — normally only changed cycles)
    python -m anomaly.replay 2025-04-09_liberation_day --full-timeline

    # Only update the CSV summary; skip YAML/PNG (for fast batches)
    python -m anomaly.replay 2025-04-09_liberation_day --no-yaml --no-plot

────────────────────────────────────────────────────────────────────────
v0.1 channel wiring (automatic):

    · CME         → CmeChannelReplay     (real Databento data)
    · Polymarket  → PolymarketChannelReplay (Polymarket public Data API)
    · others (2)  → NullChannelReplay   (skeleton, doesn't fire)

  → Current stage: CME + Polymarket with real detectors. Hyperliquid/X are next.
────────────────────────────────────────────────────────────────────────
Output layout:

  data/anomaly/replay_results/
    summary.csv                            ← cross-event aggregate
    <event_id>/
      report.yaml                          ← per-event YAML (metric + timeline)
      timeline.png                         ← matplotlib plot
      per_channel/
        cme.csv                            ← minute × channel CSV (4 files)
        polymarket.csv
        hyperliquid.csv
        x.csv
"""

from __future__ import annotations

# --- standard library ---
import argparse
import asyncio
import logging
import sys
from pathlib import Path

# --- local ---
from .channel_replays import (
    CmeChannelReplay,
    HyperliquidChannelReplay,
    PolymarketChannelReplay,
)
from .data_sources.cme_databento import CmeDatabentoSource
from .data_sources.hyperliquid_trades_csv import HyperliquidTradesCsvSource
from .data_sources.polymarket_data_api import PolymarketDataApiSource
from .event_library import EventLibrary
from .reporters import (
    write_channel_alerts_csv,
    write_per_channel_csv,
    write_summary_row,
    write_timeline_plot,
    write_yaml_report,
)
from .runner import ReplayRunner
from .schemas import ReplayResult

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Default paths — relative to the repo root.
# ─────────────────────────────────────────────────────────────────────
def _default_events_dir() -> Path:
    """data/anomaly/historical_events path (relative to repo root)."""
    return _repo_root() / "data" / "anomaly" / "historical_events"


def _default_out_dir() -> Path:
    """data/anomaly/replay_results path (relative to repo root)."""
    return _repo_root() / "data" / "anomaly" / "replay_results"


def _repo_root() -> Path:
    """src/anomaly/replay/cli.py → repo root (3 levels up)."""
    # This file: <repo>/src/anomaly/replay/cli.py → parents: [replay, anomaly, src, repo]
    return Path(__file__).resolve().parents[3]


# ─────────────────────────────────────────────────────────────────────
# Channel wiring (v0).
# ─────────────────────────────────────────────────────────────────────
def _build_channels() -> dict[str, object]:
    """v0.2: CME + Polymarket + Hyperliquid are real; rest are auto-filled with NullChannelReplay by the runner.

    Env / data dependencies:
      · CME         — DATABENTO_API_KEY (otherwise source.warmup() raises)
      · Polymarket  — none (public Data API + Gamma are unauthenticated)
      · Hyperliquid — data/hyperliquid-trades-*.csv.gz (hypedexer export). If
                      missing, source.supports()=False → warmup is skipped.

    For events where the source's supports() is False, warmup() returns immediately —
    e.g. for an event unrelated to CME, no Databento call happens → no API-key
    error even if it's missing.
    """
    cme_source = CmeDatabentoSource()
    poly_source = PolymarketDataApiSource()
    hl_source = HyperliquidTradesCsvSource()
    return {
        "cme": CmeChannelReplay(source=cme_source),
        "polymarket": PolymarketChannelReplay(source=poly_source),
        "hyperliquid": HyperliquidChannelReplay(source=hl_source),
    }


# ─────────────────────────────────────────────────────────────────────
# Bundle reporters into a single call.
# ─────────────────────────────────────────────────────────────────────
def _emit_reports(
    result: ReplayResult,
    out_dir: Path,
    *,
    write_yaml: bool,
    write_plot: bool,
    full_timeline: bool,
    alert_cooldown_minutes: int = 1440,
    plot_xlim_min: float | None = None,
    plot_xlim_max: float | None = None,
) -> dict[str, Path]:
    """ReplayResult → CSV/YAML/PNG. Returns a dict of which files were written."""
    event_dir = out_dir / result.event.event_id
    event_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    # 1) summary.csv (cross-event aggregate, always updated).
    summary_path = out_dir / "summary.csv"
    write_summary_row(result, summary_path)
    written["summary_csv"] = summary_path

    # 2) per_channel/*.csv (per-event detail, always written).
    per_channel_dir = event_dir / "per_channel"
    files = write_per_channel_csv(result, per_channel_dir)
    for ch, p in files.items():
        written[f"per_channel_{ch}"] = p

    # 2b) channel_alerts.csv — extract "operator alerts" with cooldown applied (P10.4).
    #     Deduplicates same channel/same tier within 1 hour on top of the raw
    #     minute snapshots in per_channel/*.csv. Use to see the count of alerts
    #     a real human would actually receive.
    alerts_path = event_dir / "channel_alerts.csv"
    write_channel_alerts_csv(
        result, alerts_path, cooldown_minutes=alert_cooldown_minutes,
    )
    written["channel_alerts"] = alerts_path

    # 3) report.yaml (optional).
    if write_yaml:
        yaml_path = event_dir / "report.yaml"
        write_yaml_report(result, yaml_path, full_timeline=full_timeline)
        written["yaml"] = yaml_path

    # 4) timeline.png (optional).
    if write_plot:
        plot_path = event_dir / "timeline.png"
        write_timeline_plot(
            result, plot_path,
            xlim_min=plot_xlim_min,
            xlim_max=plot_xlim_max,
        )
        written["plot"] = plot_path

    return written


# ─────────────────────────────────────────────────────────────────────
# Main async — run one event.
# ─────────────────────────────────────────────────────────────────────
async def run_one(
    event_id: str,
    *,
    events_dir: Path,
    out_dir: Path,
    write_yaml: bool,
    write_plot: bool,
    full_timeline: bool,
    alert_cooldown_minutes: int = 1440,
    plot_xlim_min: float | None = None,
    plot_xlim_max: float | None = None,
) -> ReplayResult:
    """One event's replay → reporter chain. Returns the (in-memory) result."""
    library = EventLibrary.from_dir(events_dir)
    event = library.get(event_id)
    logger.info(
        "Loaded event: %s  primary=%s symbols=%s window=%d min",
        event.event_id, event.primary_channel, event.primary_symbols, event.total_minutes,
    )

    channels = _build_channels()
    runner = ReplayRunner(channels=channels)
    result = await runner.run(event)

    written = _emit_reports(
        result, out_dir,
        write_yaml=write_yaml, write_plot=write_plot,
        full_timeline=full_timeline,
        alert_cooldown_minutes=alert_cooldown_minutes,
        plot_xlim_min=plot_xlim_min,
        plot_xlim_max=plot_xlim_max,
    )
    print()
    print(f"event_id        : {result.event.event_id}")
    print(f"max_tier        : {result.metrics.max_tier_reached.value}")
    if result.metrics.first_alert_ts:
        offset_min = (
            result.metrics.first_alert_ts - result.event.announcement_ts
        ).total_seconds() / 60.0
        print(
            f"first_alert     : {result.metrics.first_alert_tier.value if result.metrics.first_alert_tier else '-'} "
            f"@ T{offset_min:+.1f}min ({result.metrics.first_alert_ts.isoformat()})"
        )
    else:
        print("first_alert     : (none)")
    if result.metrics.warning_time_s is not None:
        print(f"warning_time    : {result.metrics.warning_time_s/60.0:+.1f} min")
    print(f"channels_fired  : {','.join(result.metrics.channels_fired) or '-'}")
    print()
    print("Outputs:")
    for name, path in written.items():
        try:
            rel = path.relative_to(Path.cwd())
        except ValueError:
            rel = path
        print(f"  {name:20s} → {rel}")
    print()
    return result


# ─────────────────────────────────────────────────────────────────────
# argparse + entrypoint.
# ─────────────────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m anomaly.replay",
        description="Replay 1 historical event end-to-end (CME real data, others null).",
    )
    parser.add_argument(
        "event_id",
        help="event_id (filename minus .md). e.g. 2025-04-09_liberation_day",
    )
    parser.add_argument(
        "--events-dir", type=Path, default=None,
        help="historical events directory (default: data/anomaly/historical_events)",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=None,
        help="output directory (default: data/anomaly/replay_results)",
    )
    parser.add_argument(
        "--no-yaml", action="store_true",
        help="skip writing report.yaml",
    )
    parser.add_argument(
        "--no-plot", action="store_true",
        help="skip writing timeline.png (save matplotlib cost)",
    )
    parser.add_argument(
        "--full-timeline", action="store_true",
        help="include even NORMAL / unchanged cycles in the YAML timeline (debug)",
    )
    parser.add_argument(
        "--alert-cooldown-minutes", type=int, default=1440,
        help="per-(channel,symbol,tier) cooldown applied to channel_alerts.csv "
             "(minutes; default 1440 = 24h). Bundles alerts for the same "
             "(channel, symbol, tier) within this interval. Demotes are always "
             "silent; escalations always pass (when the tier first goes up for "
             "the same (ch, sym)).",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logger level (default: INFO)",
    )
    # plot zoom — useful for inspecting only a leading window in long events (≥several days).
    # Unit: minutes relative to announcement (T=0). Negative = pre-announcement.
    parser.add_argument(
        "--plot-xlim-min", type=float, default=None,
        help="plot x-axis minimum (minutes; T=announcement). Negative=pre-announcement. "
             "e.g. --plot-xlim-min=-4000 → show only from 67h before announcement.",
    )
    parser.add_argument(
        "--plot-xlim-max", type=float, default=None,
        help="plot x-axis maximum (minutes; T=announcement). "
             "e.g. --plot-xlim-max=60 → only up to 60 minutes after announcement.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    events_dir = args.events_dir or _default_events_dir()
    out_dir = args.out_dir or _default_out_dir()

    try:
        asyncio.run(
            run_one(
                args.event_id,
                events_dir=events_dir,
                out_dir=out_dir,
                write_yaml=not args.no_yaml,
                write_plot=not args.no_plot,
                full_timeline=args.full_timeline,
                alert_cooldown_minutes=args.alert_cooldown_minutes,
                plot_xlim_min=args.plot_xlim_min,
                plot_xlim_max=args.plot_xlim_max,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Replay failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
