"""
monitoring/health.py — Health check for channels/storage/system + boot contract test.

────────────────────────────────────────────────────────────────────────
Role (architecture §6.4 Channel Sanity Check, §6.6 Failure Mode):

  At boot (contract test):
    Each component must prove "I'm healthy" once before the daemon starts.
    Storage directory writable, SQLite file openable, required secrets present, etc.
    On failure the caller (orchestrator) calls exit(1) — fail-fast principle.

  At runtime (heartbeat loop):
    Re-run every check every N seconds → log + update the registry.
    If a channel's last_event_ts is too old, mark UNHEALTHY → the fusion
    engine automatically sets weight=0 (graceful degrade).

────────────────────────────────────────────────────────────────────────
Design — registry pattern:

  Each component registers its own health check function with the registry.
  The health module does not depend on other modules (no reverse dependency,
  prevents circular imports).

  Channel registration example (when implementing P2 polymarket):
    registry.register(
        "channel.polymarket",
        make_staleness_check(
            name="channel.polymarket",
            get_last_event=lambda: polymarket_collector.last_event_ts,
            max_staleness_seconds=120,
        ),
    )

  Storage registration example:
    registry.register("storage.signal_store", make_sqlite_check("...", db_path))

────────────────────────────────────────────────────────────────────────
HealthStatus meanings:

  HEALTHY     — operating normally. Recent events are fresh.
  DEGRADED    — operating but with some issue (e.g. rate limit, slowness). Still included in fusion.
  UNHEALTHY   — non-functional. Fusion engine treats with weight 0 → graceful degrade.
  UNKNOWN     — not yet checked. Briefly exists right after boot.

Architecture: §6.4 Channel Sanity Check, §6.6 Failure Mode
Plan: §3.3 Goal #2 (channel sanity check)
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# =====================================================================
# Types
# =====================================================================
class HealthStatus(str, Enum):
    """Four health states. Auto-stringified during JSON serialization."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ComponentHealth:
    """Health result of one component (channel / storage / cost_tracker / etc.).

    Frozen → built once and replaced with a new instance. Safe for concurrent reads.
    """

    name: str
    status: HealthStatus
    last_check: datetime
    last_event_ts: datetime | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def is_alive(self) -> bool:
        """Can the fusion engine trust this component?"""
        return self.status in (HealthStatus.HEALTHY, HealthStatus.DEGRADED)


# Health check function signature — sync, no args, returns ComponentHealth
HealthCheckFn = Callable[[], ComponentHealth]


# =====================================================================
# Registry
# =====================================================================
class HealthRegistry:
    """Central store where components register their health checks.

    Create one instance at daemon start and inject into every component.
    Registration is reversed so the health module doesn't depend on other modules.
    """

    def __init__(self) -> None:
        self._checks: dict[str, HealthCheckFn] = {}
        self._results: dict[str, ComponentHealth] = {}
        self._lock = threading.Lock()

    def register(self, name: str, check_fn: HealthCheckFn) -> None:
        """Register a check function under a component name. Re-registering with the same name overwrites.

        Args:
            name: Dot-separated hierarchy recommended (e.g. "channel.polymarket", "storage.signal_store").
            check_fn: function that returns a ComponentHealth when called.
        """
        with self._lock:
            self._checks[name] = check_fn
            # Status right after registration is UNKNOWN (before run_all is called)
            self._results[name] = ComponentHealth(
                name=name,
                status=HealthStatus.UNKNOWN,
                last_check=_now_utc(),
            )

    def unregister(self, name: str) -> None:
        """Remove a component (for tests / dynamic reconfig)."""
        with self._lock:
            self._checks.pop(name, None)
            self._results.pop(name, None)

    def names(self) -> list[str]:
        """Names of every registered component."""
        with self._lock:
            return list(self._checks.keys())

    def run_all(self) -> dict[str, ComponentHealth]:
        """Run every registered check_fn once and cache the results.

        If check_fn raises, record UNHEALTHY (so the daemon survives).

        Returns:
            dict[str, ComponentHealth]: name → result. Snapshot at call time.
        """
        # snapshot — safe even if register() runs during execution
        with self._lock:
            checks_snapshot = dict(self._checks)

        new_results: dict[str, ComponentHealth] = {}
        for name, fn in checks_snapshot.items():
            try:
                result = fn()
                # If check_fn produced a result with the wrong name, force-correct it
                if result.name != name:
                    result = ComponentHealth(
                        name=name,
                        status=result.status,
                        last_check=result.last_check,
                        last_event_ts=result.last_event_ts,
                        error=result.error,
                        extra=result.extra,
                    )
            except Exception as e:
                result = ComponentHealth(
                    name=name,
                    status=HealthStatus.UNHEALTHY,
                    last_check=_now_utc(),
                    error=f"{type(e).__name__}: {e}",
                )
            new_results[name] = result

        with self._lock:
            self._results = new_results

        return new_results

    def get(self, name: str) -> ComponentHealth | None:
        """Most recent check result. UNKNOWN or None if run_all hasn't been called."""
        with self._lock:
            return self._results.get(name)

    def get_all(self) -> dict[str, ComponentHealth]:
        """Most recent result for every component (snapshot)."""
        with self._lock:
            return dict(self._results)

    def is_healthy(self, name: str) -> bool:
        """Convenience method — called per channel by the fusion engine.

        Returns:
            bool: True if HEALTHY or DEGRADED.
                False if not registered / UNHEALTHY / UNKNOWN.
        """
        result = self.get(name)
        return result is not None and result.is_alive()


# =====================================================================
# Contract test — once at boot
# =====================================================================
def run_contract_test(
    registry: HealthRegistry,
    required: list[str] | None = None,
) -> tuple[bool, dict[str, ComponentHealth]]:
    """Run every registered check once to decide whether the daemon may boot.

    Args:
        registry: HealthRegistry where every component is registered.
        required: Names of components that, if UNHEALTHY, prevent boot.
            None → every registered component is required.
            e.g. ["storage.signal_store", "storage.decision_store"] —
                a single dead channel does not block boot, but a dead storage does.

    Returns:
        (all_required_ok, results)
        all_required_ok: True if every required is HEALTHY/DEGRADED.
        results: every check result (including non-required).
    """
    results = registry.run_all()
    required_names = required if required is not None else list(results.keys())

    all_ok = True
    for name in required_names:
        if name not in results:
            logger.error("Contract test: required component '%s' not registered", name)
            all_ok = False
            continue
        if not results[name].is_alive():
            logger.error(
                "Contract test FAIL: %s status=%s error=%s",
                name, results[name].status.value, results[name].error,
            )
            all_ok = False
        else:
            logger.info(
                "Contract test ok: %s status=%s",
                name, results[name].status.value,
            )

    return all_ok, results


# =====================================================================
# Heartbeat loop — runs every N seconds at runtime
# =====================================================================
async def heartbeat_loop(
    registry: HealthRegistry,
    interval_seconds: int = 60,
    on_check: Callable[[dict[str, ComponentHealth]], None] | None = None,
) -> None:
    """Run as a task on the daemon's event loop. Loops forever, refreshing health.

    Args:
        registry: HealthRegistry.
        interval_seconds: check interval. v1 recommends 60s.
        on_check: optional callback receiving each cycle's results (e.g. record metrics).

    Cancellation:
        On asyncio.CancelledError, exits immediately. Used by the orchestrator's graceful shutdown.
    """
    logger.info("Heartbeat loop started — interval=%ds", interval_seconds)
    try:
        while True:
            results = registry.run_all()
            unhealthy = [n for n, r in results.items() if not r.is_alive()]
            if unhealthy:
                logger.warning("Unhealthy components: %s", unhealthy)
            else:
                logger.debug("All %d components alive", len(results))

            if on_check is not None:
                try:
                    on_check(results)
                except Exception as e:
                    # A failing callback must not kill the heartbeat
                    logger.exception("on_check callback raised: %s", e)

            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        logger.info("Heartbeat loop cancelled — shutting down")
        raise


# =====================================================================
# Built-in check factory — helpers for common patterns
# =====================================================================
def make_storage_dir_check(name: str, path: Path) -> HealthCheckFn:
    """check_fn that verifies a directory exists and is writable.

    Args:
        name: component name (used as-is when registering).
        path: directory to check.

    Returns:
        HealthCheckFn.
    """
    def check() -> ComponentHealth:
        now = _now_utc()
        if not path.exists():
            return ComponentHealth(
                name=name, status=HealthStatus.UNHEALTHY, last_check=now,
                error=f"directory does not exist: {path}",
            )
        if not path.is_dir():
            return ComponentHealth(
                name=name, status=HealthStatus.UNHEALTHY, last_check=now,
                error=f"not a directory: {path}",
            )
        # writable test — write and remove a small sentinel file
        sentinel = path / ".health_check.tmp"
        try:
            sentinel.write_text("ok")
            sentinel.unlink()
        except OSError as e:
            return ComponentHealth(
                name=name, status=HealthStatus.UNHEALTHY, last_check=now,
                error=f"directory not writable: {e}",
            )
        return ComponentHealth(
            name=name, status=HealthStatus.HEALTHY, last_check=now,
            extra={"path": str(path)},
        )

    return check


def make_sqlite_check(name: str, db_path: Path) -> HealthCheckFn:
    """Open a SQLite file and verify SELECT 1 works.

    The parent directory is assumed to be created by SignalStore/DecisionStore.
    """
    def check() -> ComponentHealth:
        now = _now_utc()
        try:
            conn = sqlite3.connect(str(db_path), timeout=2.0)
            try:
                row = conn.execute("SELECT 1").fetchone()
                if row != (1,):
                    return ComponentHealth(
                        name=name, status=HealthStatus.UNHEALTHY, last_check=now,
                        error="SELECT 1 returned unexpected result",
                    )
            finally:
                conn.close()
        except sqlite3.Error as e:
            return ComponentHealth(
                name=name, status=HealthStatus.UNHEALTHY, last_check=now,
                error=f"sqlite error: {e}",
            )
        return ComponentHealth(
            name=name, status=HealthStatus.HEALTHY, last_check=now,
            extra={"db_path": str(db_path)},
        )

    return check


def make_staleness_check(
    name: str,
    get_last_event: Callable[[], datetime | None],
    max_staleness_seconds: int,
    degraded_after_seconds: int | None = None,
) -> HealthCheckFn:
    """Decide staleness from a channel's last_event_ts.

    Args:
        name: component name.
        get_last_event: returns the channel's most recent event ts (None if none).
        max_staleness_seconds: UNHEALTHY when no new event for longer than this.
        degraded_after_seconds: DEGRADED when older than this (shorter than UNHEALTHY).
            None → no DEGRADED stage; jumps straight to UNHEALTHY.

    Returns:
        HealthCheckFn.

    Note:
        Right after boot, last_event may be None → treated as DEGRADED
        (no events yet ≠ dead).
    """
    def check() -> ComponentHealth:
        now = _now_utc()
        last = get_last_event()

        if last is None:
            # Right after boot — no events yet. Not dead, so DEGRADED.
            return ComponentHealth(
                name=name, status=HealthStatus.DEGRADED, last_check=now,
                last_event_ts=None,
                extra={"reason": "no events yet"},
            )

        age = (now - last).total_seconds()
        if age > max_staleness_seconds:
            return ComponentHealth(
                name=name, status=HealthStatus.UNHEALTHY, last_check=now,
                last_event_ts=last,
                error=f"stale: {age:.0f}s > {max_staleness_seconds}s",
                extra={"age_seconds": age},
            )

        if degraded_after_seconds is not None and age > degraded_after_seconds:
            return ComponentHealth(
                name=name, status=HealthStatus.DEGRADED, last_check=now,
                last_event_ts=last,
                extra={"age_seconds": age, "reason": "slow"},
            )

        return ComponentHealth(
            name=name, status=HealthStatus.HEALTHY, last_check=now,
            last_event_ts=last,
            extra={"age_seconds": age},
        )

    return check


def make_cost_killswitch_check(
    name: str,
    is_killed_fn: Callable[[], bool],
    used_usd_fn: Callable[[], float],
    cap_usd_fn: Callable[[], float],
) -> HealthCheckFn:
    """Verify the cost ceiling has not been exceeded. kill-switch engaged = UNHEALTHY.

    Args:
        name: usually "cost.payg".
        is_killed_fn: callable like cost_tracker.is_payg_killed.
        used_usd_fn: cost_tracker.payg_used_usd.
        cap_usd_fn: cost_tracker.cap_usd.
    """
    def check() -> ComponentHealth:
        now = _now_utc()
        used = used_usd_fn()
        cap = cap_usd_fn()
        ratio = used / cap if cap > 0 else 0.0
        extra = {"used_usd": used, "cap_usd": cap, "ratio": ratio}

        if is_killed_fn():
            return ComponentHealth(
                name=name, status=HealthStatus.UNHEALTHY, last_check=now,
                error=f"PAYG kill-switch active (used ${used:.2f} / cap ${cap:.2f})",
                extra=extra,
            )
        # Above 80% → DEGRADED — likely to hit cap soon
        if ratio >= 0.80:
            return ComponentHealth(
                name=name, status=HealthStatus.DEGRADED, last_check=now,
                extra=extra,
            )
        return ComponentHealth(
            name=name, status=HealthStatus.HEALTHY, last_check=now,
            extra=extra,
        )

    return check


# =====================================================================
# Helpers
# =====================================================================
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def format_summary(results: dict[str, ComponentHealth]) -> str:
    """Single-line, human-readable summary of health results. Used in logs."""
    by_status: dict[HealthStatus, list[str]] = {}
    for name, r in results.items():
        by_status.setdefault(r.status, []).append(name)
    parts = []
    # Output in severity order (UNHEALTHY first)
    for s in [HealthStatus.UNHEALTHY, HealthStatus.DEGRADED,
              HealthStatus.HEALTHY, HealthStatus.UNKNOWN]:
        if s in by_status:
            parts.append(f"{s.value}={len(by_status[s])}({','.join(by_status[s])})")
    return " | ".join(parts)


# Re-export for convenience
__all__ = [
    "HealthStatus",
    "ComponentHealth",
    "HealthCheckFn",
    "HealthRegistry",
    "run_contract_test",
    "heartbeat_loop",
    "make_storage_dir_check",
    "make_sqlite_check",
    "make_staleness_check",
    "make_cost_killswitch_check",
    "format_summary",
]


# Suppress unused-import warning (timedelta is reserved for future staleness factory extensions)
_ = timedelta
