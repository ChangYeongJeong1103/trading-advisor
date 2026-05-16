"""
alerts/intensity.py — Intensity labels for ChannelSignal scores.

────────────────────────────────────────────────────────────────────────
Role (P9.3.P2.B):

  Even within the same RISK_OFF tier, scores can vary widely:
    · 0.75 — barely over the threshold (possibly noise)
    · 0.85 — clear anomaly
    · 0.95 — very strong signal (check immediately)

  So the user can quickly judge "is this really urgent?" from the alert
  body, the score is converted into a human-readable label.

────────────────────────────────────────────────────────────────────────
Thresholds (system-wide):

  RISK_OFF lower bound = 0.70
  EMERGENCY lower bound = 0.90 (approximately — based on fusion engine policy)

  Split the RISK_OFF range into three buckets:
    0.70 ~ 0.78   → "just over"   barely over the threshold
    0.78 ~ 0.88   → "well over"   clear anomaly
    0.88 ~ 1.00   → "way over"    very strong signal (includes EMERGENCY range)
"""

from __future__ import annotations

# RISK_OFF threshold (must match fusion engine policy)
_RISK_OFF_THRESHOLD: float = 0.70


def intensity_label(score: float) -> str:
    """Convert ChannelSignal.score (0~1) into a human-readable intensity label.

    Args:
        score: ChannelSignal.score, in the [0, 1] range.

    Returns:
        "just over" / "well over" / "way over"  (RISK_OFF+ range)
        or "below"  (under the RISK_OFF threshold — a score that normally
                     would not appear in an alert body, but handled here as
                     a safety net).
    """
    if score < _RISK_OFF_THRESHOLD:
        return "below"
    if score < 0.78:
        return "just over"
    if score < 0.88:
        return "well over"
    return "way over"


__all__ = ["intensity_label"]
