"""
channels/hyperliquid/ — Hyperliquid perp DEX channel.

Source:
  - Hyperliquid Info API REST (POST /info, type="metaAndAssetCtxs")
    Public + no auth required + lenient rate limit.
  - WebSocket upgrade arrives in the P9 deep-dive (when trade-by-trade is needed).

Detection targets (v1 P3 baseline):
  - Sudden spike vs typical 5-min USD volume (z-score)
  P9 additions:
  - OI delta spike, funding rate jump, large wallet behavior, etc.

Implementation phase:
  P3 — collector + normalizer + features + v0 baseline detector (vol_only z-score)
  P9 — proper detector (OI surge, wallet clustering, etc.)
"""

from .channel import HyperliquidChannel  # noqa: F401
from .collector import HyperliquidCollector  # noqa: F401
from .detector import HyperliquidDetector, HyperliquidDetectorConfig  # noqa: F401
from .features import HyperliquidFeatures, compute_features  # noqa: F401
from .normalizer import normalize, normalize_asset_ctx  # noqa: F401
