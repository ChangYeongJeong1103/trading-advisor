"""
monitoring/cost_tracker.py — D13 cost ceiling + kill-switch.

────────────────────────────────────────────────────────────────────────
Role (architecture §6.7):

  Every external API/service call must go through this wrapper.
  Direct calls are forbidden (will be enforced by the P1 contract test).

  Three categories:
    payg          → Databento, OpenAI, etc. Subject to cap comparison + threshold alerts + kill-switch.
    subscription  → TradingView, Unusual Whales. Recorded in CSV only, no cap.
    free          → Polymarket API, snscrape, etc. Recorded with cost=0 (for activity visibility).

  Four effects (when recording PAYG):
    1) Append a line to cost_ledger.csv
    2) Update cost_summary_<YYYY-MM>.csv (per-day, per-tool aggregate)
    3) Threshold cross (10/20/40/60/80/100%) → alert callback
    4) Reaching 100% → set kill-switch flag + invoke kill callback

────────────────────────────────────────────────────────────────────────
Design decisions:

  Callback injection
    on_threshold_cross / on_kill_switch are injected from outside →
    cost_tracker doesn't import alerts/router (avoids a circular dep).
    In P7 the daemon orchestrator registers router.send_alert as the callback.

  Long-format CSV
    cost_summary uses (date, tool, type, cost, units) rows.
    More extensible than wide-format (dynamic columns like type_payg_databento) —
    no schema changes when adding a new tool, and Excel/pandas pivots are one line.

  Idempotency
    record_subscription_monthly skips by scanning the ledger if the same
    (tool, plan, YYYY-MM) is recorded twice (avoids double charging).
    (Protects against daemon restarts / duplicate cron firings.)

  Concurrency
    Uses threading.Lock so it's safe under both asyncio and a future thread
    executor. record()'s read-modify-write must be atomic so
    thresholds_crossed isn't fired twice.

────────────────────────────────────────────────────────────────────────
File layout (under data/anomaly/cost/):

  cost_ledger.csv               — append-only, one line per call (kept forever)
  cost_summary_<YYYY-MM>.csv    — monthly file, per-day per-tool aggregate (overwritten)
  _state.json                   — current month's payg cumulative + thresholds_crossed

Architecture: §6.7 Cost tracking & kill-switch
Plan: §11 D13
"""

from __future__ import annotations

import csv
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

# Exposed so external code can use it as a type hint
CostType = Literal["payg", "subscription", "free"]

# CSV column order — defined in one place to prevent typos
_LEDGER_COLUMNS = [
    "timestamp_utc",
    "type",
    "tool",
    "operation",
    "units",
    "unit_price_usd",
    "cost_usd",
    "note",
]

_SUMMARY_COLUMNS = [
    "date",          # YYYY-MM-DD (UTC)
    "tool",
    "type",
    "daily_cost_usd",
    "daily_units",
]


# =====================================================================
# Threshold cross event — payload passed to the alert callback
# =====================================================================
@dataclass(frozen=True)
class ThresholdCrossEvent:
    """Fires when a threshold is crossed. Used by alerts/router when building the email body.

    Attributes:
        ratio: one of 0.10 / 0.20 / 0.40 / 0.60 / 0.80 / 1.00.
        payg_cumulative_usd: current cumulative PAYG cost (USD).
        cap_usd: monthly PAYG cap (e.g. 1000.0).
        per_tool_payg: PAYG cumulative cost per tool. e.g. {"databento": 152.30, "openai": 47.10}.
        subscription_total_usd: For reference — total subscription this month.
        grand_total_usd: payg + subscription + free (free is 0).
        period: "YYYY-MM" string.
        is_kill_switch: True if ratio==1.00 (kill-switch fires simultaneously).
    """

    ratio: float
    payg_cumulative_usd: float
    cap_usd: float
    per_tool_payg: dict[str, float]
    subscription_total_usd: float
    grand_total_usd: float
    period: str
    is_kill_switch: bool


# =====================================================================
# CostTracker — single class that owns every D13 responsibility
# =====================================================================
class CostTracker:
    """Cost wrapper for every external service call (D13).

    Create one instance at daemon start and inject it into every channel / collector.

    Example:
        >>> tracker = CostTracker(
        ...     cost_dir=Path("data/anomaly/cost"),
        ...     cap_usd=1000.0,
        ...     thresholds=[0.10, 0.20, 0.40, 0.60, 0.80, 1.00],
        ... )
        >>> # After one Databento API call
        >>> tracker.record("databento", "replay_query", units=1000, unit_price=0.0001)
        0.1
    """

    # ── ctor ──
    def __init__(
        self,
        cost_dir: Path,
        cap_usd: float,
        thresholds: list[float],
        kill_switch_override: bool = False,
        on_threshold_cross: Callable[[ThresholdCrossEvent], None] | None = None,
        on_kill_switch: Callable[[], None] | None = None,
    ) -> None:
        """
        Args:
            cost_dir: data/anomaly/cost/ path. Created if missing.
            cap_usd: monthly PAYG cap (e.g. 1000.0). Pass config.cost.payg_cap_usd directly.
            thresholds: list of ratios at which to alert on cross. Auto-sorted if not pre-sorted.
            kill_switch_override: When True, the kill-switch is not engaged at 100% (manual ops mode).
            on_threshold_cross: callback to invoke on threshold cross. None → log only.
            on_kill_switch: callback to invoke on kill-switch. None → just sets the flag.
        """
        self._cost_dir = cost_dir
        self._cost_dir.mkdir(parents=True, exist_ok=True)

        self._cap_usd = float(cap_usd)
        # sorted + dedup → prevents firing the alarm twice for the same ratio
        self._thresholds = sorted(set(thresholds))
        self._kill_switch_override = kill_switch_override

        self._on_threshold_cross = on_threshold_cross
        self._on_kill_switch = on_kill_switch

        # Concurrency — protects record()'s read-modify-write
        self._lock = threading.Lock()

        # File paths
        self._ledger_path = cost_dir / "cost_ledger.csv"
        self._state_path = cost_dir / "_state.json"

        # State — load from disk (preserves thresholds_crossed across restarts)
        self._state = self._load_state()

        # PAYG kill-switch flag (channels reference this read-only)
        self._payg_killed: bool = self._state.get("payg_killed", False)

    # ─────────────────────────────────────────────────────────────────
    # Public API — read-only state queries
    # ─────────────────────────────────────────────────────────────────
    @property
    def is_payg_killed(self) -> bool:
        """When True, PAYG service calls are forbidden (channels check this right before each call)."""
        return self._payg_killed

    @property
    def payg_used_usd(self) -> float:
        """PAYG cumulative this month (USD). Excludes subscription / free."""
        # Reset if the month rolled over, then return
        self._maybe_reset_for_new_month()
        return float(self._state.get("payg_cumulative_usd", 0.0))

    @property
    def payg_remaining_usd(self) -> float:
        """Remaining PAYG budget this month. Negative if cap exceeded."""
        return self._cap_usd - self.payg_used_usd

    @property
    def cap_usd(self) -> float:
        return self._cap_usd

    # ─────────────────────────────────────────────────────────────────
    # Public API — three record functions
    # ─────────────────────────────────────────────────────────────────
    def record(
        self,
        tool: str,
        operation: str,
        units: float,
        unit_price: float,
        type: CostType = "payg",
        note: str = "",
    ) -> float:
        """The most central API — call after every external API call.

        Args:
            tool: service name. e.g. "databento", "openai", "tradingview".
            operation: kind of call. e.g. "replay_query", "vision_call", "monthly_plan".
            units: usage (call count, token count, GB, etc.).
            unit_price: USD per unit. e.g. $0.0001/row → 0.0001.
            type: "payg" | "subscription" | "free". Default payg.
            note: free-form note (for debugging).

        Returns:
            float: cost_usd of this record (= units * unit_price).

        Side effects:
            1) Append one line to cost_ledger.csv.
            2) Update the per-day aggregate in cost_summary_<YYYY-MM>.csv.
            3) [PAYG only] On threshold cross, invoke on_threshold_cross.
            4) [PAYG only] On reaching cap, engage kill-switch + invoke on_kill_switch.
        """
        cost_usd = float(units) * float(unit_price)
        now = datetime.now(timezone.utc)

        # ── inside the lock: keep all read-modify-write atomic ──
        with self._lock:
            self._maybe_reset_for_new_month()

            # 1) ledger append (for every type)
            self._append_ledger(now=now, type=type, tool=tool, operation=operation,
                                units=units, unit_price=unit_price, cost=cost_usd, note=note)

            # 2) summary update (for every type)
            self._update_summary(now=now, tool=tool, type=type, cost=cost_usd, units=units)

            # 3-4) PAYG only: cap / threshold / kill-switch handling
            if type == "payg":
                self._state["payg_cumulative_usd"] = self.payg_used_usd + cost_usd
                # per-tool cumulative — included in the alert email body
                per_tool: dict[str, float] = self._state.setdefault("per_tool_payg", {})
                per_tool[tool] = per_tool.get(tool, 0.0) + cost_usd
                self._save_state()

                # Threshold cross check
                self._check_thresholds_and_kill()

        return cost_usd

    def record_subscription_monthly(
        self,
        tool: str,
        plan: str,
        monthly_usd: float,
    ) -> bool:
        """Record monthly subscription cost idempotently.

        Calling twice with the same (tool, plan, YYYY-MM) does not double charge.
        At daemon start, init_monthly_subscriptions() calls this for every enabled sub.

        Args:
            tool: e.g. "tradingview", "unusualwhales".
            plan: e.g. "pro", "basic_200".
            monthly_usd: monthly fee (e.g. 30.00).

        Returns:
            bool: True if newly recorded; False if already recorded this month.
        """
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        with self._lock:
            if self._already_recorded_subscription(tool=tool, plan=plan, period=period):
                # Already recorded this month → skip (idempotent)
                return False

        # Release the lock and call record() (record() takes its own lock)
        self.record(
            tool=tool,
            operation=f"monthly:{plan}",
            units=1.0,
            unit_price=monthly_usd,
            type="subscription",
            note=f"monthly_{period}",
        )
        return True

    def record_free_daily(
        self,
        tool: str,
        operation: str,
        daily_count: int,
    ) -> None:
        """Record daily activity volume for a free service. cost=0; visible in the ledger only.

        Called by the daemon every day at 23:59:59 UTC (for activity visibility).

        Args:
            tool: e.g. "polymarket", "snscrape".
            operation: e.g. "ws_messages", "scrape_calls".
            daily_count: number of calls/messages that day.
        """
        self.record(
            tool=tool,
            operation=operation,
            units=float(daily_count),
            unit_price=0.0,
            type="free",
            note="daily_aggregate",
        )

    # ─────────────────────────────────────────────────────────────────
    # Internal — CSV writes
    # ─────────────────────────────────────────────────────────────────
    def _append_ledger(
        self,
        now: datetime,
        type: str,
        tool: str,
        operation: str,
        units: float,
        unit_price: float,
        cost: float,
        note: str,
    ) -> None:
        """Append one line to cost_ledger.csv. Auto-creates a header if the file is missing."""
        # If the file doesn't exist or is empty, write the header first
        write_header = not self._ledger_path.exists() or self._ledger_path.stat().st_size == 0

        with self._ledger_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(_LEDGER_COLUMNS)
            writer.writerow([
                now.isoformat(),
                type,
                tool,
                operation,
                f"{units:.6f}",
                f"{unit_price:.8f}",
                f"{cost:.6f}",
                note,
            ])

    def _update_summary(
        self,
        now: datetime,
        tool: str,
        type: str,
        cost: float,
        units: float,
    ) -> None:
        """Update the (date, tool, type) row in cost_summary_<YYYY-MM>.csv.

        Long-format: one row = (date, tool, type, daily_cost, daily_units).
        Read → modify → write the entire file on every record (small per-month
        size, so this is fine).
        """
        period = now.strftime("%Y-%m")
        date_str = now.strftime("%Y-%m-%d")
        summary_path = self._cost_dir / f"cost_summary_{period}.csv"

        # Read all existing rows
        rows: list[dict[str, str]] = []
        if summary_path.exists():
            with summary_path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)

        # Find a row matching (date, tool, type)
        key = (date_str, tool, type)
        found = False
        for row in rows:
            if (row["date"], row["tool"], row["type"]) == key:
                row["daily_cost_usd"] = f"{float(row['daily_cost_usd']) + cost:.6f}"
                row["daily_units"] = f"{float(row['daily_units']) + units:.6f}"
                found = True
                break

        if not found:
            rows.append({
                "date": date_str,
                "tool": tool,
                "type": type,
                "daily_cost_usd": f"{cost:.6f}",
                "daily_units": f"{units:.6f}",
            })

        # Rewrite the whole file (overwrite)
        with summary_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_SUMMARY_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

    # ─────────────────────────────────────────────────────────────────
    # Internal — threshold + kill-switch
    # ─────────────────────────────────────────────────────────────────
    def _check_thresholds_and_kill(self) -> None:
        """Check whether the current cumulative crossed a new threshold; on cross, fire callbacks.

        Caller: record() (within the lock). Do not call directly.
        """
        used = float(self._state.get("payg_cumulative_usd", 0.0))
        crossed: list[float] = self._state.get("thresholds_crossed", [])

        new_crossed: list[float] = []
        for ratio in self._thresholds:
            limit = self._cap_usd * ratio
            if used >= limit and ratio not in crossed:
                new_crossed.append(ratio)

        if not new_crossed:
            return

        # Fire for everything newly crossed (usually 1, but a large record may cross multiple)
        for ratio in new_crossed:
            crossed.append(ratio)
            is_kill = ratio >= 1.0

            event = ThresholdCrossEvent(
                ratio=ratio,
                payg_cumulative_usd=used,
                cap_usd=self._cap_usd,
                per_tool_payg=dict(self._state.get("per_tool_payg", {})),
                subscription_total_usd=self._compute_subscription_total(),
                grand_total_usd=self._compute_grand_total(),
                period=self._current_period(),
                is_kill_switch=is_kill and not self._kill_switch_override,
            )

            # callback (silent if not registered — normal before P7)
            if self._on_threshold_cross is not None:
                self._on_threshold_cross(event)

            # 100% reached + override not set → engage kill-switch
            if is_kill and not self._kill_switch_override:
                self._payg_killed = True
                self._state["payg_killed"] = True
                if self._on_kill_switch is not None:
                    self._on_kill_switch()

        self._state["thresholds_crossed"] = crossed
        self._save_state()

    # ─────────────────────────────────────────────────────────────────
    # Internal — state file
    # ─────────────────────────────────────────────────────────────────
    def _load_state(self) -> dict:
        """Load _state.json. Empty state if missing or parse fails.

        Schema:
          {
            "period": "YYYY-MM",
            "payg_cumulative_usd": float,
            "per_tool_payg": {tool: float, ...},
            "thresholds_crossed": [0.10, 0.20, ...],
            "payg_killed": bool,
          }
        """
        if not self._state_path.exists():
            return self._fresh_state()
        try:
            with self._state_path.open("r", encoding="utf-8") as f:
                state = json.load(f)
            # Reset if it's a different month
            if state.get("period") != self._current_period():
                return self._fresh_state()
            return state
        except (json.JSONDecodeError, OSError):
            # File corrupt → safely fall back to fresh (next record will write properly)
            return self._fresh_state()

    def _save_state(self) -> None:
        """Atomically write the current state to _state.json."""
        # write to temp → rename (atomic on POSIX)
        tmp_path = self._state_path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(self._state, f, indent=2)
        tmp_path.replace(self._state_path)

    def _fresh_state(self) -> dict:
        """Empty state for the start of a new month."""
        return {
            "period": self._current_period(),
            "payg_cumulative_usd": 0.0,
            "per_tool_payg": {},
            "thresholds_crossed": [],
            "payg_killed": False,
        }

    def _maybe_reset_for_new_month(self) -> None:
        """If the month rolled over, reset cumulative + thresholds + kill_switch.

        Subscriptions are recorded again at the start of a new month when
        init_monthly_subscriptions is called.
        """
        cur = self._current_period()
        if self._state.get("period") != cur:
            self._state = self._fresh_state()
            self._payg_killed = False
            self._save_state()

    # ─────────────────────────────────────────────────────────────────
    # Internal — helpers
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _current_period() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _already_recorded_subscription(self, tool: str, plan: str, period: str) -> bool:
        """Scan the ledger to check whether the same (tool, monthly:plan, period) sub row exists.

        Core to idempotency. Called every daemon start, so even at 100MB the
        scan happens once. 100MB CSV ≈ 1M rows ≈ a few seconds → acceptable.
        """
        if not self._ledger_path.exists():
            return False
        target_op = f"monthly:{plan}"
        target_note = f"monthly_{period}"
        with self._ledger_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if (
                    row.get("type") == "subscription"
                    and row.get("tool") == tool
                    and row.get("operation") == target_op
                    and row.get("note") == target_note
                ):
                    return True
        return False

    def _compute_subscription_total(self) -> float:
        """Total subscription this month. Scans the ledger (only when an alert fires).

        Not called on every record, so the cost is OK.
        """
        period = self._current_period()
        total = 0.0
        if not self._ledger_path.exists():
            return 0.0
        with self._ledger_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("type") != "subscription":
                    continue
                if row.get("note") != f"monthly_{period}":
                    continue
                try:
                    total += float(row["cost_usd"])
                except (KeyError, ValueError):
                    continue
        return total

    def _compute_grand_total(self) -> float:
        """Grand total this month = payg + subscription (free is 0)."""
        return self.payg_used_usd + self._compute_subscription_total()


# =====================================================================
# Module-level helper — called at daemon startup
# =====================================================================
def init_monthly_subscriptions(tracker: CostTracker, enabled_subs: list[dict]) -> int:
    """Idempotently record active subscriptions at daemon startup.

    Even with multiple daemon restarts in the same month, no double recording.

    Args:
        tracker: CostTracker instance.
        enabled_subs: list of {"tool": str, "plan": str, "monthly_usd": float}.
            e.g. [
              {"tool": "tradingview", "plan": "pro", "monthly_usd": 30.0},
              {"tool": "unusualwhales", "plan": "basic", "monthly_usd": 75.0},
            ]
            X API Basic should be included only when cfg.cost.x_api_basic_enabled=True.

    Returns:
        int: number of newly recorded subscriptions (0 means everything was already recorded).
    """
    new_count = 0
    for sub in enabled_subs:
        recorded = tracker.record_subscription_monthly(
            tool=sub["tool"],
            plan=sub["plan"],
            monthly_usd=sub["monthly_usd"],
        )
        if recorded:
            new_count += 1
    return new_count
