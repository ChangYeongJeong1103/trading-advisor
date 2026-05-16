"""
src/anomaly/monitoring/ — System health + cost monitoring.

Three responsibilities:
  health.py        → channel sanity check + heartbeat (architecture §6.4, §6.6)
  metrics.py       → operational metrics like latency / throughput / queue size
  cost_tracker.py  → D13 cost ceiling + kill-switch (wraps every external call)
"""
