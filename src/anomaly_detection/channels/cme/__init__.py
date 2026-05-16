"""
channels/cme/ — CME futures channel.

P4 walking-skeleton (current):
  · MockCMECollector  — synthetic trades (inject spikes via env CME_MOCK_SPIKE)
  · normalize_mock_trade
  · CMEFeatures       — 5min vol z-score + 1min price jump (%)
  · CMEDetector       — vol_z + price_jump tier rules
  · CMEChannel        — wiring + 5s polling loop

P9 deep-dive (planned):
  · DatabentoClient (real tick data)
  · TradingViewAdapter (webhook)
  · UnusualWhalesAdapter (options flow)
  → all merged into the same NormalizedEvent, reusing the same features/detector.

D7 (LOCKED): real sources start historical-only (safe PAYG cost).
"""

# real source stubs (P9)
from .databento_client import DatabentoClient  # noqa: F401
from .tradingview_adapter import TradingViewAdapter  # noqa: F401
from .unusualwhales_adapter import UnusualWhalesAdapter  # noqa: F401

# P4 walking-skeleton main components
from .mock_collector import MockCMECollector  # noqa: F401
from .normalizer import normalize_mock_trade  # noqa: F401
from .features import CMEFeatures, compute_features  # noqa: F401
from .detector import CMEDetector, CMEDetectorConfig  # noqa: F401
from .channel import CMEChannel  # noqa: F401
