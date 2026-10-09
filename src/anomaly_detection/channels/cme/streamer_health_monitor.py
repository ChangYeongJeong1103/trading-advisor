"""
channels/cme/streamer_health_monitor.py — CME Live Streamer health probe.

────────────────────────────────────────────────────────────────────────
Responsibilities (P9.3.P3 Step D):

  Periodically verify that the CMELiveStreamer running on the VM is alive.
  The streamer overwrites `_health/heartbeat.json` in GCS every 60 seconds,
  and this monitor reads that file every 5 minutes; "if ts is older than 6 minutes"
  is treated as an outage and sends email / telegram alerts.

  Design decisions (user-agreed):
    · Check interval      = 5 min (CHECK_INTERVAL_SEC = 300)
    · Outage threshold    = 6 min (STALE_THRESHOLD_SEC = 360)
    · Alert throttle      = 1 hour (ALERT_THROTTLE_SEC = 3600)
      → Avoids spamming on every check until the outage is resolved.
    · First check is preceded by a "WARMING UP" window (~30s after streamer boot).

  Error model:
    · HEARTBEAT_MISSING  : file does not exist at all (streamer never came up)
    · HEARTBEAT_STALE    : file exists but ts exceeds the threshold (session/network issues)
    · HEARTBEAT_MALFORMED: ts parse failure (bucket write permission issue, corruption)
    · GCS_ERROR          : list/get itself failed (IAM, network)

  False-positive guard (added 2026-06-16):
    · gcs_error means "the monitor could not read GCS" (NOT "the streamer is
      down") — semantically different from missing/stale. Transient SSL/network
      blips are common and self-heal, so we do NOT page on a single failure;
      a DOWN alert is sent only after N (GCS_ERROR_ALERT_AFTER) consecutive
      failures. missing/stale are real outage signals and still alert immediately.

  On recovery, send a "✅ recovered" alert once for end-to-end visibility.
    · The currently_down flag is in-memory, so it is lost when the Cloud Run
      instance restarts → in the past a DOWN could arrive with no RECOVERED.
      To prevent that, the down/ok state is persisted to GCS (STATE_BLOB) and
      restored on boot, so the first healthy check after a restart still emits
      the recovery alert once.
────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..cme.live_streamer import HEARTBEAT_BLOB
from ...alerts.format_time import format_pdt

logger = logging.getLogger(__name__)


# ─── Agreed timing constants ─────────────────────────────────────────
CHECK_INTERVAL_SEC: int = 300     # check heartbeat every 5 minutes
STALE_THRESHOLD_SEC: int = 360    # treat ≥ 6 minutes as outage
ALERT_THROTTLE_SEC: int = 3600    # suppress duplicate alerts for 1 hour
WARMUP_SEC: int = 60              # wait this long after boot before the first check

# Alert on gcs_error only after N consecutive failures (guards against single
# transient SSL/network blips). 5-min interval × 3 ≈ 15 min of GCS unreachability.
GCS_ERROR_ALERT_AFTER: int = 3

# GCS blob that persists the monitor's down/ok state — prevents a missed
# recovery alert when the Cloud Run instance restarts.
STATE_BLOB: str = "_health/monitor_state.json"


@dataclass(frozen=True)
class HealthMonitorConfig:
    """StreamerHealthMonitor configuration.

    Attributes:
        gcs_bucket: name of the bucket the streamer writes heartbeat to.
        check_interval_sec / stale_threshold_sec / alert_throttle_sec:
            overrides for the constants above (for testing/tuning).
    """
    gcs_bucket: str = "anomaly-cme-trades"
    check_interval_sec: int = CHECK_INTERVAL_SEC
    stale_threshold_sec: int = STALE_THRESHOLD_SEC
    alert_throttle_sec: int = ALERT_THROTTLE_SEC
    gcs_error_alert_after: int = GCS_ERROR_ALERT_AFTER


@dataclass
class _AlertState:
    """Recent emission state for outage alerts — used for throttle + recovery decisions."""
    last_alert_ts: Optional[datetime] = None
    last_reason: Optional[str] = None   # "missing" / "stale" / "malformed" / "gcs_error"
    currently_down: bool = False        # True → awaiting a recovery alert
    consecutive_gcs_errors: int = 0     # consecutive gcs_error counter (reset on read success)
    down_state_persisted: bool = False  # whether the current down state was saved to GCS


# =====================================================================
# StreamerHealthMonitor
# =====================================================================
class StreamerHealthMonitor:
    """Runs as a background task inside the daemon, inspecting heartbeat.json.

    Public API:
      · `await monitor.run()`   — loop forever until stop() is called.
      · `monitor.stop()`        — shutdown signal (graceful).
      · `monitor.snapshot()`    — dict (for debug / health endpoint).
    """

    def __init__(
        self,
        *,
        config: HealthMonitorConfig,
        email_renderer=None,       # object with .send(subject, html) (or None)
        telegram_renderer=None,    # object with .send(text) (or None)
        gcs_client=None,           # for tests; fake possible
    ) -> None:
        self._cfg = config
        self._email = email_renderer
        self._telegram = telegram_renderer
        self._gcs_client = gcs_client  # lazy init
        self._stop_event = asyncio.Event()
        self._state = _AlertState()
        # Last observations (for snapshot)
        self._last_check_ts: Optional[datetime] = None
        self._last_heartbeat_ts: Optional[datetime] = None
        self._last_reason: Optional[str] = None

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        """Forever loop — periodic checks + alerts until stop()."""
        try:
            await asyncio.sleep(WARMUP_SEC)  # give the streamer time to boot
            # Restart guard — restore whether the previous instance ended "down"
            # from GCS (if so, the next healthy check emits one ✅ recovered alert).
            await self._load_persisted_state()
            while not self._stop_event.is_set():
                try:
                    await self._check_once()
                except Exception as e:
                    # Exceptions in the check itself — log only, retry next cycle
                    logger.exception(
                        "StreamerHealthMonitor: unexpected error (%s) — retry next tick",
                        type(e).__name__,
                    )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._cfg.check_interval_sec,
                    )
                    return  # stop called
                except asyncio.TimeoutError:
                    continue  # normal — next check
        except asyncio.CancelledError:
            raise

    # ─────────────────────────────────────────────────────────────────
    # Core — one check
    # ─────────────────────────────────────────────────────────────────
    async def _check_once(self) -> None:
        """Read heartbeat once, judge it, alert if needed."""
        self._last_check_ts = datetime.now(timezone.utc)

        try:
            payload = await asyncio.to_thread(self._fetch_heartbeat)
        except Exception as e:
            # gcs_error = "the monitor could not read GCS" (NOT a streamer outage).
            # Transient SSL/network blips do not page on a single failure — only
            # after N consecutive failures.
            self._state.consecutive_gcs_errors += 1
            n = self._state.consecutive_gcs_errors
            threshold = self._cfg.gcs_error_alert_after
            logger.warning(
                "StreamerHealthMonitor: GCS access failed (%d/%d): %s (%s)",
                n, threshold, e, type(e).__name__,
            )
            if n < threshold:
                return  # below threshold — retry next cycle (false-positive guard)
            await self._report_problem(
                reason="gcs_error",
                detail=(
                    f"GCS access failed {n} times in a row "
                    f"(threshold={threshold}): {type(e).__name__}: {e}"
                ),
            )
            return

        # Reached here = GCS read succeeded → reset the gcs_error counter.
        self._state.consecutive_gcs_errors = 0

        if payload is None:
            await self._report_problem(
                reason="missing",
                detail="heartbeat.json not present in bucket (streamer did not boot, or permission issue)",
            )
            return

        ts_str = payload.get("ts")
        if not isinstance(ts_str, str):
            await self._report_problem(
                reason="malformed",
                detail=f"heartbeat.json missing/odd ts field: {ts_str!r}",
            )
            return

        try:
            hb_ts = datetime.fromisoformat(ts_str)
            if hb_ts.tzinfo is None:
                hb_ts = hb_ts.replace(tzinfo=timezone.utc)
        except ValueError as e:
            await self._report_problem(
                reason="malformed",
                detail=f"ts parse failed ({ts_str!r}): {e}",
            )
            return

        self._last_heartbeat_ts = hb_ts
        age_sec = (datetime.now(timezone.utc) - hb_ts).total_seconds()
        if age_sec > self._cfg.stale_threshold_sec:
            await self._report_problem(
                reason="stale",
                detail=(
                    f"heartbeat written {age_sec:.0f}s ago "
                    f"(threshold={self._cfg.stale_threshold_sec}s). "
                    f"streamer halted or network disconnected."
                ),
                extra_payload=payload,
            )
            return

        # healthy
        await self._report_ok(age_sec, payload)

    # ─────────────────────────────────────────────────────────────────
    # GCS helper
    # ─────────────────────────────────────────────────────────────────
    def _fetch_heartbeat(self) -> Optional[dict]:
        """Return parsed heartbeat dict, or None if blob missing."""
        client = self._get_client()
        bucket = client.bucket(self._cfg.gcs_bucket)
        blob = bucket.blob(HEARTBEAT_BLOB)
        if not blob.exists(client):
            return None
        raw = blob.download_as_bytes()
        return json.loads(raw)

    def _get_client(self):
        """Lazy init google.cloud.storage.Client."""
        if self._gcs_client is None:
            from google.cloud import storage  # noqa: import-outside-toplevel
            self._gcs_client = storage.Client()
        return self._gcs_client

    # ─────────────────────────────────────────────────────────────────
    # State persistence (GCS) — prevents a missed recovery alert on restart
    # ─────────────────────────────────────────────────────────────────
    async def _persist_state(self, *, down: bool, reason: Optional[str]) -> bool:
        """Best-effort save of the current down/ok state to GCS. Returns success.

        If GCS is unreachable the write may also fail → the exception is swallowed
        and False is returned. The caller retries on the next tick (while down).
        """
        try:
            await asyncio.to_thread(self._write_state_blob, down, reason)
            return True
        except Exception as e:
            logger.warning(
                "StreamerHealthMonitor: state persist failed: %s (%s)",
                e, type(e).__name__,
            )
            return False

    def _write_state_blob(self, down: bool, reason: Optional[str]) -> None:
        """Sync helper — overwrite monitor_state.json in GCS."""
        client = self._get_client()
        bucket = client.bucket(self._cfg.gcs_bucket)
        blob = bucket.blob(STATE_BLOB)
        payload = {
            "down": down,
            "reason": reason,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        blob.upload_from_string(
            json.dumps(payload, indent=2),
            content_type="application/json",
        )

    async def _load_persisted_state(self) -> None:
        """On boot, restore the previous monitor state — guarantees recovery
        across restarts.

        If the previous instance ended "down", restore currently_down=True so the
        next healthy check emits one ✅ recovered alert.
        """
        try:
            state = await asyncio.to_thread(self._read_state_blob)
        except Exception as e:
            logger.warning(
                "StreamerHealthMonitor: state load failed: %s (%s)",
                e, type(e).__name__,
            )
            return
        if state and state.get("down") is True:
            self._state.currently_down = True
            self._state.last_reason = state.get("reason")
            self._state.down_state_persisted = True
            self._last_reason = state.get("reason")
            logger.info(
                "StreamerHealthMonitor: restored DOWN state from GCS "
                "(reason=%s) — will emit recovered on next healthy check.",
                state.get("reason"),
            )

    def _read_state_blob(self) -> Optional[dict]:
        """Sync helper — read monitor_state.json (None if absent)."""
        client = self._get_client()
        bucket = client.bucket(self._cfg.gcs_bucket)
        blob = bucket.blob(STATE_BLOB)
        if not blob.exists(client):
            return None
        return json.loads(blob.download_as_bytes())

    # ─────────────────────────────────────────────────────────────────
    # Alert emit — throttle + recovery
    # ─────────────────────────────────────────────────────────────────
    async def _report_problem(
        self,
        *,
        reason: str,
        detail: str,
        extra_payload: Optional[dict] = None,
    ) -> None:
        """On detected outage → throttle check, then send alert and update state."""
        self._last_reason = reason
        now = datetime.now(timezone.utc)

        # Throttle: skip if the previous alert was within 1h (and same type)
        if (
            self._state.last_alert_ts is not None
            and self._state.last_reason == reason
            and (now - self._state.last_alert_ts).total_seconds()
            < self._cfg.alert_throttle_sec
        ):
            logger.info(
                "StreamerHealthMonitor: throttled (%s) — last alert %.0fs ago",
                reason, (now - self._state.last_alert_ts).total_seconds(),
            )
            self._state.currently_down = True
            # If the down state was not yet persisted (e.g. the GCS write also
            # failed on the first alert), retry every tick — once GCS is briefly
            # writable the state sticks, guaranteeing recovery across restarts.
            if not self._state.down_state_persisted:
                self._state.down_state_persisted = await self._persist_state(
                    down=True, reason=reason,
                )
            return

        subject = f"⚠️ [CME Streamer DOWN] {reason.upper()}"
        html = _render_down_html(
            reason=reason, detail=detail, now=now, extra=extra_payload,
        )
        text = _render_down_text(
            reason=reason, detail=detail, now=now,
        )

        await self._dispatch(subject=subject, html=html, text=text)
        self._state.last_alert_ts = now
        self._state.last_reason = reason
        self._state.currently_down = True
        # Persist the down state to GCS (best-effort) so a restart still recovers.
        self._state.down_state_persisted = await self._persist_state(
            down=True, reason=reason,
        )
        logger.error(
            "StreamerHealthMonitor: DOWN alert sent (reason=%s): %s",
            reason, detail,
        )

    async def _report_ok(self, age_sec: float, payload: dict) -> None:
        """Healthy observation — if we were previously down, emit one recovery alert."""
        if not self._state.currently_down:
            return  # normal OK — no alert
        now = datetime.now(timezone.utc)
        subject = "✅ [CME Streamer RECOVERED] heartbeat fresh"
        html = _render_recovered_html(
            age_sec=age_sec, now=now, payload=payload,
            prev_reason=self._state.last_reason or "unknown",
        )
        text = (
            f"✅ CME streamer recovered\n"
            f"Heartbeat age: {age_sec:.0f}s (threshold "
            f"{self._cfg.stale_threshold_sec}s)\n"
            f"Checked at: {format_pdt(now)}\n"
            f"Previous issue: {self._state.last_reason}"
        )
        await self._dispatch(subject=subject, html=html, text=text)
        self._state.currently_down = False
        self._state.last_reason = None
        self._state.down_state_persisted = False
        # Persist the recovered state so a later restart does not re-read a stale down.
        await self._persist_state(down=False, reason=None)
        logger.info("StreamerHealthMonitor: recovered — alert sent (age=%.0fs)", age_sec)

    async def _dispatch(
        self, *, subject: str, html: str, text: str,
    ) -> None:
        """Try email + telegram independently with separate exception handling."""
        if self._email is not None:
            try:
                await self._email.send(subject, html)
            except Exception as e:
                logger.exception("StreamerHealthMonitor: email send failed: %s", e)
        if self._telegram is not None:
            try:
                await self._telegram.send(text)
            except Exception as e:
                logger.exception("StreamerHealthMonitor: telegram send failed: %s", e)

    # ─────────────────────────────────────────────────────────────────
    # Snapshot — exposed via the health endpoint
    # ─────────────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        return {
            "last_check_ts": (
                self._last_check_ts.isoformat() if self._last_check_ts else None
            ),
            "last_heartbeat_ts": (
                self._last_heartbeat_ts.isoformat() if self._last_heartbeat_ts else None
            ),
            "currently_down": self._state.currently_down,
            "consecutive_gcs_errors": self._state.consecutive_gcs_errors,
            "down_state_persisted": self._state.down_state_persisted,
            "last_reason": self._last_reason,
            "last_alert_ts": (
                self._state.last_alert_ts.isoformat()
                if self._state.last_alert_ts else None
            ),
            "config": {
                "bucket": self._cfg.gcs_bucket,
                "check_interval_sec": self._cfg.check_interval_sec,
                "stale_threshold_sec": self._cfg.stale_threshold_sec,
                "alert_throttle_sec": self._cfg.alert_throttle_sec,
            },
        }


# =====================================================================
# Render helpers — minimal HTML / text (same pattern as renderer/email)
# =====================================================================
def _render_down_text(*, reason: str, detail: str, now: datetime) -> str:
    return (
        f"⚠️ CME Streamer DOWN\n"
        f"Reason: {reason}\n"
        f"Detail: {detail}\n"
        f"Checked at: {format_pdt(now)}\n"
        f"\n"
        f"Action: check the VM's systemd status\n"
        f"  gcloud compute ssh cme-streamer --zone=us-west1-b -- \\\n"
        f"    'sudo systemctl status cme-streamer'\n"
        f"\n"
        f"* Repeat alerts for the same failure type are suppressed for 1 hour."
    )


def _render_down_html(
    *, reason: str, detail: str, now: datetime, extra: Optional[dict],
) -> str:
    from html import escape
    extra_block = ""
    if extra:
        pretty = json.dumps(extra, indent=2)
        extra_block = (
            f"<h4 style='margin:12px 0 4px;'>Last heartbeat payload</h4>"
            f"<pre style='background:#f6f6f6;padding:8px;border-radius:4px;"
            f"font-size:12px;overflow:auto;'>{escape(pretty)}</pre>"
        )
    return f"""
<div style="font-family:system-ui,sans-serif;max-width:640px;">
  <div style="background:#c0392b;color:#fff;padding:12px 16px;border-radius:6px;">
    <div style="font-size:16px;font-weight:600;">
      ⚠️ CME Live Streamer DOWN — {escape(reason.upper())}
    </div>
    <div style="font-size:13px;opacity:.9;margin-top:4px;">
      Checked at {escape(format_pdt(now))}
    </div>
  </div>
  <div style="padding:12px 16px;">
    <p style="margin:0 0 8px;"><b>Detail:</b> {escape(detail)}</p>
    <p style="margin:0 0 8px;">
      <b>Action:</b>
      <code style="background:#f6f6f6;padding:2px 4px;border-radius:3px;">
        gcloud compute ssh cme-streamer --zone=us-west1-b --
        sudo systemctl status cme-streamer
      </code>
    </p>
    {extra_block}
    <p style="font-size:12px;color:#888;margin-top:16px;">
      Further alerts of the same type are suppressed for 1 hour.
      On recovery, a single ✅ Recovered alert will be sent.
    </p>
  </div>
</div>
"""


def _render_recovered_html(
    *, age_sec: float, now: datetime, payload: dict, prev_reason: str,
) -> str:
    from html import escape
    pretty = json.dumps(payload, indent=2)
    return f"""
<div style="font-family:system-ui,sans-serif;max-width:640px;">
  <div style="background:#27ae60;color:#fff;padding:12px 16px;border-radius:6px;">
    <div style="font-size:16px;font-weight:600;">
      ✅ CME Live Streamer RECOVERED
    </div>
    <div style="font-size:13px;opacity:.9;margin-top:4px;">
      Heartbeat fresh — checked at {escape(format_pdt(now))}
    </div>
  </div>
  <div style="padding:12px 16px;">
    <p style="margin:0 0 8px;">
      Heartbeat age: <b>{age_sec:.0f}s</b><br>
      Previous issue: <b>{escape(prev_reason)}</b>
    </p>
    <pre style="background:#f6f6f6;padding:8px;border-radius:4px;font-size:12px;overflow:auto;">
{escape(pretty)}</pre>
  </div>
</div>
"""


__all__ = [
    "StreamerHealthMonitor",
    "HealthMonitorConfig",
    "CHECK_INTERVAL_SEC",
    "STALE_THRESHOLD_SEC",
    "ALERT_THROTTLE_SEC",
    "GCS_ERROR_ALERT_AFTER",
    "STATE_BLOB",
]
