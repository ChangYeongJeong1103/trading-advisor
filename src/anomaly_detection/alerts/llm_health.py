"""
alerts/llm_health.py — LLM availability tracking + outage alerting.

──────────────────────────────────────────────────────────────────────
Why this exists (user decision 2026-08-09):

  On 2026-08-02 09:30 PDT the OpenAI credit balance went negative
  (-$5.32) and every LLM call started failing with 429
  (credit_balance_exhausted). Each call site was designed to fall back
  quietly on failure, so nobody noticed for 7.5 days. Actual damage:

    · alert emails silently lost their Market-impact row (no LLM verdict)
    · polymarket EMERGENCY alerts could not clear the score gate, so
      every X post was skipped
    · Truth Social derives its tier from the LLM score itself, so every
      Trump post fell back to score=0 NORMAL and was dropped — the
      channel was effectively blind

  We keep the per-call fallbacks (one failure must never kill the
  daemon), but a *sustained* failure has to reach a human.

──────────────────────────────────────────────────────────────────────
Design:

  · Three places call the LLM, and they are constructed at different
    times:
      1) alerts/llm_assessor.py             — EMERGENCY alert assessment
      2) channels/truth_social/llm_scorer.py — Trump post scoring
      3) channels/x/llm_classifier.py        — X post classification
    They all share one OpenAI account, so credit/key outages always hit
    all three at once. Per-call-site counters would each stay below
    their own threshold and miss the outage; a single shared counter
    detects it far sooner.

  · Because the call sites are built at different levels of the object
    graph, constructor injection would have to thread through four
    layers. Instead we expose one module-level instance (LLM_HEALTH),
    the same way logging.getLogger is used, and the daemon only calls
    set_alert_cb() at startup to plug in email delivery.

  · The counting / alerting logic lives in the shared ApiHealthTracker
    class in alerts/api_health.py (split out when X API outage alerts
    were added on 2026-10-09). This module only defines the LLM
    component name and message text.

  · The `degraded` property tells the email renderer whether to show the
    "LLM scoring unavailable" banner.
"""

from __future__ import annotations

from .api_health import ApiHealthTracker

# Component name shown in health alerts; also the cooldown / digest key.
LLM_COMPONENT_NAME = "system.llm"

# How many consecutive failures before we alert.
#
# LLM calls only happen when there is an event (Trump post / X post /
# EMERGENCY). In quiet hours there are only a few calls per hour, so a
# large threshold would delay the alert by days; a threshold of 1 would
# fire on every transient timeout. 3 absorbs blips while catching a
# sustained outage like credit exhaustion usually within an hour.
_LLM_FAIL_THRESHOLD = 3

# Backward-compatible alias — the class itself is api_health.ApiHealthTracker.
LLMHealthTracker = ApiHealthTracker

# Shared singleton — see "Design" in the module docstring.
# Call sites import it via `from ..alerts.llm_health import LLM_HEALTH`;
# only the daemon calls set_alert_cb() at startup.
LLM_HEALTH = ApiHealthTracker(
    component=LLM_COMPONENT_NAME,
    fail_message=(
        "LLM calls failed {count} times in a row. "
        "Alert emails lose their Market-impact score, score-gated X "
        "posting is skipped, and Truth Social posts fall back to "
        "NORMAL (channel effectively blind). "
        "Check the OpenAI credit balance and API key first."
    ),
    recovery_message=(
        "LLM calls recovered — {source} succeeded. "
        "Market-impact scoring and Truth Social tiering are live again."
    ),
    banner_message=(
        "LLM scoring unavailable since {since} "
        "({count} consecutive failures). "
        "Market-impact score is missing from this alert, and "
        "score-gated X posting is being skipped."
    ),
    fail_threshold=_LLM_FAIL_THRESHOLD,
)


__all__ = [
    "LLM_HEALTH",
    "LLM_COMPONENT_NAME",
    "LLMHealthTracker",
]
