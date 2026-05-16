"""
src/anomaly/core/ — Channel-agnostic core.

Code in this folder never knows "which channel" anything came from.
It only deals with canonical schemas like ChannelSignal / FusedAnomalyEvent /
DecisionRecord, and is responsible for channel registration / fusion /
decision / state transitions.

Architecture: §2 Logical view, §5 Behavioral view
"""
