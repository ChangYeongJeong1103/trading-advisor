"""
anomaly/entrypoints — collection of execution entry points (architecture §6.1).

Modules in this package are the actual "main()" entry points:

  anomaly_daemon  — 24/7 production daemon (Cloud Run / local)

  (Post-P9) one-off tools such as backfill and replay will also live here.

Each module can be run via `python -m anomaly.entrypoints.<name>`.
"""
