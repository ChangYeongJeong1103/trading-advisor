"""
src/anomaly/alerts/renderer/ — Per-channel message rendering.

email.py    → HTML email body (subject + per-channel breakdown + 30-min timeline + links)
telegram.py → Telegram message (EMERGENCY only, short plain text)

The same DecisionRecord is re-rendered differently for each of the two channels.
"""
