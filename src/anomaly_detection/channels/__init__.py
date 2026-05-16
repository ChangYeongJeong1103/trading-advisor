"""
src/anomaly/channels/ — where Channel implementations live.

Each channel internally has:
  collector    → talks to the source (WebSocket / REST / scrape)
  normalizer   → converts to a unified NormalizedEvent
  features     → produces rolling stats, z-scores, etc.
  detector     → emits its channel's ChannelSignal (score + tier)

Core never imports a channel's internal files.
Only uses the base.Channel interface + ChannelSignal schema.

Architecture: §2.1 Component Responsibility, §4.1 Canonical Types
"""
