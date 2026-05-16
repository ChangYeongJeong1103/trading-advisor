"""
src/anomaly/storage/ — Persistence layer.

D3 (LOCKED) — two storage technologies mixed (architecture §4.2):
  Parquet  → raw_store, feature_store
            (large time series, append-only, columnar compression)
  SQLite   → signal_store, decision_store
            (small, query-heavy, audit/replay)

D12 deployment:
  Local dev → plain filesystem
  Cloud Run → SQLite = mounted volume, Parquet = GCS FUSE mount

Retention (architecture §4.2):
  raw_store      : 7-day rolling (large)
  feature_store  : 30-day rolling
  signal_store   : unlimited (audit)
  decision_store : unlimited (audit)
"""
