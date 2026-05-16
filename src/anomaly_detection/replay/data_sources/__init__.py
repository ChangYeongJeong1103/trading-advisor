"""
anomaly.replay.data_sources — historical bar fetchers (one per channel).

In the v0 skeleton stage, only a placeholder (NullDataSource) is included.
Real sources are added incrementally (build order — docs/p10-replay-framework.md §7):

  · cme_databento.py    — wraps databento_client.fetch_historical_range
  · polymarket_graphql.py
  · x_snscrape.py
  · hyperliquid_info.py
  · fixture.py          — local parquet/JSON (HL old events only)
"""

from .base import HistoricalDataSource  # noqa: F401
from .null import NullDataSource  # noqa: F401
