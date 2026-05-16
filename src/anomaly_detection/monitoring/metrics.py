"""
monitoring/metrics.py — In-memory metric counters / gauges / histograms.

────────────────────────────────────────────────────────────────────────
Role (architecture §7 NFR):
  Measure the daemon's latency / throughput / internal state.
  Source of truth for verifying three NFRs:
    - Fusion latency < 500ms P95     → histogram("fusion.latency_ms")
    - EMERGENCY push < 10s P95        → histogram("alert.emergency.latency_s")
    - per-channel uptime ≥ 99%        → counter("channel.<ch>.events_total")
                                       + counter("channel.<ch>.errors_total")

  v1 is in-memory only — Cloud Monitoring / Prometheus integration is in P9+.
  Reasons it's enough for now:
    - Dumping to stdout is auto-collected by Cloud Logging.
    - The heartbeat loop periodically prints a snapshot.
    - The most recent N minutes of latency can be embedded in the alert email body.

────────────────────────────────────────────────────────────────────────
Three metric types (following Prometheus naming convention):

  Counter   — monotonically increasing only. Never decreases until reset.
              e.g. "events_total", "errors_total", "alerts_sent_total"
  Gauge     — current value that can go up or down.
              e.g. "buffer_size", "active_connections", "cost_used_usd"
  Histogram — value distribution. Add samples via observe(), get
              p50/p95/p99 etc. via stats().
              e.g. "fusion_latency_ms", "request_duration_ms"

────────────────────────────────────────────────────────────────────────
Labels (multi-dimensional):

  Even with the same metric name, different label combinations are
  treated as separate data.
  registry.counter("events_total", {"channel": "polymarket"})  → distinct
  registry.counter("events_total", {"channel": "cme"})         → distinct

  Calls without labels are also OK (default = empty labels).

────────────────────────────────────────────────────────────────────────
Usage example:

  registry = MetricRegistry()

  # Counter — per event
  registry.counter("events_total", {"channel": "polymarket"}).inc()

  # Gauge — current buffer size
  registry.gauge("buffer_size", {"channel": "cme"}).set(42)

  # Histogram — measure latency (context manager is most convenient)
  with registry.time_block("fusion_latency_ms"):
      run_fusion(...)

  # Periodic dump
  print(registry.snapshot())

Architecture: §7 NFR
Plan: §3.3 Goal #2 (channel sanity check), Goal #5 (push delivery latency)
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

# Maximum sample count retained per histogram.
# 10_000 = 8 bytes × 10000 = 80KB per histogram. Even 10 histograms total 800KB.
# Small enough and still gives reasonable p99 accuracy.
_HISTOGRAM_MAX_SAMPLES = 10_000


# =====================================================================
# Helpers
# =====================================================================
def _label_key(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    """Convert a labels dict into a hashable key.

    sorted tuple of (k, v) → the same label combination always maps to the same key.
    """
    if not labels:
        return ()
    return tuple(sorted(labels.items()))


def _format_labels(labels_key: tuple[tuple[str, str], ...]) -> str:
    """Prometheus-style output — {channel="cme",symbol="CL"}"""
    if not labels_key:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in labels_key)
    return "{" + inner + "}"


# =====================================================================
# Counter
# =====================================================================
class Counter:
    """Monotonically increasing counter (events_total style)."""

    __slots__ = ("_value", "_lock")

    def __init__(self) -> None:
        self._value: float = 0.0
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0) -> None:
        """Increase by `amount`. Raises ValueError if amount < 0 (violates counter semantics)."""
        if amount < 0:
            raise ValueError(f"Counter.inc requires amount >= 0, got {amount}")
        with self._lock:
            self._value += amount

    def value(self) -> float:
        with self._lock:
            return self._value

    def reset(self) -> None:
        """Reset to 0. Generally not called (cumulative is the norm)."""
        with self._lock:
            self._value = 0.0


# =====================================================================
# Gauge
# =====================================================================
class Gauge:
    """Current value (can go up or down)."""

    __slots__ = ("_value", "_lock")

    def __init__(self) -> None:
        self._value: float = 0.0
        self._lock = threading.Lock()

    def set(self, value: float) -> None:
        with self._lock:
            self._value = float(value)

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value += amount

    def dec(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value -= amount

    def value(self) -> float:
        with self._lock:
            return self._value


# =====================================================================
# Histogram
# =====================================================================
@dataclass(frozen=True)
class HistogramStats:
    """Histogram snapshot. Frozen → safe for the caller to keep around."""

    count: int
    sum: float
    min: float
    max: float
    mean: float
    p50: float
    p95: float
    p99: float

    def to_dict(self) -> dict[str, float | int]:
        """JSON / log friendly. Key for P95 stays as 'p95'."""
        return {
            "count": self.count, "sum": self.sum,
            "min": self.min, "max": self.max, "mean": self.mean,
            "p50": self.p50, "p95": self.p95, "p99": self.p99,
        }


class Histogram:
    """Value distribution. Keeps only the last _HISTOGRAM_MAX_SAMPLES samples (memory cap).

    Auto-rotates via deque(maxlen=N) — oldest sample is dropped first.
    Theoretical downside: old outliers disappear. Acceptable for v1's
    latency-measurement purpose. If accurate percentiles become necessary,
    introduce something like t-digest in P9+.
    """

    __slots__ = ("_samples", "_count", "_sum", "_lock")

    def __init__(self) -> None:
        self._samples: deque[float] = deque(maxlen=_HISTOGRAM_MAX_SAMPLES)
        # count/sum stay accurate even when the cap drops samples — Prometheus-compatible
        self._count: int = 0
        self._sum: float = 0.0
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        """Add one value."""
        with self._lock:
            self._samples.append(float(value))
            self._count += 1
            self._sum += float(value)

    def stats(self) -> HistogramStats:
        """Statistics over the currently retained samples.

        Note:
            count / sum are cumulative (ignore the cap).
            min / max / mean / percentile are based only on samples within
            the cap — i.e., the most recent N.
        """
        with self._lock:
            samples = sorted(self._samples)
            count = self._count
            total = self._sum

        if not samples:
            return HistogramStats(count=count, sum=total, min=0.0, max=0.0,
                                  mean=0.0, p50=0.0, p95=0.0, p99=0.0)

        return HistogramStats(
            count=count,
            sum=total,
            min=samples[0],
            max=samples[-1],
            mean=statistics.fmean(samples),
            p50=_percentile(samples, 0.50),
            p95=_percentile(samples, 0.95),
            p99=_percentile(samples, 0.99),
        )


def _percentile(sorted_samples: list[float], p: float) -> float:
    """Assumes sorted_samples is already sorted. Nearest-rank method.

    Args:
        sorted_samples: ascending-sorted list. Raises ValueError if empty.
        p: 0.0 ~ 1.0.
    """
    if not sorted_samples:
        raise ValueError("empty samples")
    if p <= 0:
        return sorted_samples[0]
    if p >= 1:
        return sorted_samples[-1]
    # nearest-rank: ceil(p * N) - 1 (0-indexed)
    n = len(sorted_samples)
    rank = max(0, min(n - 1, int(p * n)))
    return sorted_samples[rank]


# =====================================================================
# MetricRegistry
# =====================================================================
@dataclass
class _MetricEntry:
    """Used to distinguish metric type during snapshot serialization."""

    type: str  # "counter" | "gauge" | "histogram"
    labels: tuple[tuple[str, str], ...]
    instance: Counter | Gauge | Histogram = field(repr=False)


class MetricRegistry:
    """Central store for all metrics. One instance per daemon at startup.

    - Calling with the same (name, labels) returns the existing instance (factory pattern).
    - Thread-safe.
    """

    def __init__(self) -> None:
        # (name, label_key) → _MetricEntry
        self._metrics: dict[tuple[str, tuple[tuple[str, str], ...]], _MetricEntry] = {}
        self._lock = threading.Lock()

    # ── factory methods ──
    def counter(self, name: str, labels: dict[str, str] | None = None) -> Counter:
        """Get or create a Counter by (name + label) combination."""
        return self._get_or_create(name, labels, "counter", Counter)  # type: ignore[return-value]

    def gauge(self, name: str, labels: dict[str, str] | None = None) -> Gauge:
        return self._get_or_create(name, labels, "gauge", Gauge)  # type: ignore[return-value]

    def histogram(self, name: str, labels: dict[str, str] | None = None) -> Histogram:
        return self._get_or_create(name, labels, "histogram", Histogram)  # type: ignore[return-value]

    def _get_or_create(
        self,
        name: str,
        labels: dict[str, str] | None,
        type_str: str,
        cls: type,
    ) -> Counter | Gauge | Histogram:
        key = (name, _label_key(labels))
        with self._lock:
            entry = self._metrics.get(key)
            if entry is None:
                entry = _MetricEntry(type=type_str, labels=key[1], instance=cls())
                self._metrics[key] = entry
            elif entry.type != type_str:
                # Same name + labels but a changed type is clearly a caller bug
                raise TypeError(
                    f"metric '{name}'{_format_labels(key[1])} already exists "
                    f"as {entry.type}, requested as {type_str}"
                )
        return entry.instance

    # ── timing helper ──
    @contextmanager
    def time_block(
        self, name: str, labels: dict[str, str] | None = None
    ) -> Iterator[None]:
        """Automatically record elapsed milliseconds into a Histogram.

        Example:
            with registry.time_block("fusion_latency_ms", {"channel": "cme"}):
                run_fusion(...)
        """
        hist = self.histogram(name, labels)
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            hist.observe(elapsed_ms)

    # ── snapshot ──
    def snapshot(self) -> dict[str, Any]:
        """Dump the current value of every metric.

        Returns:
            dict: {
                "counters":   {name+labels_str: value, ...},
                "gauges":     {name+labels_str: value, ...},
                "histograms": {name+labels_str: HistogramStats.to_dict(), ...}
            }
            JSON-serializable.
        """
        counters: dict[str, float] = {}
        gauges: dict[str, float] = {}
        histograms: dict[str, dict[str, float | int]] = {}

        with self._lock:
            entries = list(self._metrics.items())

        for (name, labels_key), entry in entries:
            display_name = name + _format_labels(labels_key)
            if entry.type == "counter":
                counters[display_name] = entry.instance.value()  # type: ignore[union-attr]
            elif entry.type == "gauge":
                gauges[display_name] = entry.instance.value()    # type: ignore[union-attr]
            elif entry.type == "histogram":
                histograms[display_name] = entry.instance.stats().to_dict()  # type: ignore[union-attr]

        return {
            "counters": counters,
            "gauges": gauges,
            "histograms": histograms,
        }

    def names(self) -> list[str]:
        """List of every registered (name + labels_str) — for debugging."""
        with self._lock:
            return [n + _format_labels(k) for (n, k) in self._metrics.keys()]

    def clear(self) -> None:
        """Remove all metrics — tests only. Do not call in production."""
        with self._lock:
            self._metrics.clear()


__all__ = [
    "Counter",
    "Gauge",
    "Histogram",
    "HistogramStats",
    "MetricRegistry",
]
