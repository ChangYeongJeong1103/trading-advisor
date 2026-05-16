"""anomaly/calendar — scheduled economic release windows (P12-A).

This package provides a calendar filter to reduce CME insider_v1 false positives.
Regular economic releases like EIA/API/NFP are pre-announced, so they are
normal macro flow — not insider trading — and not worth pushing as alerts.

ChannelAlertDispatcher calls this before the cooldown check → alerts within
the window are suppressed (and cooldown state is left untouched → real
anomalies after the window still trigger normally).
"""

from .scheduled_releases import (
    DEFAULT_SCHEDULED_WINDOWS,
    ScheduledReleaseWindow,
    is_in_scheduled_window,
)

__all__ = [
    "DEFAULT_SCHEDULED_WINDOWS",
    "ScheduledReleaseWindow",
    "is_in_scheduled_window",
]
