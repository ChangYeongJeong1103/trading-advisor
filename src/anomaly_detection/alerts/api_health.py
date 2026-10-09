"""
alerts/api_health.py — Shared consecutive-failure tracker + outage alerts for paid external APIs.

──────────────────────────────────────────────────────────────────────
Why this is shared (user decision 2026-10-09):

  This started as an LLM-only tracker (llm_health.py). Then on 2026-09-16
  the X API credits ran out, and 10 EMERGENCY X posts plus X channel
  collection failed silently for three weeks. Same root cause as the LLM
  outage:

    · Call sites "fail quietly and fall back" → correct, a single failure
      must not kill the daemon, but nobody notices a sustained outage.
    · For paid APIs, credit exhaustion is the most common outage, and once
      it happens every call fails until a human tops up.

  So the "N consecutive failures → 1 email, recovery → 1 email" logic lives
  in one shared class, and each API gets its own instance with a different
  component name and message text:
    · alerts/llm_health.py   → LLM_HEALTH   ("system.llm")
    · alerts/x_api_health.py → X_API_HEALTH ("system.x_api")

──────────────────────────────────────────────────────────────────────
Behavior:

  record_success()  → reset counter to 0. If a FAIL alert was sent,
                      dispatch one RECOVERY alert.
  record_failure()  → consecutive counter +1. On reaching threshold,
                      dispatch one REACTIVE_FAIL alert (no resend until
                      recovery).

  Alerts go through the health alert path (email-only). Ops outages are
  never broadcast to Telegram / X (existing P12-F principle).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .health_alert import HealthAlertKind, SystemHealthAlert

logger = logging.getLogger(__name__)

# Async callback signature compatible with dispatcher.dispatch_health_alert.
HealthAlertCallback = Callable[[SystemHealthAlert], Awaitable[object]]

# Default number of consecutive failures before alerting.
# 1 alerts on every transient timeout; too large delays the alert.
# 3 = absorbs transient blips while catching sustained outages (e.g. credits) fast.
_DEFAULT_FAIL_THRESHOLD = 3


class ApiHealthTracker:
    """Counts consecutive external API failures and emails ops at the threshold.

    Args:
        component: Component name stamped on the health alert (e.g. "system.llm").
            Also used as the weekly digest / cooldown key.
        fail_message: REACTIVE_FAIL email body. ``{count}`` is replaced with
            the consecutive failure count.
        recovery_message: REACTIVE_RECOVERY email body. ``{source}`` is
            replaced with the call site that confirmed recovery.
        banner_message: Optional alert-email banner text with ``{since}`` and
            ``{count}`` placeholders. Empty → banner_text() always returns "".
        fail_threshold: Consecutive failures before sending REACTIVE_FAIL.
        alert_cb: Async callback, usually dispatcher.dispatch_health_alert.
            None → count only, never alert (test / dev default).
    """

    def __init__(
        self,
        *,
        component: str,
        fail_message: str,
        recovery_message: str,
        banner_message: str = "",
        fail_threshold: int = _DEFAULT_FAIL_THRESHOLD,
        alert_cb: HealthAlertCallback | None = None,
    ) -> None:
        self._component = component
        self._fail_message = fail_message
        self._recovery_message = recovery_message
        self._banner_message = banner_message
        self._fail_threshold = max(1, int(fail_threshold))
        self._alert_cb = alert_cb

        self._consecutive_failures: int = 0
        self._first_fail_ts: datetime | None = None
        self._last_error: str = ""
        self._last_source: str = ""
        # Whether a FAIL alert has already been sent. True → no resend until recovery.
        self._fail_alert_active: bool = False

        # Cumulative stats — for the /health endpoint and debugging.
        self._total_success: int = 0
        self._total_failure: int = 0

        # record_* is called concurrently from different asyncio tasks (per-channel
        # loops). Serialize so overlapping increments can't send the same alert twice.
        self._lock = asyncio.Lock()

    # ────────────────────────────────────────────────────────────────
    # Configuration
    # ────────────────────────────────────────────────────────────────
    def set_alert_cb(self, alert_cb: HealthAlertCallback | None) -> None:
        """Inject the email dispatch callback at daemon startup."""
        self._alert_cb = alert_cb

    # ────────────────────────────────────────────────────────────────
    # State queries (renderer / health endpoint)
    # ────────────────────────────────────────────────────────────────
    @property
    def component(self) -> str:
        return self._component

    @property
    def degraded(self) -> bool:
        """Is the API currently failing repeatedly?

        True after even one consecutive failure (before the threshold) — the
        alert-email banner must explain why *this* alert lacks a score, so it
        reacts immediately (different purpose from the email threshold).
        """
        return self._consecutive_failures > 0

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def first_fail_ts(self) -> datetime | None:
        return self._first_fail_ts

    @property
    def stats(self) -> dict[str, int]:
        # Key prefix is the last token of the component (system.llm → llm_*).
        prefix = self._component.rsplit(".", 1)[-1]
        return {
            f"{prefix}_success_total": self._total_success,
            f"{prefix}_failure_total": self._total_failure,
            f"{prefix}_consecutive_failures": self._consecutive_failures,
        }

    def banner_text(self) -> str:
        """One-line explanation for the alert-email banner. Empty unless degraded."""
        if not self.degraded or not self._banner_message:
            return ""
        since = (
            self._first_fail_ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if self._first_fail_ts
            else "unknown"
        )
        return self._banner_message.format(
            since=since, count=self._consecutive_failures,
        )

    # ────────────────────────────────────────────────────────────────
    # Recording API — called by API call sites
    # ────────────────────────────────────────────────────────────────
    async def record_success(self, *, source: str) -> None:
        """One successful call. Dispatches a recovery alert if a FAIL alert was sent.

        Args:
            source: Call-site identifier (e.g. "alert_assessor").
        """
        async with self._lock:
            self._total_success += 1
            had_failures = self._consecutive_failures > 0
            was_alert_active = self._fail_alert_active

            self._consecutive_failures = 0
            self._first_fail_ts = None
            self._last_error = ""
            self._fail_alert_active = False

            # Recovery alert only if a FAIL alert was sent — transient failures
            # that stayed below the threshold don't need a recovery email.
            if not (had_failures and was_alert_active and self._alert_cb):
                return
            alert = SystemHealthAlert(
                kind=HealthAlertKind.REACTIVE_RECOVERY,
                component=self._component,
                message=self._recovery_message.format(source=source),
            )
            cb = self._alert_cb

        await self._safe_dispatch(cb, alert, label="RECOVERY")
        logger.info(
            "ApiHealthTracker[%s]: REACTIVE_RECOVERY dispatched (source=%s)",
            self._component, source,
        )

    async def record_failure(self, *, source: str, error: BaseException | str) -> None:
        """One failed call. Dispatches REACTIVE_FAIL on reaching the threshold.

        Args:
            source: Call-site identifier (e.g. "truth_social_scorer").
            error: Exception object or message. Included as the email detail.
        """
        async with self._lock:
            self._total_failure += 1
            self._consecutive_failures += 1
            self._last_source = source
            self._last_error = f"[{source}] {error}"
            if self._first_fail_ts is None:
                self._first_fail_ts = datetime.now(timezone.utc)

            if (
                self._consecutive_failures < self._fail_threshold
                or self._fail_alert_active
                or self._alert_cb is None
            ):
                return

            alert = SystemHealthAlert(
                kind=HealthAlertKind.REACTIVE_FAIL,
                component=self._component,
                message=self._fail_message.format(count=self._consecutive_failures),
                since=self._first_fail_ts or datetime.now(timezone.utc),
                consecutive_failures=self._consecutive_failures,
                detail=self._last_error,
            )
            cb = self._alert_cb
            # Set before dispatch so a failing callback can't trigger a retry storm.
            self._fail_alert_active = True

        await self._safe_dispatch(cb, alert, label="FAIL")
        logger.warning(
            "ApiHealthTracker[%s]: REACTIVE_FAIL dispatched (consecutive=%d, source=%s)",
            self._component, self._consecutive_failures, source,
        )

    # ────────────────────────────────────────────────────────────────
    # Internal
    # ────────────────────────────────────────────────────────────────
    async def _safe_dispatch(
        self,
        cb: HealthAlertCallback,
        alert: SystemHealthAlert,
        *,
        label: str,
    ) -> None:
        """Alert dispatch is best-effort — failures never block the caller."""
        try:
            await cb(alert)
        except Exception as exc:  # noqa: BLE001 — alert path safety net
            logger.exception(
                "ApiHealthTracker[%s]: alert_cb (%s) raised — %s",
                self._component, label, exc,
            )

    def reset(self) -> None:
        """Reset state — for tests / ops debugging."""
        self._consecutive_failures = 0
        self._first_fail_ts = None
        self._last_error = ""
        self._fail_alert_active = False


__all__ = [
    "ApiHealthTracker",
    "HealthAlertCallback",
]
