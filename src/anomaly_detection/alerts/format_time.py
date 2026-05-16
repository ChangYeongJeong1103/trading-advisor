"""
alerts/format_time.py — PDT representation for timestamps shown in alert bodies.

────────────────────────────────────────────────────────────────────────
Role (P9.3.P1.D):

  The user (Bay Area resident) sees every alert time in PDT/PST.
  Internally the schema stores UTC (timezone-aware); only the moments
  shown to humans go through this module for conversion.

  Machine-readable timestamps (audit/log/snapshot) stay as ISO UTC.
  (Easier to search/filter/replay, so we don't change the format.)

────────────────────────────────────────────────────────────────────────
Format rules:

  · Default: "YYYY-MM-DD HH:MM PDT"  (e.g. "2026-04-19 03:50 PDT")
  · DST switches twice a year → zoneinfo automatically picks PDT/PST.
  · naive datetime is assumed to be UTC (schema convention); aware is
    converted as-is.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Bay Area — automatically switches between PDT (UTC-7) / PST (UTC-8) by DST
BAY_AREA_TZ = ZoneInfo("America/Los_Angeles")

# Default for alert body display. Seconds are hidden (detection is per-minute, so meaningless).
_FMT_DEFAULT = "%Y-%m-%d %H:%M %Z"


def format_pdt(dt: datetime, *, fmt: str = _FMT_DEFAULT) -> str:
    """UTC datetime → "2026-04-19 03:50 PDT" string.

    Args:
        dt: Time to convert. If naive, assumed to be UTC.
        fmt: strftime format. If it contains "%Z", PDT/PST is filled in automatically.

    Returns:
        Bay Area local representation string (DST handled automatically).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BAY_AREA_TZ).strftime(fmt)


__all__ = ["format_pdt", "BAY_AREA_TZ"]
