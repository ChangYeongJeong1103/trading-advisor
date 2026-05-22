"""
monitoring/weekly_digest.py — 매주 1회, 모든 component 헬스 요약 이메일.

──────────────────────────────────────────────────────────────────────
역할 (P12-F, 2026-05-22):

  Reactive alert (TruthSocialChannel 의 5-fail-in-a-row) 는 "이미 망가진"
  것만 잡는다. 어떤 채널이 _slowly_ 죽어가거나, 사용자가 잊고 있는 채널이
  의도와 다르게 동작하지 않는 경우는 reactive 로 잡히지 않는다.

  Weekly digest 는 그 silent gap 을 메운다:
    · 매주 월요일 06:00 PT (Pacific) 에 발송.
    · HealthRegistry.get_all() snapshot + 채널들의 last_event_ts 종합.
    · 단일 이메일 — 상태 표(table) 형태. 클릭 동작 / link 없음 (단방향 알림).

──────────────────────────────────────────────────────────────────────
설계:

  · asyncio task — daemon 의 다른 background loop 들과 같은 패턴.
  · cron 라이브러리 안 씀 — 60s 마다 "다음 발송 시각 지났나?" 체크하는 단순
    loop. 의존성 추가 없음 + edge case (DST, leap second) 가 zoneinfo 로 자동
    처리됨.
  · 디스크 영속성 없음 — daemon 재시작 시 다음 발송 시각은 "지금부터 다음 월요일
    06:00 PT" 로 다시 계산. 한 주 안에 재시작이 자주 일어나면 발송이 늦거나
    누락될 수 있지만, 그 정도 운영 사고는 reactive alert 가 잡고 있어야 정상.
  · 의도된 단순화: weekly cadence 가 cooldown(24h) 을 항상 초과 → cooldown 에
    걸려서 1주 안에 2번 발송되는 사고는 일어날 수 없음.

──────────────────────────────────────────────────────────────────────
운영 사용 예 (daemon entrypoint):

    digest_task = asyncio.create_task(
        weekly_digest_loop(
            registry=health_registry,
            dispatch_health_alert=alert_dispatcher.dispatch_health_alert,
            channels=channels,
            timezone_name="America/Los_Angeles",
        ),
        name="weekly-health-digest",
    )

──────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from ..alerts.health_alert import (
    ComponentSnapshot,
    HealthAlertKind,
    SystemHealthAlert,
)
from ..channels.base import Channel
from .health import ComponentHealth, HealthRegistry, HealthStatus

logger = logging.getLogger(__name__)

# 콜백 시그니처 — dispatcher.dispatch_health_alert 와 호환.
HealthAlertDispatch = Callable[[SystemHealthAlert], Awaitable[object]]

# 발송 cadence — 사용자 결정: 매주 월요일 06:00 PT.
_DEFAULT_TZ = "America/Los_Angeles"
_DEFAULT_WEEKDAY = 0   # 0 = Monday (Python datetime.weekday() 기준)
_DEFAULT_HOUR = 6
_DEFAULT_MINUTE = 0

# 체크 주기 — 매 60초 마다 "발송 시각 지났나?" 검사.
_TICK_SECONDS = 60.0


async def weekly_digest_loop(
    *,
    registry: HealthRegistry,
    dispatch_health_alert: HealthAlertDispatch,
    channels: dict[str, Channel] | None = None,
    timezone_name: str = _DEFAULT_TZ,
    weekday: int = _DEFAULT_WEEKDAY,
    hour: int = _DEFAULT_HOUR,
    minute: int = _DEFAULT_MINUTE,
    tick_seconds: float = _TICK_SECONDS,
) -> None:
    """Background task — 매주 한 번 health digest 이메일 발송.

    Args:
        registry: 모든 component 가 등록된 HealthRegistry.
        dispatch_health_alert: 보통 `ChannelAlertDispatcher.dispatch_health_alert`.
        channels: 채널 이름 → 채널 객체. snapshot 의 last_event_age_seconds
            보강에 사용 (registry 의 last_event_ts 만 쓰면 누락되는 채널이 있을
            수 있음). None 이면 registry 정보만 사용.
        timezone_name: IANA tz name. 기본 "America/Los_Angeles".
        weekday: 0=Mon … 6=Sun. 기본 0.
        hour / minute: 발송 시각 (해당 tz 기준).
        tick_seconds: 발송 시각 검사 주기. 운영 60s 가 적당. 테스트에선 짧게.

    Cancellation:
        asyncio.CancelledError 받으면 즉시 종료.
    """
    tz = ZoneInfo(timezone_name)
    next_at = _next_run_at(
        now=datetime.now(tz), weekday=weekday, hour=hour, minute=minute,
    )
    logger.info(
        "weekly_digest: started — next run at %s (%s)",
        next_at.isoformat(), timezone_name,
    )

    try:
        while True:
            now_local = datetime.now(tz)
            if now_local >= next_at:
                try:
                    await _run_once(
                        registry=registry,
                        dispatch_health_alert=dispatch_health_alert,
                        channels=channels or {},
                        sent_at=now_local,
                    )
                except Exception as e:  # noqa: BLE001 — alert path best-effort
                    logger.exception("weekly_digest: run failed — %s", e)

                # 다음 발송 시각 — 이번에 발송한 시각 기준 1주 뒤.
                next_at = _next_run_at(
                    now=now_local + timedelta(minutes=1),
                    weekday=weekday,
                    hour=hour,
                    minute=minute,
                )
                logger.info(
                    "weekly_digest: next run at %s (%s)",
                    next_at.isoformat(), timezone_name,
                )

            await asyncio.sleep(tick_seconds)
    except asyncio.CancelledError:
        logger.info("weekly_digest: loop cancelled — shutting down")
        raise


# ─────────────────────────────────────────────────────────────────────
# Internal helpers — pure 함수로 테스트 용이.
# ─────────────────────────────────────────────────────────────────────
def _next_run_at(
    *,
    now: datetime,
    weekday: int,
    hour: int,
    minute: int,
) -> datetime:
    """주어진 'now' (tz-aware) 다음에 오는 (weekday, hour:minute) 인스턴트.

    오늘이 해당 요일이고 현재 시각이 hour:minute 이전이면 "오늘" 반환. 아니면
    다음 주 해당 요일.
    """
    target_time = time(hour=hour, minute=minute)
    today_target = datetime.combine(now.date(), target_time, tzinfo=now.tzinfo)

    days_ahead = (weekday - now.weekday()) % 7
    if days_ahead == 0 and now < today_target:
        # 오늘이 발송 요일 + 아직 발송 시각 전.
        return today_target

    # 오늘 발송 요일 + 시각 지났거나, 다른 요일 — 다음 주기까지.
    if days_ahead == 0:
        days_ahead = 7
    return datetime.combine(
        now.date() + timedelta(days=days_ahead),
        target_time,
        tzinfo=now.tzinfo,
    )


async def _run_once(
    *,
    registry: HealthRegistry,
    dispatch_health_alert: HealthAlertDispatch,
    channels: dict[str, Channel],
    sent_at: datetime,
) -> None:
    """현재 시점 snapshot 을 모아 SystemHealthAlert 생성 + dispatch."""
    # 1) Registry 최신화. 다른 background 가 이미 돌리고 있을 수도 있지만
    #    digest 의 정확도를 위해 한 번 더 강제 실행.
    health_results = registry.run_all()

    # 2) ComponentSnapshot 으로 변환.
    snapshots = _build_snapshots(
        health_results=health_results,
        channels=channels,
        now_utc=datetime.now(timezone.utc),
    )

    # 3) Summary message — degraded/unhealthy 가 0 인지 한눈에.
    by_status = _count_by_status(snapshots)
    summary = (
        f"healthy={by_status.get('healthy', 0)}, "
        f"degraded={by_status.get('degraded', 0)}, "
        f"unhealthy={by_status.get('unhealthy', 0)}, "
        f"unknown={by_status.get('unknown', 0)}"
    )

    alert = SystemHealthAlert(
        kind=HealthAlertKind.WEEKLY_DIGEST,
        component="system.weekly_digest",
        message=f"Weekly health snapshot — {summary}.",
        since=sent_at,
        snapshots=tuple(snapshots),
    )

    await dispatch_health_alert(alert)
    logger.info("weekly_digest: dispatched — %s", summary)


def _build_snapshots(
    *,
    health_results: dict[str, ComponentHealth],
    channels: dict[str, Channel],
    now_utc: datetime,
) -> list[ComponentSnapshot]:
    """ComponentHealth + Channel.fetch_health() 를 합쳐 디스플레이용 리스트.

    Status 결정 우선순위 (가장 사용자에게 의미있는 신호 위주):
      1) channels dict 에 있고 fetch_health.status != 'unknown' → 그 값을 사용
         (ok → "healthy", fail → "unhealthy"). 사용자 정의:
         "real-time data collection 잘 되고 있으면 OK".
      2) HealthRegistry 에 result 가 있으면 그 status (storage/cost 등).
      3) 둘 다 없으면 'unknown' + 이유 note.
    """
    snapshots: list[ComponentSnapshot] = []
    seen_names: set[str] = set()

    # 1) Channel 부터 — fetch_health 우선 사용 (사용자가 가장 원하는 답).
    for channel_name, channel in sorted(channels.items()):
        key = f"channel.{channel_name}"
        fh = channel.fetch_health()
        last_event = getattr(channel, "last_event_ts", None)
        age = _event_age_seconds(last_event, now_utc)
        last_attempt_age = _event_age_seconds(fh.last_attempt_at, now_utc)

        # fetch_health.status → 사람이 읽는 health label.
        status_label = {
            "ok": HealthStatus.HEALTHY.value,
            "fail": HealthStatus.UNHEALTHY.value,
            "unknown": HealthStatus.UNKNOWN.value,
        }[fh.status]

        extra: dict[str, str] = {}
        if last_attempt_age is not None:
            extra["last_fetch_attempt_age_s"] = f"{last_attempt_age:.0f}"
        if fh.status == "fail" and fh.error:
            extra["error"] = fh.error[:200]
        if fh.status == "unknown":
            extra["note"] = "fetch tracking not yet wired"

        snapshots.append(ComponentSnapshot(
            name=key,
            status=status_label,
            last_event_age_seconds=age,
            extra=extra,
        ))
        seen_names.add(key)

    # 2) Registry 의 storage/cost 등 채널 외 component.
    for name in sorted(health_results.keys()):
        if name in seen_names:
            # 채널 entry 가 이미 추가된 경우 — fetch_health 가 우선.
            continue
        result = health_results[name]
        age = _event_age_seconds(result.last_event_ts, now_utc)
        snapshots.append(ComponentSnapshot(
            name=name,
            status=result.status.value,
            last_event_age_seconds=age,
            extra=_safe_extra(result),
        ))

    return snapshots


def _event_age_seconds(
    last_event_ts: datetime | None,
    now_utc: datetime,
) -> float | None:
    """last_event_ts → 경과 초. None 이면 None."""
    if last_event_ts is None:
        return None
    if last_event_ts.tzinfo is None:
        # 안전망 — registry 가 tz-naive datetime 을 돌려주면 UTC 로 간주.
        last_event_ts = last_event_ts.replace(tzinfo=timezone.utc)
    return max(0.0, (now_utc - last_event_ts).total_seconds())


def _safe_extra(result: ComponentHealth) -> dict[str, str]:
    """ComponentHealth.extra 중 표시해도 안전한 항목만 string 으로 추림.

    PII / 내부경로 노출 없도록 whitelist (path / db_path / age_seconds /
    reason / ratio / used_usd / cap_usd) 만 통과.
    """
    allowed = {
        "path", "db_path", "reason",
        "ratio", "used_usd", "cap_usd",
        "age_seconds",
    }
    out: dict[str, str] = {}
    if result.error:
        out["error"] = result.error[:200]
    for key, value in (result.extra or {}).items():
        if key not in allowed:
            continue
        try:
            out[key] = f"{value}"
        except Exception:  # noqa: BLE001 — extra 값이 이상하면 그냥 skip
            continue
    return out


def _count_by_status(snapshots: list[ComponentSnapshot]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for snap in snapshots:
        counts[snap.status] = counts.get(snap.status, 0) + 1
    return counts


__all__ = [
    "weekly_digest_loop",
]
