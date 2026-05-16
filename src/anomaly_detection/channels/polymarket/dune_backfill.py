"""
polymarket/dune_backfill.py — Historical backfill via Dune Analytics.

Responsibilities:
  - Fetch Polymarket on-chain trade history via Dune SQL queries
  - Build wallet behavior baselines (typical size, typical frequency)
  - Provide a reference distribution for the detector's z-score / percentile

Runs separately from the real-time collector — daily once only (cron or manual).

Implementation phase: late P2, or P9 (once the detector needs a baseline)
"""


async def backfill(symbol: str, days: int = 30) -> None:
    """Fetch the past N days of historical trades from Dune → save to feature_store.

    Args:
        symbol: Polymarket market identifier.
        days: backfill window (default 30 days).
    """
    raise NotImplementedError("Implemented in late P2")
