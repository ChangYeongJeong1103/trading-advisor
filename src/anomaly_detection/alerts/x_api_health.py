"""
alerts/x_api_health.py — X API availability tracking + outage alerting.

──────────────────────────────────────────────────────────────────────
Why this exists (user decision 2026-10-09):

  On 2026-09-16 10:35 UTC the X API credits ran out and every X API call
  started failing with 402 (credits depleted). Failures only showed up as
  ERROR log lines with no alert, so nobody noticed for three weeks.
  Actual damage:

    · All 10 EMERGENCY auto-posts to X failed (CME / Hyperliquid)
    · X channel collection (watched accounts) failed at the user-lookup
      step → the channel was effectively blind

──────────────────────────────────────────────────────────────────────
Design:

  · Two places call the X API, and both spend the same developer-account
    credits:
      1) channels/x/collector.py       — read watched accounts (10-min polling)
      2) alerts/channel_dispatcher.py  — EMERGENCY alert posting
    → one shared tracker, for the same reason as llm_health.

  · The collector checks 2 accounts every 10 minutes, so a credit outage
    reaches the threshold (3) within 10–20 minutes.

  · Counting / alerting logic lives in alerts/api_health.py
    (ApiHealthTracker). This module only defines the X component name
    and message text.
"""

from __future__ import annotations

from .api_health import ApiHealthTracker

# Component name shown in health alerts; also the cooldown / digest key.
X_API_COMPONENT_NAME = "system.x_api"

# 3 = absorbs a transient failure in one cycle (2 accounts), detects on the second.
_X_API_FAIL_THRESHOLD = 3

# Shared singleton — call sites import it and only call record_*;
# only the daemon calls set_alert_cb() at startup.
X_API_HEALTH = ApiHealthTracker(
    component=X_API_COMPONENT_NAME,
    fail_message=(
        "X API calls failed {count} times in a row. "
        "EMERGENCY X posting is failing and the X channel "
        "(watched accounts) is effectively blind. "
        "Check the X API credit balance and API keys first."
    ),
    recovery_message=(
        "X API calls recovered — {source} succeeded. "
        "EMERGENCY X posting and X channel collection are live again."
    ),
    fail_threshold=_X_API_FAIL_THRESHOLD,
)


__all__ = [
    "X_API_HEALTH",
    "X_API_COMPONENT_NAME",
]
