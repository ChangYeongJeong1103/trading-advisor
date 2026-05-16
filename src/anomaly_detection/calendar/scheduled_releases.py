"""
calendar/scheduled_releases.py — pre-announced economic release windows
                                  (P12-A: calendar filter for CME insider_v1).

────────────────────────────────────────────────────────────────────────
Role:
  When the CME insider_v1 detector catches an anomaly, check whether the
  timestamp falls in a "pre-announced economic release" window. If yes,
  classify it as SCHEDULED_BURST so ChannelAlertDispatcher suppresses the alert.

  Why:
    P12 false-positive analysis (3-day baseline, BZ/CL):
      raw EMERGENCY: 13 events (4.33/day)  ← exceeds target of 0-2/day
      cooldown only: 4 events  (1.33/day)
      cooldown + this calendar filter: 3 events (1.00/day)  ← target met

    EIA/API/NFP release times are public knowledge; bursts around those times
    are normal macro flow, not "insider". From the user's perspective they're
    already on the calendar — repeating them is a duplicate alert.

────────────────────────────────────────────────────────────────────────
Public API:

  · `ScheduledReleaseWindow` — dataclass. Spec for one release's window.
  · `DEFAULT_SCHEDULED_WINDOWS` — v1 default windows (EIA/API/NFP).
  · `is_in_scheduled_window(ts, channel, symbol, windows=None)` — pure
    function. Returns (in_window: bool, label: str).

────────────────────────────────────────────────────────────────────────
v1 windows (UTC, ±90-minute buffer included — pre-positioning + post-reaction):

  · Tuesdays  19:00-22:00 UTC → API Crude Oil          (release 20:30, BZ/CL)
  · Wednesdays 13:00-16:30 UTC → EIA Petroleum Status   (release 14:30, BZ/CL)
  · Thursdays  13:00-16:30 UTC → EIA Natural Gas        (release 14:30, BZ/CL)
  · 1st Friday of month 11:00-14:00 UTC → NFP            (release 12:30, all)

  ↑ Why ±90 min: false-positive analysis found both 52 min pre-release BZ
  EMERGENCY (pre-positioning) and a 1.5h post-release reaction. Too narrow
  (±0–30 min) misses real bursts; too wide (±3h) absorbs other-time anomalies.

  TODO P12-A.2:
    · CPI/PPI: Tue/Wed between days 8-15 of month at 12:30 UTC (hard-coded dates needed)
    · FOMC statement: every 8 weeks 18:00 UTC (Fed dot plot date table)
    · OPEC+ monthly meeting: first week of month (varies)

────────────────────────────────────────────────────────────────────────
Usage example (inside channel_dispatcher):

    # Calendar check before cooldown — so cooldown state isn't polluted.
    in_sched, label = is_in_scheduled_window(sig.ts, channel, sig.symbol)
    if in_sched:
        self._stats["suppressed_scheduled"] += 1
        logger.info("calendar suppress: %s @ %s", label, sig.ts)
        continue
    # Then cooldown.decide(...)
"""

from __future__ import annotations

# ── standard library ──────────────────────────────────────────────────
from dataclasses import dataclass, field
from datetime import datetime, time, timezone

# ─────────────────────────────────────────────────────────────────────
# Window spec
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ScheduledReleaseWindow:
    """Spec for one recurring economic release window.

    Attributes:
        label: human-readable label. e.g. "EIA Petroleum".
        weekday: 0=Mon, 1=Tue, ..., 6=Sun (Python `datetime.weekday()`).
        start_time: UTC start time (inclusive).
        end_time:   UTC end time (inclusive).
        day_of_month_max: only apply when day-of-month ≤ this. None=unlimited.
            (e.g. NFP is the 1st Friday of the month → day_of_month_max=7.)
        applies_to_channels: set of channel names this release affects.
            Empty set = every channel.
        applies_to_symbols: set of symbols this release affects.
            Empty set = every symbol.
    """

    label: str
    weekday: int
    start_time: time
    end_time: time
    day_of_month_max: int | None = None
    applies_to_channels: frozenset[str] = field(default_factory=frozenset)
    applies_to_symbols: frozenset[str] = field(default_factory=frozenset)

    def matches(self, ts: datetime, channel: str, symbol: str) -> bool:
        """Does this ts × channel × symbol fall within the window?

        Note:
            ts is assumed UTC. If another tz, the caller normalizes before passing.
        """
        # If tz-aware, convert to UTC (naive → assume UTC).
        if ts.tzinfo is not None:
            ts = ts.astimezone(timezone.utc)

        if ts.weekday() != self.weekday:
            return False
        if self.day_of_month_max is not None and ts.day > self.day_of_month_max:
            return False
        t = ts.time()
        if not (self.start_time <= t <= self.end_time):
            return False
        if self.applies_to_channels and channel not in self.applies_to_channels:
            return False
        if self.applies_to_symbols and symbol not in self.applies_to_symbols:
            return False
        return True


# ─────────────────────────────────────────────────────────────────────
# v1 default windows — focused on CME oil (driven by false-positive analysis)
# ─────────────────────────────────────────────────────────────────────
# Weekday constants (readability).
_TUE = 1
_WED = 2
_THU = 3
_FRI = 4

# CME oil set.
_CME_OIL = frozenset({"BZ", "CL"})
_CME_CHAN = frozenset({"cme"})

DEFAULT_SCHEDULED_WINDOWS: tuple[ScheduledReleaseWindow, ...] = (
    # API Crude Oil — Tue 20:30 UTC release. ±90 min = 19:00~22:00.
    ScheduledReleaseWindow(
        label="API Crude",
        weekday=_TUE,
        start_time=time(19, 0),
        end_time=time(22, 0),
        applies_to_channels=_CME_CHAN,
        applies_to_symbols=_CME_OIL,
    ),
    # EIA Petroleum Status — Wed 14:30 UTC release. ±90 min = 13:00~16:30.
    ScheduledReleaseWindow(
        label="EIA Petroleum",
        weekday=_WED,
        start_time=time(13, 0),
        end_time=time(16, 30),
        applies_to_channels=_CME_CHAN,
        applies_to_symbols=_CME_OIL,
    ),
    # EIA Natural Gas Storage — Thu 14:30 UTC release. ±90 min = 13:00~16:30.
    # We assume BZ/CL are also affected (sympathy move). We don't trade NG
    # futures (HH), but oil tends to wobble at the same time.
    ScheduledReleaseWindow(
        label="EIA NatGas",
        weekday=_THU,
        start_time=time(13, 0),
        end_time=time(16, 30),
        applies_to_channels=_CME_CHAN,
        applies_to_symbols=_CME_OIL,
    ),
    # Non-Farm Payrolls — 1st Fri 12:30 UTC release. ±90 min = 11:00~14:00.
    # Macro → affects every CME instrument (oil included). symbols left empty = all.
    ScheduledReleaseWindow(
        label="NFP",
        weekday=_FRI,
        start_time=time(11, 0),
        end_time=time(14, 0),
        day_of_month_max=7,                # First Friday.
        applies_to_channels=_CME_CHAN,
    ),
)


# ─────────────────────────────────────────────────────────────────────
# Pure decision function — called by the dispatcher.
# ─────────────────────────────────────────────────────────────────────
def is_in_scheduled_window(
    ts: datetime,
    channel: str,
    symbol: str,
    windows: tuple[ScheduledReleaseWindow, ...] | None = None,
) -> tuple[bool, str]:
    """Does ts × channel × symbol fall inside a pre-announced release window?

    Args:
        ts: alert timestamp (UTC recommended).
        channel: channel name. e.g. "cme".
        symbol: symbol ticker. e.g. "BZ".
        windows: list of windows to apply. If None, DEFAULT_SCHEDULED_WINDOWS.

    Returns:
        (in_window, label).
        in_window=True → label is the matched release name.
        in_window=False → label="" .

    Note:
        If two or more windows match simultaneously, return the _first_ match
        (priority follows the declaration order of DEFAULT_SCHEDULED_WINDOWS).
    """
    cfg = windows if windows is not None else DEFAULT_SCHEDULED_WINDOWS
    for win in cfg:
        if win.matches(ts, channel, symbol):
            return True, win.label
    return False, ""
