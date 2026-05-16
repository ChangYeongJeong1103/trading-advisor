"""
anomaly.replay.channel_replays — per-channel replay implementations.

Each file is a ChannelReplay implementation (runner.py Protocol) for one channel.
It pairs a raw bar fetcher from data_sources/ with that channel's production
feature_engine + detector, producing step(sim_clock) → ChannelSignal.

v0.1 (this commit): cme.py + polymarket.py.
v0.2 (P10.4): hyperliquid.py (CSV-based, free path).
Coming later: x.py.
"""

from .cme import CmeChannelReplay  # noqa: F401
from .hyperliquid import HyperliquidChannelReplay  # noqa: F401
from .polymarket import PolymarketChannelReplay  # noqa: F401
