"""
alerts/health_alert.py — System health alerts (email-only, separate path).

──────────────────────────────────────────────────────────────────────
역할 (P12-F, 2026-05-22):

  시장 신호(ChannelSignal → Tier) 와 운영 헬스(SystemHealthAlert) 를
  의도적으로 분리한다. 같은 alert pipeline 을 통과시키면 운영 헬스
  알림이 EMERGENCY 로 분류되어 Telegram / X 로 broadcast 되어 버린다.
  운영 약점을 공개 SNS 에 노출하는 건 보안/신호품질 양쪽에서 나쁨.

  → SystemHealthAlert 는 자체 dispatch path 로 **이메일만** 발송.
     Telegram / X 는 절대 거치지 않음 (channel_dispatcher 의 X/Telegram
     분기에 도달조차 안 함).

──────────────────────────────────────────────────────────────────────
구성:

  · SystemHealthAlert  — dataclass. 발송할 사건 1건.
  · HealthAlertKind    — REACTIVE_FAIL / REACTIVE_RECOVERY / WEEKLY_DIGEST
  · render_health_email()
      pure 함수. SystemHealthAlert + 옵션 context → RenderedEmail.
      기존 channel_email.send_email() 이 그대로 발송 가능.
  · HealthAlertCooldown
      메모리 내 (channel, kind) → last_sent_ts dict. 동일 사건 24h 1회.

──────────────────────────────────────────────────────────────────────
운영 시나리오:

  1) TruthSocialChannel 이 GCS read 5번 연속 fail
     → HealthMonitor 가 SystemHealthAlert(kind=REACTIVE_FAIL) 생성
     → ChannelAlertDispatcher.dispatch_health_alert() → 이메일 1통
     → 같은 alert 는 24h 동안 다시 안 옴 (cooldown)
     → recovery 되면 REACTIVE_RECOVERY 1통

  2) 매주 월요일 06:00 PT
     → WeeklyHealthDigest task 가 모든 채널 상태 종합
     → SystemHealthAlert(kind=WEEKLY_DIGEST, channels=[...]) 생성
     → 이메일 1통 (요약 표)

──────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import html as html_lib
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from .renderer.channel_email import RenderedEmail

logger = logging.getLogger(__name__)


class HealthAlertKind(str, Enum):
    """Health alert 의 3가지 종류 — subject/색상/cooldown key 결정."""

    REACTIVE_FAIL = "reactive_fail"          # 연속 fail 감지 — 즉시 발송
    REACTIVE_RECOVERY = "reactive_recovery"  # 복구 — 1회 발송
    WEEKLY_DIGEST = "weekly_digest"          # 매주 월요일 06:00 PT


@dataclass(frozen=True)
class ComponentSnapshot:
    """Weekly digest 의 한 줄 — 한 채널/storage 의 최신 상태."""

    name: str                       # 예: "channel.truth_social"
    status: str                     # "healthy" / "degraded" / "unhealthy" / "unknown"
    last_event_age_seconds: float | None = None  # 가장 최근 event 까지 경과초
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SystemHealthAlert:
    """발송할 운영 헬스 사건 1건. dispatcher 가 받아 이메일로 변환.

    Attributes:
        kind: REACTIVE_FAIL / REACTIVE_RECOVERY / WEEKLY_DIGEST.
        component: 영향 받은 component 이름 (예: "channel.truth_social").
            WEEKLY_DIGEST 면 "system" 같이 generic.
        message: 사람이 읽을 1줄 설명.
        since: 사건 시작 시각 (REACTIVE_FAIL 의 first-fail 시각 등).
        consecutive_failures: REACTIVE_FAIL 일 때 연속 실패 횟수.
        snapshots: WEEKLY_DIGEST 일 때 채널별 ComponentSnapshot 리스트.
        detail: HTML 본문 보강용 자유 텍스트 (선택).
    """
    kind: HealthAlertKind
    component: str
    message: str
    since: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    consecutive_failures: int = 0
    snapshots: tuple[ComponentSnapshot, ...] = ()
    detail: str = ""

    def cooldown_key(self) -> tuple[str, str]:
        """동일 사건 dedup 용. (component, kind) 단일 keypair.

        REACTIVE_FAIL 과 REACTIVE_RECOVERY 는 의도적으로 다른 key —
        recovery 가 같은 cooldown 안에서도 발송되어야 함.
        """
        return (self.component, self.kind.value)


class HealthAlertCooldown:
    """메모리 내 24h cooldown — 동일 (component, kind) 가 폭주하지 않도록.

    fail/recovery 패턴이 짧은 시간에 반복되면 cooldown 안에서 후속이 묶임.
    daemon 재시작 시 state 가 0 으로 reset 되는 건 의도된 동작
    (재시작 = 사용자가 이미 인지하고 개입한 상황으로 간주).
    """

    def __init__(self, *, default_window: timedelta = timedelta(hours=24)) -> None:
        self._window = default_window
        self._last_sent: dict[tuple[str, str], datetime] = {}
        self._lock = threading.Lock()

    def should_send(self, alert: SystemHealthAlert, *, now: datetime | None = None) -> bool:
        """현재 시각에 alert 를 발송해야 하는지.

        Returns:
            True  — cooldown 통과 (발송 OK). 통과한 시점에 last_sent 가 갱신됨.
            False — cooldown 안 — skip.
        """
        now = now or datetime.now(timezone.utc)
        key = alert.cooldown_key()
        with self._lock:
            last = self._last_sent.get(key)
            if last is not None and (now - last) < self._window:
                return False
            self._last_sent[key] = now
        return True

    def reset(self, component: str | None = None) -> None:
        """테스트 / 운영 디버깅 용 reset."""
        with self._lock:
            if component is None:
                self._last_sent.clear()
            else:
                self._last_sent = {
                    k: v for k, v in self._last_sent.items()
                    if k[0] != component
                }


# ─────────────────────────────────────────────────────────────────────
# Render — pure 함수. 테스트 쉬움.
# ─────────────────────────────────────────────────────────────────────
_KIND_TO_EMOJI: dict[HealthAlertKind, str] = {
    HealthAlertKind.REACTIVE_FAIL: "🛠️",
    HealthAlertKind.REACTIVE_RECOVERY: "✅",
    HealthAlertKind.WEEKLY_DIGEST: "📋",
}

_KIND_TO_LABEL: dict[HealthAlertKind, str] = {
    HealthAlertKind.REACTIVE_FAIL: "System Health — Component failing",
    HealthAlertKind.REACTIVE_RECOVERY: "System Health — Component recovered",
    HealthAlertKind.WEEKLY_DIGEST: "System Health — Weekly digest",
}


def render_health_email(alert: SystemHealthAlert) -> RenderedEmail:
    """SystemHealthAlert → 단순 HTML email (PNG 첨부 없음).

    Subject 는 시장 alert 와 구분되도록 "[Health]" prefix 를 박는다 — 사용자가
    inbox 에서 한 눈에 분리 가능. 본문은 plain HTML (style minimal).
    """
    emoji = _KIND_TO_EMOJI[alert.kind]
    label = _KIND_TO_LABEL[alert.kind]
    subject = f"{emoji} [Health] {label} — {alert.component}"

    when_iso = alert.since.astimezone(timezone.utc).isoformat(timespec="seconds")

    # 본문 — kind 마다 약간 다른 layout.
    if alert.kind == HealthAlertKind.WEEKLY_DIGEST:
        body_html = _render_weekly_body(alert)
    else:
        body_html = _render_reactive_body(alert)

    html_doc = f"""<!doctype html>
<html><body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color: #222;">
  <div style="max-width: 640px; margin: 0 auto; padding: 16px;">
    <h2 style="margin: 0 0 12px 0;">{emoji} {html_lib.escape(label)}</h2>
    <p style="color: #666; margin: 0 0 16px 0;">
      Component: <code>{html_lib.escape(alert.component)}</code><br/>
      Since (UTC): <code>{html_lib.escape(when_iso)}</code>
    </p>
    {body_html}
    <hr style="margin: 24px 0; border: none; border-top: 1px solid #eee;"/>
    <p style="font-size: 12px; color: #888;">
      Sent by anomaly-daemon health monitor (separate path from market alerts).
      This email is never broadcast to Telegram or X.
    </p>
  </div>
</body></html>"""

    return RenderedEmail(
        subject=subject,
        html=html_doc,
        inline_images=[],
        alert_id=f"health-{alert.component}-{alert.kind.value}-{int(alert.since.timestamp())}",
    )


def _render_reactive_body(alert: SystemHealthAlert) -> str:
    """REACTIVE_FAIL / REACTIVE_RECOVERY 용 본문."""
    fail_block = ""
    if alert.consecutive_failures > 0:
        fail_block = f"""
        <p>
          <strong>Consecutive failures:</strong> {alert.consecutive_failures}
        </p>"""

    detail_block = ""
    if alert.detail:
        detail_block = (
            f"<pre style='background:#f7f7f7;padding:12px;border-radius:6px;"
            f"font-size:12px;overflow-x:auto;'>"
            f"{html_lib.escape(alert.detail)}</pre>"
        )

    return f"""
    <p style="font-size: 15px; line-height: 1.5;">
      {html_lib.escape(alert.message)}
    </p>
    {fail_block}
    {detail_block}
    """


def _render_weekly_body(alert: SystemHealthAlert) -> str:
    """WEEKLY_DIGEST 용 본문 — 채널별 상태표."""
    if not alert.snapshots:
        return ("<p>No component snapshots recorded for this digest.</p>"
                "<p>(Likely a first-run or registry empty.)</p>")

    rows = []
    for snap in alert.snapshots:
        status_color = _status_color(snap.status)
        age = (
            f"{snap.last_event_age_seconds:.0f}s"
            if snap.last_event_age_seconds is not None
            else "—"
        )
        extra_text = ""
        if snap.extra:
            extra_text = ", ".join(
                f"{k}={v}" for k, v in snap.extra.items()
                if not k.startswith("_")
            )
        rows.append(
            f"<tr>"
            f"<td style='padding:6px 10px;'><code>{html_lib.escape(snap.name)}</code></td>"
            f"<td style='padding:6px 10px;color:{status_color};'>"
            f"<strong>{html_lib.escape(snap.status)}</strong></td>"
            f"<td style='padding:6px 10px;'>{html_lib.escape(age)}</td>"
            f"<td style='padding:6px 10px;font-size:12px;color:#666;'>"
            f"{html_lib.escape(extra_text)}</td>"
            f"</tr>"
        )

    table = (
        "<table style='border-collapse:collapse;width:100%;font-size:14px;'>"
        "<thead>"
        "<tr style='background:#f0f0f0;text-align:left;'>"
        "<th style='padding:8px 10px;'>Component</th>"
        "<th style='padding:8px 10px;'>Status</th>"
        "<th style='padding:8px 10px;'>Last event age</th>"
        "<th style='padding:8px 10px;'>Extra</th>"
        "</tr>"
        "</thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )

    intro = (
        "<p>Weekly health digest — snapshot of every registered component.</p>"
    )
    return intro + table


def _status_color(status: str) -> str:
    """HealthStatus value → HTML hex color."""
    return {
        "healthy": "#1a7f37",
        "degraded": "#bf8700",
        "unhealthy": "#cf222e",
        "unknown": "#6e7781",
    }.get(status.lower(), "#222")


__all__ = [
    "ComponentSnapshot",
    "HealthAlertCooldown",
    "HealthAlertKind",
    "SystemHealthAlert",
    "render_health_email",
]
