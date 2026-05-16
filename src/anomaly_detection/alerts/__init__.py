"""
src/anomaly/alerts/ — Push notification layer (primary UX in v1).

DecisionRecord → reaches the user.

Flow:
  DecisionRecord
    ↓
  router.dispatch()        — choose channel by tier (Email / Email+Telegram)
    ↓
  throttle.should_send()   — state-change-only, cooldown, dilution defense
    ↓
  renderer.email.render()  — self-contained HTML body (5-min batch + 30-min timeline)
  renderer.telegram.send() — EMERGENCY only
    ↓
  link_builder             — external visual links (Hypurrscan, Polymarket UI, etc.)
"""
