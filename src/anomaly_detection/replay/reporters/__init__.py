"""
anomaly.replay.reporters — ReplayResult → human-facing artifacts.

Three reporters (all take only an in-memory ReplayResult as input):

  · csv_report.py  — summary.csv (1 row per event) + per_channel/<channel>.csv
  · yaml_report.py — per-event YAML (timeline + metrics + transitions)
  · plot.py        — matplotlib PNG (signal stem + tier step + announce vline)

Each reporter is a pure function — same ReplayResult → same output.
"""

from .csv_report import write_summary_row, write_per_channel_csv  # noqa: F401
from .yaml_report import write_yaml_report  # noqa: F401
from .plot import write_timeline_plot  # noqa: F401
from .channel_alerts import write_channel_alerts_csv  # noqa: F401
