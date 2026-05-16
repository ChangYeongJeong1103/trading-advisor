"""
reporters/csv_report.py — ReplayResult → CSV.

────────────────────────────────────────────────────────────────────────
Two kinds of output:

  1. summary.csv (cross-event aggregate, used in --all mode or cumulatively appended)
       columns: event_id, primary_channel, announcement_ts,
                max_tier_reached, first_alert_ts, first_alert_tier,
                detection_latency_s, warning_time_s, fp_count,
                channels_fired, total_minutes, replay_started_at

       1 event = 1 row. If the same event_id already exists, the row is
       overwritten (re-run result wins — the last run is the latest).

  2. per_channel/<channel>.csv (per-event detail)
       columns: sim_clock, sim_clock_offset_min,
                channel, symbol, tier, score, fired_detectors, reason_codes
       One row per minute × channel. Humans analyze it via sort/filter in Excel.

────────────────────────────────────────────────────────────────────────
Design decisions:

  · stdlib csv only (no pandas — replay artifacts should be lightweight).

  · summary.csv uses atomic writes (tmp → rename) — prevents partial-write corruption.

  · Re-running the same event only updates that event's row in summary.csv.
    Otherwise multiple rows would pile up and make cross-event comparisons noisy.

────────────────────────────────────────────────────────────────────────
Reference: docs/p10-replay-framework.md §2.6, §6
"""

from __future__ import annotations

# --- standard library ---
import csv
import logging
from pathlib import Path

# --- local ---
from ...core.schemas import ALL_CHANNELS
from ..schemas import ReplayResult

logger = logging.getLogger(__name__)


def _peak_symbol_per_channel(result: ReplayResult) -> dict[str, str]:
    """The symbol that produced the highest tier per channel → dict.

    Same tier → the symbol of the earliest minute.
    If the channel never fired (NORMAL only) → empty string.
    """
    # channel → (best_rank, symbol). best_rank = -1 → not yet fired.
    best: dict[str, tuple[int, str]] = {ch: (-1, "") for ch in ALL_CHANNELS}
    for m in result.minutes:
        for ch, sig in m.per_channel_signals.items():
            if sig is None:
                continue
            r = sig.tier.rank()
            if r <= 0:
                continue  # NORMAL — not a fire
            if r > best.get(ch, (-1, ""))[0]:
                best[ch] = (r, sig.symbol)
    return {ch: sym for ch, (_, sym) in best.items()}


# ─────────────────────────────────────────────────────────────────────
# summary.csv — cross-event aggregate.
# ─────────────────────────────────────────────────────────────────────
SUMMARY_COLUMNS: list[str] = [
    "event_id",
    "primary_channel",
    "announcement_ts",
    "max_tier_reached",
    "first_alert_ts",
    "first_alert_tier",
    "detection_latency_s",
    "warning_time_s",
    "fp_count",
    "channels_fired",
    # peak_symbol_<channel> — symbol that produced the strongest signal on that channel.
    # For multi-symbol channels like CME, record which contract burst (e.g. BZ).
    # Empty string if the channel never signaled.
    "peak_symbol_cme",
    "peak_symbol_polymarket",
    "peak_symbol_hyperliquid",
    "peak_symbol_x",
    "total_minutes",
    "replay_started_at",
]


def _result_to_summary_row(result: ReplayResult) -> dict[str, str]:
    """ReplayResult → 1 dict (CSV row)."""
    m = result.metrics
    peaks = _peak_symbol_per_channel(result)
    return {
        "event_id": result.event.event_id,
        "primary_channel": result.event.primary_channel,
        "announcement_ts": result.event.announcement_ts.isoformat(),
        "max_tier_reached": m.max_tier_reached.value,
        "first_alert_ts": m.first_alert_ts.isoformat() if m.first_alert_ts else "",
        "first_alert_tier": m.first_alert_tier.value if m.first_alert_tier else "",
        "detection_latency_s": (
            f"{m.detection_latency_s:.1f}" if m.detection_latency_s is not None else ""
        ),
        "warning_time_s": (
            f"{m.warning_time_s:.1f}" if m.warning_time_s is not None else ""
        ),
        "fp_count": str(m.fp_count),
        # Multiple channels in one CSV cell — use '|' to avoid commas inside commas.
        "channels_fired": "|".join(m.channels_fired),
        "peak_symbol_cme": peaks.get("cme", ""),
        "peak_symbol_polymarket": peaks.get("polymarket", ""),
        "peak_symbol_hyperliquid": peaks.get("hyperliquid", ""),
        "peak_symbol_x": peaks.get("x", ""),
        "total_minutes": str(len(result.minutes)),
        "replay_started_at": result.started_at.isoformat(),
    }


def write_summary_row(result: ReplayResult, summary_path: Path) -> None:
    """Upsert this result's row into summary.csv (keyed by event_id).

    If the file does not exist, create it (with header).
    If it exists, overwrite the same-event_id row and keep the rest as-is.
    """
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    new_row = _result_to_summary_row(result)

    # Read existing rows (if any). Keep everything except the conflicting event_id.
    existing: list[dict[str, str]] = []
    if summary_path.exists():
        with summary_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # If the column schema has changed — older rows have empty
                # values for missing columns. Ignore new columns (forward compat).
                if row.get("event_id") != new_row["event_id"]:
                    existing.append(row)

    # new + existing → sort by event_id for deterministic order.
    rows = existing + [new_row]
    rows.sort(key=lambda r: r.get("event_id", ""))

    # atomic write — tmp → rename.
    tmp_path = summary_path.with_suffix(summary_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            # Drop keys not in SUMMARY_COLUMNS (extrasaction=ignore).
            # Fill missing keys with empty strings.
            full_row = {col: row.get(col, "") for col in SUMMARY_COLUMNS}
            writer.writerow(full_row)
    tmp_path.replace(summary_path)

    logger.info(
        "csv_report: summary upserted event=%s → %s (%d rows total)",
        result.event.event_id, summary_path, len(rows),
    )


# ─────────────────────────────────────────────────────────────────────
# per_channel/<channel>.csv — per-event detail.
# ─────────────────────────────────────────────────────────────────────
PER_CHANNEL_COLUMNS: list[str] = [
    "sim_clock",
    "sim_clock_offset_min",
    "channel",
    "symbol",
    "tier",
    "score",
    "direction",
    "confidence",
    "fired_detectors",
    "reason_codes",
]


def write_per_channel_csv(
    result: ReplayResult,
    per_channel_dir: Path,
) -> dict[str, Path]:
    """Write a minute-level CSV per channel under per_channel_dir for this event.

    Returns:
        dict[channel_name, Path]: the paths written.
    """
    per_channel_dir.mkdir(parents=True, exist_ok=True)

    # channel_name → list[row dict]. Prepare all 4 channels in advance.
    by_channel: dict[str, list[dict[str, str]]] = {}

    announce_ts = result.event.announcement_ts

    for m in result.minutes:
        offset_min = (m.sim_clock - announce_ts).total_seconds() / 60.0
        for ch_name, sig in m.per_channel_signals.items():
            row: dict[str, str] = {
                "sim_clock": m.sim_clock.isoformat(),
                "sim_clock_offset_min": f"{offset_min:+.2f}",
                "channel": ch_name,
                "symbol": sig.symbol if sig else "",
                "tier": sig.tier.value if sig else "NORMAL",
                "score": f"{sig.score:.4f}" if sig else "0.0000",
                "direction": sig.direction.value if sig else "neutral",
                "confidence": f"{sig.confidence:.2f}" if sig else "0.00",
                # list → '|' separator (avoids commas inside cells).
                "fired_detectors": "|".join(sig.fired_detectors) if sig else "",
                "reason_codes": "|".join(sig.reason_codes) if sig else "",
            }
            by_channel.setdefault(ch_name, []).append(row)

    written: dict[str, Path] = {}
    for ch_name, rows in by_channel.items():
        path = per_channel_dir / f"{ch_name}.csv"
        # atomic write.
        tmp = path.with_suffix(".csv.tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=PER_CHANNEL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        tmp.replace(path)
        written[ch_name] = path
        logger.debug("csv_report: per_channel %s → %s (%d rows)", ch_name, path, len(rows))

    logger.info(
        "csv_report: per_channel CSVs written for event=%s → %s (%d channels)",
        result.event.event_id, per_channel_dir, len(written),
    )
    return written


__all__ = [
    "SUMMARY_COLUMNS",
    "PER_CHANNEL_COLUMNS",
    "write_summary_row",
    "write_per_channel_csv",
]
