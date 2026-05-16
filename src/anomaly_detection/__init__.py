"""
src/anomaly/ — Anomaly detection subsystem (v1).

A new package fully separated from the legacy RAG (src/advisor.py, etc.).
A system that detects abnormal trading flow across 4 channels (Polymarket /
Hyperliquid / CME / X) and pushes email + Telegram notifications.

Detailed design: docs/anomaly-architecture-v1.md, docs/anomaly-upgrade-plan.md
"""
