"""
reporters/channel_alerts.py — Per-channel alert event CSV (P10.5 cooldown v2).

────────────────────────────────────────────────────────────────────────
Role:
  per_channel/<ch>.csv has one row per minute × channel → noisy.
  This reporter applies **per-(channel, symbol, tier) cooldown +
  demote-silent + escalation** rules on top of that raw output to extract
  only the "alerts an operator actually receives".

  Cooldown granularity moves from v1 (per channel) → v2
  (per channel × symbol × tier), so when a different symbol fires within the
  same channel (e.g. CME CL EMERGENCY 30 min after CME BZ EMERGENCY), it
  produces a separate alert. A different asset moving is a different signal.

────────────────────────────────────────────────────────────────────────
Cooldown rules (v2 — user decision locked 2026-04-21):

  Per (channel, symbol), track the "max tier emitted within the last 24h"
  (`_seen_24h`). Per (channel, symbol, tier), track the "last emit time for
  that exact key" (`_last_sent`).

  When a new minute signal arrives (sig.ts, sig.tier, sig.symbol, channel):

    1) sig.tier == NORMAL                                      → silent (normal_skip)

    2) max_seen_24h(ch, sym) exists and sig.tier < max_seen_24h
       → silent (demote_silent)
       ↳ Don't send downgrade alerts like "user got EMERGENCY 1h ago, now
         dropping to RISK_OFF" (user decision).

    3) last_sent[(ch, sym, tier)] is None:
        a) max_seen_24h(ch, sym) is None     → emit `initial`
        b) sig.tier > max_seen_24h           → emit `escalation(prev=<X>)`
        ↳ First emit within 24h for this (channel, symbol, tier) combo.
          (b is the case where the tier went up one step within the same asset.)

    4) elapsed since last_sent[(ch, sym, tier)] >= cooldown
       → emit `cooldown_expired`
       ↳ After 24h, send again even for the same tier ("still at risk"
         reminder).

    5) Otherwise                                               → silent (suppressed_cooldown)

  Just after emit:
    · `_last_sent[(ch, sym, tier)] = sig.ts`
    · `_seen_24h[(ch, sym)] .append((sig.ts, sig.tier))`
       (this deque pops entries older than cutoff on every call).

────────────────────────────────────────────────────────────────────────
Background for the user decision (alert-fatigue analysis):

  · v1's escalation rule (`tier > last.tier → pass immediately`) let an
    oscillating Polymarket event with EMERGENCY → RISK_OFF → EMERGENCY →
    RISK_OFF sequences pass each time, producing 278 alerts over 6 days
    (≈ one every 31 minutes).
  · In v2, once EMERGENCY is emitted within a single (ch, sym), the
    (ch, sym, EMERGENCY) key is locked for 24h → demotes (RISK_OFF) are
    silent → re-spikes (EMERGENCY) are silent too (cooldown). The net effect
    is a strong 24h "EMERGENCY ceiling lock".
  · Other (channel, symbol) pairs are independent → CME BZ EMERGENCY followed
    by CME CL EMERGENCY is preserved (cross-symbol corroboration kept).
  · Downgrade alerts are out of scope for production (user decision — "no
    need to be alerted about risk receding").

────────────────────────────────────────────────────────────────────────
Output schema (channel_alerts.csv):

  alert_ts            — sim_clock (UTC ISO)
  alert_offset_min    — minutes from announcement_ts (positive=after, negative=before)
  channel             — polymarket | cme | hyperliquid | x
  symbol              — symbol at the time of fire
  tier                — WATCH | RISK_OFF | EMERGENCY
  reason              — initial | escalation(prev=<X>) | cooldown_expired
  fired_detectors     — '|'-separated
  reason_codes        — '|'-separated (detector reason text)
  score               — 0~1
  direction           — up | down | neutral
"""

from __future__ import annotations

import csv
import logging
from collections import deque
from datetime import timedelta
from pathlib import Path

# P10.5 → cooldown logic was extracted to alerts/cooldown.py so the production
# daemon can share it (a.4.1, 2026-04-21).
# This reporter reuses that module's pure decision function + state containers.
from ...alerts.cooldown import (
    _LastSent,
    _SeenWindow,
    _decide_emit,
)
from ...core.schemas import Tier
from ..schemas import ReplayResult

logger = logging.getLogger(__name__)


# CSV columns are 100% compatible with v1 — existing analysis notebooks still work.
CHANNEL_ALERTS_COLUMNS: list[str] = [
    "alert_ts",
    "alert_offset_min",
    "channel",
    "symbol",
    "tier",
    "reason",
    "fired_detectors",
    "reason_codes",
    "score",
    "direction",
]


# ─────────────────────────────────────────────────────────────────────
# Main entry — ReplayResult → CSV.
# ─────────────────────────────────────────────────────────────────────
def write_channel_alerts_csv(
    result: ReplayResult,
    out_path: Path,
    *,
    cooldown_minutes: int = 1440,  # v2 default = 24h (v1 was 60min)
) -> Path:
    """ReplayResult → channel_alerts.csv (only alerts that pass v2 cooldown).

    Args:
        result: the ReplayResult produced by ReplayRunner.
        out_path: csv file path to write (parent dir auto-created).
        cooldown_minutes: minimum re-alert interval (minutes) per
            (channel, symbol, tier) key. Default 1440 = 24h — strong
            anti-fatigue lock. Reduce only for tests or short-event debugging.

    Returns:
        out_path (for caller convenience).

    Note:
        Pure function — same ReplayResult + same cooldown → same result.
        Leaves per_channel/<ch>.csv alone and only writes this file extra.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cooldown = timedelta(minutes=cooldown_minutes)
    announce_ts = result.event.announcement_ts

    # State carried by the caller — _decide_emit is a pure function.
    last_sent: dict[tuple[str, str, Tier], _LastSent] = {}
    seen_window: dict[tuple[str, str], _SeenWindow] = {}

    rows: list[dict[str, str]] = []

    # minutes are sim_clock-ascending (guaranteed by ReplayResult).
    for minute in result.minutes:
        for ch_name, sig in minute.per_channel_signals.items():
            if sig is None:
                continue

            emit, reason = _decide_emit(
                sig,
                channel=ch_name,
                last_sent=last_sent,
                seen_window=seen_window,
                cooldown=cooldown,
            )
            if not emit:
                continue

            # Write the CSV row.
            offset_min = (sig.ts - announce_ts).total_seconds() / 60.0
            rows.append({
                "alert_ts": sig.ts.isoformat(),
                "alert_offset_min": f"{offset_min:+.2f}",
                "channel": ch_name,
                "symbol": sig.symbol,
                "tier": sig.tier.value,
                "reason": reason,
                "fired_detectors": "|".join(sig.fired_detectors),
                "reason_codes": "|".join(sig.reason_codes),
                "score": f"{sig.score:.4f}",
                "direction": sig.direction.value,
            })

            # State update — only record what was emitted.
            cst_key = (ch_name, sig.symbol, sig.tier)
            last_sent[cst_key] = _LastSent(ts=sig.ts)

            cs_key = (ch_name, sig.symbol)
            window = seen_window.setdefault(cs_key, deque())
            window.append((sig.ts, sig.tier))

    # atomic write — tmp → rename (same pattern as csv_report.py).
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CHANNEL_ALERTS_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(out_path)

    logger.info(
        "channel_alerts(v2): wrote %d alerts (cooldown=%dmin) → %s",
        len(rows), cooldown_minutes, out_path,
    )
    return out_path


__all__ = [
    "CHANNEL_ALERTS_COLUMNS",
    "write_channel_alerts_csv",
]
