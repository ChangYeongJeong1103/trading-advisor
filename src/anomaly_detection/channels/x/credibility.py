"""
x/credibility.py — Per-account weight (0.0 ~ 1.0).

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §5.4 channel tier mapping):

  The importance of an X signal depends on who posted it.
  The detector multiplies by this weight when deciding channel score and tier.

  P5 baseline = hardcoded dict.
  P9 deep-dive = consider dynamic weights via regression on historical post → market move.

────────────────────────────────────────────────────────────────────────
Operational notes:

  · Handles whether they include the "@" prefix or not — handled identically (case-insensitive).
  · Unknown accounts get weight = 0.0 → detector ignores them → blocks noise.
  · "Fix": adding an account to the watchlist alone gives weight 0 (unregistered accounts are ignored).
    → New account weights must be added explicitly to this dict (intentional design).

────────────────────────────────────────────────────────────────────────
Plan: §8 P5 (account credibility weight in v0 detector)
"""

from __future__ import annotations


# Default weight dict — initial values from screening + operational experience.
# The 5 accounts named in plan §10 watchlist + future candidates.
_DEFAULT_WEIGHTS: dict[str, float] = {
    # 5 accounts from the watchlist (D10) — anomaly-detection bots
    "lookonchain":       0.90,
    "whalealert":        0.90,
    "polymarkethistory": 0.80,
    "unusual_whales":    0.85,
    "unusualwhales":     0.85,   # commonly used alias
    "bubblemaps":        0.75,
}


def _normalize(handle: str) -> str:
    """Strip @/whitespace + lowercase. Keeps comparisons consistent."""
    return handle.strip().lstrip("@").lower()


def account_weight(account_handle: str) -> float:
    """Account handle → weight in [0, 1]. Unknown accounts return 0.0.

    Args:
        account_handle: "@WhaleAlert" / "WhaleAlert" / "whalealert" all OK.

    Returns:
        float in [0, 1]. Unregistered handles return 0.0.
    """
    if not account_handle:
        return 0.0
    return _DEFAULT_WEIGHTS.get(_normalize(account_handle), 0.0)


def known_accounts() -> list[str]:
    """List of all (normalized) handles registered in the weight dict."""
    return list(_DEFAULT_WEIGHTS.keys())
