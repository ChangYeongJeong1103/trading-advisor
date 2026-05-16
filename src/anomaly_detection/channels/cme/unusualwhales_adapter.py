"""
cme/unusualwhales_adapter.py — Unusual Whales options flow adapter.

Subscription (D13: type="subscription"). Provides context-rich data such as
options sweeps, dark pool, and congressional trading. Used to corroborate
CME futures signals.

Implementation phase: P4
"""


class UnusualWhalesAdapter:
    """Unusual Whales API client."""

    def __init__(self, config: object) -> None:
        # TODO(P4): API key, polling interval (poll if no real-time stream)
        pass

    async def poll_options_flow(self, underlying: str) -> list:
        """Poll the most recent options flow for the given underlying.

        Returns:
            list of RawEvent (options sweep events).
        """
        raise NotImplementedError("Implemented in P4")
