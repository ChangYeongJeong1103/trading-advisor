"""
channels/polymarket/ — Polymarket prediction market channel.

Source:
  - Polymarket CLOB WebSocket API   (real-time trade + order book)
  - Polymarket REST API             (market metadata, market list)
  - Dune Analytics                  (historical wallet behavior, backfill)

Detection targets (architecture §1, plan §3.2):
  - Sudden spike vs typical daily volume (z-score)
  - Probability jump in a short window (e.g. 0.30 → 0.65)
  - One wallet suddenly placing a large size
  - Suspected coordination patterns like "150+ accounts in one day"

Implementation phase:
  P2 — collector + normalizer + v0 baseline detector (z-score)
  P9 — proper detector (wallet clustering, jump test, etc.)
"""

from .channel import PolymarketChannel  # noqa: F401
from .collector import PolymarketCollector  # noqa: F401
from .detector import PolymarketDetector, PolymarketDetectorConfig  # noqa: F401
from .features import PolymarketFeatures, compute_features  # noqa: F401
from .normalizer import (  # noqa: F401
    normalize,
    normalize_market_snapshot,
    normalize_trade,
)
