"""
polymarket/detector.py — Polymarket channel-level tier decision. v2 (P9.1).

────────────────────────────────────────────────────────────────────────
Responsibilities (architecture §5.4.1, §5.2 / docs/p9-detection-design.md):
    FeatureSnapshot → ChannelSignal.
    The highest tier across the OR of the detector pool is the channel tier.

    v1 P2 baseline (legacy):
        price_jump_v1: price_jump_1min (based on last_trade_price) — odds_gap_v2 fallback

    v2 P9.1 new (4 detectors):
        vol_burst_v2     : vol_zscore_tod_v1 (M1 — time-of-day robust z-score) preferred
                           Falls back to vol_burst_abs_v1 when TOD samples are insufficient (<5)
        vol_burst_abs_v1 : (P12-D 2026-05-07) absolute USD volume threshold.
                           Introduced to solve the baseline-collapse problem of σ-based vol_z_v1.
                           In sparse Polymarket conditions baseline≈0 makes σ explode → meaningless.
                           Instead, judge by an absolute "≥ $X in 5 min" threshold.
        odds_gap_v2      : mid_price_jump_1min (M2 — orderbook mid)
                           Falls back to price_jump_v1 when has_mid_price=0
        odds_cusum_v1    : cusum_pos / cusum_neg (online change detection)
                           threshold = cumulative deviation in price [0,1] (3%p ~ 8%p)
        directional_v1   : combines |imbalance_5min| × same_side_run_length
                           Captures strong directional conviction (insider behavior pattern)

    P10.3 wallet detector:
        wallet_concentration_v1 :
            Stand-alone evaluation of wallet_concentration_score (HHI + dir_ratio + few-wallet bonus).
            Catches the "1-3 wallets dominate" or "all 38 wallets go the same direction" signature.
            Guards: n_trades >= 5 AND total_usd >= $5k (blocks micro-trade FPs).

    P12-D single-wallet detector (2026-05-07):
        single_wallet_burst_v1 :
            "n_trades_5min <= 3 but volume is large" — supplements the "single-wallet one-shot bet"
            pattern (Maduro $33K, 5/6 Iran $49.5K form) that wallet_concentration_v1 misses.
            When n_trades is low, HHI/dir_ratio are meaningless so WC is inactive → separate detector.

    P10.3 wallet modulator (final_tier ±1):
        - high concentration (wc_score >= 0.65) AND a non-WC detector fires at RISK_OFF/WATCH
          or above → final_tier ↑ by one step (insider booster)
        - low concentration (wc_score <= 0.30) AND unique_wallets >= 30
          AND final_tier == EMERGENCY → demote to RISK_OFF (suspected retail panic).

    Final channel tier = max(all detector tiers) → wallet modulator → final.
    Direction = directional_v1 first → the stronger cusum side → NEUTRAL.

────────────────────────────────────────────────────────────────────────
Design decisions (v2):

    · Not "v1 vs v2 both active" — v2 is primary, v1 is used only as fallback:
        - When tod_baseline_n >= tod_min_samples, only vol_burst_v2 is active and abs_v1 is skipped
        - When has_mid_price=1.0, only odds_gap_v2 is active and price_jump_v1 is skipped
        - This prevents two detectors with the same meaning from firing and inflating the score.

    · vol_burst and single_wallet_burst coexist as independent detectors (different signals).
        - Single-wallet one-shot ($X, n=2) → both fire (reinforcing)
        - Dispersed trading ($X, n=20)    → only vol_burst fires (SW skipped by n_trades guard)
        - When both fire, reason_codes show both → LLM/reader explicitly recognizes
          whether it was "a one-shot from one wallet".

    · directional_v1 has a safety-net "minimum sample" gate (n_trades_5min >= 5).
        Prevents EMERGENCY from one or two same-direction trades alone.

    · odds_cusum is semantically a "slow cumulative" detector. When corroborated by burst-type
        detectors it indicates genuine sustained pressure. Even on its own it is meaningful
        (gradual insider accumulation).

    · warmup policy change (v1 → v2):
        v1 always returned NORMAL when baseline_ready=False.
        v2 keeps mid/imbalance/cusum-based detectors functional even when baseline_ready=False
        (they only need current spot data). Only vol_burst_v2 requires the baseline.

    · Score: max across each detector's score. (Noisy-OR-style combination happens in the fusion stage.)

    · Threshold is a dataclass — v2 thresholds live here too. Moves to thresholds.yaml in P10.

────────────────────────────────────────────────────────────────────────
Architecture: §5.2 (fusion input), §5.4.1 (channel tier), §5.2 (score norm)
Plan: docs/p9-detection-design.md vol_burst_v2 / odds_gap_v2 / odds_cusum_v1 /
       directional_v1 detector definitions
"""

from __future__ import annotations

# stdlib only
import logging
from dataclasses import dataclass

from ...core.schemas import (
    CHANNEL_POLYMARKET,
    ChannelSignal,
    Direction,
    FeatureSnapshot,
    Tier,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Threshold config — v1 (compat) + v2 new
# =====================================================================
@dataclass(frozen=True, slots=True)
class PolymarketDetectorConfig:
    """v1 + v2 thresholds. Planned to split into thresholds.yaml in P10.

    v1 (legacy fallback):
        vol_z_*    : vol_zscore_5min (in-memory baseline) thresholds
        pj_*       : price_jump_1min (last trade) thresholds

    v2 (P9.1):
        tod_z_*    : vol_zscore_tod_v1 (M1) thresholds — same scale but a lower noise floor
        tod_min_n  : tod_baseline_n must be ≥ this value to activate vol_burst_v2
        mid_jump_* : mid_price_jump_1min thresholds — mid-based, so smaller values are meaningful vs v1
        cusum_*    : cumulative-deviation thresholds for cusum_pos/neg (price [0,1] units)
        imbalance_*: |imbalance_5min| thresholds (absolute value in [0,1])
        run_*      : same_side_run_length thresholds (integer)
        dir_min_n  : minimum 5-min trade count required to activate directional_v1
    """
    # ── v1 legacy ──
    # NOTE(v0.3.2 hotfix): EMERGENCY threshold raised to suppress false positives.
    # NOTE(v0.7.3 2026-05-07): vol_z_* is deprecated — the vol_z_v1 detector
    # was replaced by vol_burst_abs_v1. Fixes the σ-explosion issue in sparse
    # Polymarket conditions where baseline≈0 (49,539σ case). Config fields are
    # kept for backward compatibility.
    vol_z_watch: float = 4.0
    vol_z_riskoff: float = 6.0
    vol_z_emergency: float = 10.0
    pj_watch: float = 0.10
    pj_riskoff: float = 0.20
    pj_emergency: float = 0.20

    # ── v1 vol_burst_abs (P12-D 2026-05-07) ──
    # Fallback to absolute USD volume instead of σ when tod_baseline is insufficient.
    # Thresholds chosen by comparison with historical insider cases (Maduro $33K,
    # 5/6 Iran $49.5K). To be fine-tuned after 1-2 weeks of operation.
    vol_abs_watch_usd: float = 20_000.0
    vol_abs_riskoff_usd: float = 30_000.0
    vol_abs_emergency_usd: float = 70_000.0

    # ── v1 single_wallet_burst (P12-D 2026-05-07) ──
    # "very few n_trades but large volume" = single-wallet one-shot bet pattern.
    # Supplements cases the 5/6 Iran case ($49.5K, n_trades=2) that
    # wallet_concentration_v1 (requires n_trades>=5) cannot catch.
    sw_max_n_trades: int = 3                   # only active at or below this
    sw_watch_usd: float = 20_000.0
    sw_riskoff_usd: float = 30_000.0
    sw_emergency_usd: float = 70_000.0

    # ── v2 vol_burst (M1 time-of-day) ──
    tod_z_watch: float = 5.0
    tod_z_riskoff: float = 8.0
    tod_z_emergency: float = 12.0
    tod_min_n: int = 5

    # ── v2 odds_gap (M2 mid) ──
    mid_jump_watch: float = 0.06      # 6%p — hotfix: shrink the noise band
    mid_jump_riskoff: float = 0.12
    mid_jump_emergency: float = 0.25

    # ── v2 odds_cusum (cumulative deviation) ──
    cusum_watch: float = 0.04         # 4%p cumulative
    cusum_riskoff: float = 0.07       # 7%p
    cusum_emergency: float = 0.12     # hotfix: prevents EMERGENCY from a single cusum spike

    # ── v2 directional (imbalance × run) ──
    imbalance_watch: float = 0.55
    imbalance_riskoff: float = 0.75
    imbalance_emergency: float = 0.92
    run_watch: int = 6
    run_riskoff: int = 12
    run_emergency: int = 30
    dir_min_n: int = 5                # directional does NOT fire if n_trades_5min < 5
    min_riskoff_detectors_for_emergency: int = 2

    # ── direction classification threshold (signal-level) ──
    direction_imbalance_min: float = 0.30  # UP/DOWN only when |imbalance| ≥ this

    # ── P10.3 wallet_concentration_v1 (stand-alone detector) ──
    # Threshold on wallet_concentration_score ∈ [0, 1].
    # Synthesized score, so threshold is low — ≥ 0.55 indicates a "few-wallet
    # concentration / same-direction consensus" signature with higher confidence
    # than retail panic.
    #
    # Threshold calibration:
    #   - 1 wallet alone (\$100k) → wc_score≈0.90 → EMERGENCY
    #   - 5 wallets equal (all YES) → wc_score≈0.80 → RISK_OFF
    #   - 38 wallets split (Iran scenario, all YES) → wc_score≈0.66 → RISK_OFF
    #   - 50 wallets 80%/20% (news reaction) → wc_score≈0.40 → NORMAL
    #   - 100 retail mixed → wc_score≈0.25 → NORMAL
    wc_watch: float = 0.50
    wc_riskoff: float = 0.65
    wc_emergency: float = 0.85
    # Stand-alone wallet_concentration_v1 firing needs a minimum trade sample
    # for protection. With very few trades (n < 5), one wallet alone makes
    # HHI=1.0 → false-positive risk.
    wc_min_n_trades: int = 5
    # Stand-alone wallet_concentration_v1 firing also requires minimum volume
    # (the level at which an insider makes a meaningful bet = ≥ $5k).
    wc_min_total_usd: float = 5_000.0

    # ── P10.3 modulator: adjust another detector's tier by ±1 via wallet ──
    # high concentration → bump other detectors' result one step ↑ (capped at EMERGENCY)
    # low concentration  → demote another detector's EMERGENCY result by one step (suspected retail)
    wc_high_threshold: float = 0.65   # ≥ this is "high concentration" booster
    wc_low_threshold: float = 0.30    # ≤ this is "dispersed" — suspected retail panic
    # Minimum wallet count for the modulator to demote EMERGENCY → RISK_OFF
    # (with only 1-2 wallets, do not fold even if dispersed — too little data).
    wc_low_min_unique_wallets: int = 30
    # P10.4 boost guard: minimum wallet count required for WC_BOOST to fire.
    # With only 1~2 wallets, wallet_concentration_v1 stand-alone detector already
    # catches it, so no need to bump other detectors and create EMERGENCY spam.
    # Booster is meaningful only when 5+ wallets reach same-direction consensus
    # (e.g. the 38-wallet split pattern).
    wc_boost_min_unique_wallets: int = 5


# =====================================================================
# PolymarketDetector v2
# =====================================================================
class PolymarketDetector:
    """Tier-decision logic for the Polymarket channel (v1 + v2 detector pool OR)."""

    def __init__(self, config: PolymarketDetectorConfig | None = None) -> None:
        self.config = config or PolymarketDetectorConfig()

    # ─────────────────────────────────────────────────────────────────
    # Main — called once per cycle
    # ─────────────────────────────────────────────────────────────────
    def evaluate(self, features: FeatureSnapshot) -> ChannelSignal:
        """FeatureSnapshot → ChannelSignal.

        Args:
            features: PolymarketFeatures.compute_snapshot() result (includes v2 features).

        Returns:
            ChannelSignal: always returned (may be NORMAL). features_ref carries features.id.
        """
        c = self.config
        f = features.features

        # ── feature extraction (default 0 — safe even if keys are missing) ──
        # v1 — `pj` is used in the price_jump_v1 fallback. vol_zscore_5min is
        # deprecated in P12-D (vol_burst_abs_v1 replaces it) — the feature itself
        # is kept for compatibility but no longer read by the detector.
        pj = float(f.get("price_jump_1min", 0.0))
        baseline_ready = float(f.get("baseline_ready", 0.0)) >= 1.0

        # v2
        tod_z = float(f.get("vol_zscore_tod_v1", 0.0))
        tod_n = float(f.get("tod_baseline_n", 0.0))
        mid_jump = float(f.get("mid_price_jump_1min", 0.0))
        has_mid = float(f.get("has_mid_price", 0.0)) >= 1.0
        cusum_pos = float(f.get("cusum_pos", 0.0))
        cusum_neg = float(f.get("cusum_neg", 0.0))
        imbalance = float(f.get("imbalance_5min", 0.0))
        run_len = float(f.get("same_side_run_length", 0.0))
        n_trades = float(f.get("n_trades_5min", 0.0))

        # P10.3 wallet concentration features (default 0 if absent — compat with old features)
        wc_score = float(f.get("wallet_concentration_score", 0.0))
        wc_unique = float(f.get("unique_wallets_5min", 0.0))
        wc_top_share = float(f.get("top_wallet_share_5min", 0.0))
        wc_dir_ratio = float(f.get("directional_wallet_ratio_5min", 0.0))
        cur_vol_usd = float(f.get("current_vol_usd", 0.0))

        # ─────────────────────────────────────────────────────────────
        # Detector 1: vol_burst_v2 (M1 time-of-day) — fallback to vol_burst_abs_v1
        #    Use v2 σ-based when TOD samples suffice, otherwise fall back to
        #    the absolute USD threshold. The σ-based vol_z_v1 was deprecated
        #    (P12-D 2026-05-07) because σ explodes when baseline≈0 in sparse Polymarket.
        # ─────────────────────────────────────────────────────────────
        burst_tier = Tier.NORMAL
        burst_score = 0.0
        burst_name = ""
        burst_reason = ""

        use_tod = tod_n >= c.tod_min_n
        if use_tod:
            burst_tier = self._tier_3step(
                tod_z, c.tod_z_watch, c.tod_z_riskoff, c.tod_z_emergency,
            )
            burst_score = self._z_score_to_unit(
                tod_z, c.tod_z_watch, c.tod_z_riskoff, c.tod_z_emergency,
            )
            burst_name = "vol_burst_v2"
            if tod_z >= c.tod_z_watch:
                burst_reason = f"VOL_TOD_Z={tod_z:.1f} (n={int(tod_n)})"
        else:
            # vol_burst_abs_v1 — absolute USD threshold (independent of baseline, prevents σ explosion).
            burst_tier = self._tier_3step(
                cur_vol_usd,
                c.vol_abs_watch_usd, c.vol_abs_riskoff_usd, c.vol_abs_emergency_usd,
            )
            burst_score = self._linear_score(
                cur_vol_usd,
                c.vol_abs_watch_usd, c.vol_abs_riskoff_usd, c.vol_abs_emergency_usd,
            )
            burst_name = "vol_burst_abs_v1"
            if cur_vol_usd >= c.vol_abs_watch_usd:
                burst_reason = (
                    f"VOL_USD=${cur_vol_usd / 1000:.1f}K (n={int(n_trades)})"
                )

        # ─────────────────────────────────────────────────────────────
        # Detector 2: odds_gap_v2 (M2 mid-price jump) — fallback to v1 pj
        # ─────────────────────────────────────────────────────────────
        if has_mid:
            gap_tier = self._tier_3step(
                mid_jump,
                c.mid_jump_watch, c.mid_jump_riskoff, c.mid_jump_emergency,
            )
            gap_score = self._linear_score(
                mid_jump,
                c.mid_jump_watch, c.mid_jump_riskoff, c.mid_jump_emergency,
            )
            gap_name = "odds_gap_v2"
            gap_reason = f"MID_JUMP_1M={mid_jump:.3f}" if mid_jump >= c.mid_jump_watch else ""
        else:
            # v1 fallback — jump based on last_trade_price
            gap_tier = self._tier_2step(pj, c.pj_watch, c.pj_riskoff)
            gap_score = self._linear_score(pj, c.pj_watch, c.pj_riskoff, c.pj_riskoff * 2.0)
            gap_name = "price_jump_v1"
            gap_reason = f"PRICE_JUMP_1M={pj:.3f}" if pj >= c.pj_watch else ""

        # ─────────────────────────────────────────────────────────────
        # Detector 3: odds_cusum_v1 (cumulative deviation)
        #    Inspect both pos and neg and adopt the stronger one.
        #    Also makes a first-pass direction call here (UP if pos>neg else DOWN).
        # ─────────────────────────────────────────────────────────────
        cusum_strength = max(cusum_pos, cusum_neg)
        cusum_tier = self._tier_3step(
            cusum_strength,
            c.cusum_watch, c.cusum_riskoff, c.cusum_emergency,
        )
        cusum_score = self._linear_score(
            cusum_strength,
            c.cusum_watch, c.cusum_riskoff, c.cusum_emergency,
        )
        cusum_reason = (
            f"CUSUM_{'POS' if cusum_pos >= cusum_neg else 'NEG'}={cusum_strength:.3f}"
            if cusum_strength >= c.cusum_watch else ""
        )

        # ─────────────────────────────────────────────────────────────
        # Detector 4: directional_v1 (imbalance × run)
        #    Both |imbalance| and run_length must clear the same tier threshold to fire.
        #    → No alert from a small-sample asymmetry alone.
        # ─────────────────────────────────────────────────────────────
        dir_tier = Tier.NORMAL
        dir_score = 0.0
        dir_reason = ""

        if n_trades >= c.dir_min_n:
            abs_imb = abs(imbalance)
            dir_tier = self._directional_tier(abs_imb, run_len)
            dir_score = self._directional_score(abs_imb, run_len)
            if dir_tier != Tier.NORMAL:
                dir_reason = (
                    f"DIR_IMB={imbalance:+.2f} RUN={int(run_len)}"
                )

        # ─────────────────────────────────────────────────────────────
        # Detector 5: wallet_concentration_v1 (P10.3)
        #    Few-wallet concentration OR many-wallet same-direction consensus (= 38-wallet split).
        #    Insider signature: even at the same volume, a narrow wallet distribution
        #    raises suspicion.
        #
        #    Guards for stand-alone firing:
        #      - minimum trade count (n_trades >= wc_min_n_trades) — with only 1-2 trades,
        #        HHI=1.0 is automatic and a false positive otherwise.
        #      - minimum volume ($5k+) — do not flag concentration of micro-trades.
        # ─────────────────────────────────────────────────────────────
        wc_tier = Tier.NORMAL
        wc_score_norm = 0.0
        wc_reason = ""

        if (
            n_trades >= c.wc_min_n_trades
            and cur_vol_usd >= c.wc_min_total_usd
        ):
            wc_tier = self._tier_3step(
                wc_score, c.wc_watch, c.wc_riskoff, c.wc_emergency,
            )
            wc_score_norm = self._linear_score(
                wc_score, c.wc_watch, c.wc_riskoff, c.wc_emergency,
            )
            if wc_tier != Tier.NORMAL:
                wc_reason = (
                    f"WC={wc_score:.2f} (n_wallets={int(wc_unique)} "
                    f"top={wc_top_share:.0%} dir_ratio={wc_dir_ratio:.0%})"
                )

        # ─────────────────────────────────────────────────────────────
        # Detector 6: single_wallet_burst_v1 (P12-D 2026-05-07)
        #    "very few n_trades but large volume" — single-wallet one-shot bet.
        #    Supplements patterns wallet_concentration_v1 cannot catch (n_trades < 5).
        #    Form: Maduro insider ($33K initial) / 5/6 Iran ($49.5K, n=2).
        # ─────────────────────────────────────────────────────────────
        sw_tier = Tier.NORMAL
        sw_score_norm = 0.0
        sw_reason = ""

        if 0 < n_trades <= c.sw_max_n_trades:
            sw_tier = self._tier_3step(
                cur_vol_usd,
                c.sw_watch_usd, c.sw_riskoff_usd, c.sw_emergency_usd,
            )
            sw_score_norm = self._linear_score(
                cur_vol_usd,
                c.sw_watch_usd, c.sw_riskoff_usd, c.sw_emergency_usd,
            )
            if sw_tier != Tier.NORMAL:
                sw_reason = (
                    f"SW_BURST=${cur_vol_usd / 1000:.1f}K (n={int(n_trades)})"
                )

        # ─────────────────────────────────────────────────────────────
        # Aggregate — max tier, max score, build fired list
        # ─────────────────────────────────────────────────────────────
        tier_pairs = [
            (burst_tier, burst_name, burst_reason, burst_score),
            (gap_tier, gap_name, gap_reason, gap_score),
            (cusum_tier, "odds_cusum_v1", cusum_reason, cusum_score),
            (dir_tier, "directional_v1", dir_reason, dir_score),
            (wc_tier, "wallet_concentration_v1", wc_reason, wc_score_norm),
            (sw_tier, "single_wallet_burst_v1", sw_reason, sw_score_norm),
        ]

        fired = [name for t, name, _, _ in tier_pairs if t != Tier.NORMAL and name]
        reasons = [r for _, _, r, _ in tier_pairs if r]
        final_tier = Tier.max_of([t for t, _, _, _ in tier_pairs])
        final_score = max((s for _, _, _, s in tier_pairs), default=0.0)
        final_score = max(0.0, min(1.0, final_score))

        # hotfix(v0.3.2): prevent EMERGENCY spam from a single overly-sensitive detector.
        if final_tier == Tier.EMERGENCY:
            riskoff_plus_cnt = sum(
                1 for t, _, _, _ in tier_pairs if t in (Tier.RISK_OFF, Tier.EMERGENCY)
            )
            if riskoff_plus_cnt < c.min_riskoff_detectors_for_emergency:
                final_tier = Tier.RISK_OFF
                reasons.append("HOTFIX_EMERGENCY_GUARD")

        # ─────────────────────────────────────────────────────────────
        # P10.3 Wallet modulator
        # Adjust the tier of fired burst/cusum/directional detectors by ±1 based on wallet distribution.
        #
        #   high concentration (wc_score >= wc_high_threshold)
        #     → "truly insider-looking" → +1 step (capped at EMERGENCY)
        #     Only acts as a booster when at least one of burst/cusum/dir is RISK_OFF or higher,
        #     not from the wallet feature alone.
        #
        #   low concentration (wc_score <= wc_low_threshold)
        #     AND unique_wallets >= wc_low_min_unique_wallets (meaningful dispersion)
        #     → "suspected retail panic" → demote EMERGENCY only by one step.
        # ─────────────────────────────────────────────────────────────
        # Highest tier among burst/cusum/dir in tier_pairs (excluding WC itself).
        non_wc_max = Tier.max_of([
            burst_tier, gap_tier, cusum_tier, dir_tier,
        ])
        if (
            wc_score >= c.wc_high_threshold
            and non_wc_max in (Tier.RISK_OFF, Tier.WATCH)
            and final_tier != Tier.EMERGENCY
            # P10.4 guard: do not boost when only 1~2 wallets exist.
            # That case is already handled by stand-alone wallet_concentration_v1.
            # Only boost when 5+ wallets reach same-direction consensus (e.g. 38-wallet split).
            and wc_unique >= c.wc_boost_min_unique_wallets
        ):
            final_tier = self._tier_up(final_tier)
            reasons.append(
                f"WC_BOOST(score={wc_score:.2f} n={int(wc_unique)})"
            )
        elif (
            wc_score <= c.wc_low_threshold
            and wc_unique >= c.wc_low_min_unique_wallets
            and final_tier == Tier.EMERGENCY
            and n_trades >= c.dir_min_n
        ):
            final_tier = Tier.RISK_OFF
            reasons.append(
                f"WC_DAMP_RETAIL(score={wc_score:.2f} n={int(wc_unique)})"
            )

        # ─────────────────────────────────────────────────────────────
        # Direction decision — directional_v1 → cusum → NEUTRAL
        # ─────────────────────────────────────────────────────────────
        direction = self._resolve_direction(
            imbalance=imbalance,
            cusum_pos=cusum_pos,
            cusum_neg=cusum_neg,
            dir_fired=(dir_tier != Tier.NORMAL),
        )

        # ─────────────────────────────────────────────────────────────
        # Confidence — lower slightly when baseline is insufficient (audit signal)
        # ─────────────────────────────────────────────────────────────
        if not baseline_ready and not use_tod:
            # If neither is available, burst-type detectors are meaningless → lower confidence
            confidence = 0.5
            if final_tier == Tier.NORMAL:
                # NORMAL but leave an audit trace
                reasons.append("WARMUP_BASELINE_INSUFFICIENT")
        else:
            confidence = 1.0

        return ChannelSignal(
            channel=CHANNEL_POLYMARKET,
            symbol=features.symbol,
            ts=features.ts,
            score=final_score,
            tier=final_tier,
            direction=direction,
            confidence=confidence,
            features_ref=features.id,
            fired_detectors=fired,
            reason_codes=reasons,
        )

    # ─────────────────────────────────────────────────────────────────
    # Per-detector tier rules — split into small helpers
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _tier_3step(value: float, w: float, r: float, e: float) -> Tier:
        """value >= e → EMERGENCY, >= r → RISK_OFF, >= w → WATCH, else NORMAL."""
        if value >= e:
            return Tier.EMERGENCY
        if value >= r:
            return Tier.RISK_OFF
        if value >= w:
            return Tier.WATCH
        return Tier.NORMAL

    @staticmethod
    def _tier_up(t: Tier) -> Tier:
        """Bump Tier up by one step (NORMAL→WATCH→RISK_OFF→EMERGENCY, stays at EMERGENCY)."""
        if t == Tier.NORMAL:
            return Tier.WATCH
        if t == Tier.WATCH:
            return Tier.RISK_OFF
        if t == Tier.RISK_OFF:
            return Tier.EMERGENCY
        return t  # EMERGENCY unchanged

    @staticmethod
    def _tier_2step(value: float, w: float, r: float) -> Tier:
        """value >= r → RISK_OFF, >= w → WATCH, else NORMAL. (No EMERGENCY)"""
        if value >= r:
            return Tier.RISK_OFF
        if value >= w:
            return Tier.WATCH
        return Tier.NORMAL

    def _directional_tier(self, abs_imb: float, run_len: float) -> Tier:
        """Rule that requires imbalance + run_length to satisfy simultaneously.

        Both must clear the threshold for that tier — one strong side alone drops a tier.
        """
        c = self.config
        # EMERGENCY: both EMERGENCY
        if abs_imb >= c.imbalance_emergency and run_len >= c.run_emergency:
            return Tier.EMERGENCY
        # RISK_OFF: both RISK_OFF (or one EMERGENCY + the other RISK_OFF)
        if abs_imb >= c.imbalance_riskoff and run_len >= c.run_riskoff:
            return Tier.RISK_OFF
        # WATCH: both WATCH
        if abs_imb >= c.imbalance_watch and run_len >= c.run_watch:
            return Tier.WATCH
        return Tier.NORMAL

    # ─────────────────────────────────────────────────────────────────
    # Score mapping — [0, 1] piecewise linear
    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _linear_score(value: float, w: float, r: float, e: float) -> float:
        """Map `value` to [0,1] piecewise based on (w, r, e) thresholds.

        Mapping:
            value <= 0          → 0.0
            value < w           → 0 ~ 0.50 (linear)
            value < r           → 0.50 ~ 0.70
            value < e           → 0.70 ~ 0.85
            value >= e          → 0.85 ~ 0.95 (asymptotic)
        """
        if value <= 0:
            return 0.0
        if value < w:
            return 0.50 * (value / w) if w > 0 else 0.0
        if value < r:
            t = (value - w) / (r - w) if r > w else 0.0
            return 0.50 + 0.20 * t
        if value < e:
            t = (value - r) / (e - r) if e > r else 0.0
            return 0.70 + 0.15 * t
        # >= emergency: asymptotic to 0.95
        extra = max(0.0, value - e)
        return 0.85 + 0.10 * (extra / (extra + e))  # another `e` of growth reaches 0.90

    @staticmethod
    def _z_score_to_unit(z: float, w: float, r: float, e: float) -> float:
        """z-score variant (simple wrapper — clarifies intent). Same as _linear_score.

        z-score may be negative (current < baseline), but that is not an anomaly, so return 0.
        """
        if z <= 0:
            return 0.0
        return PolymarketDetector._linear_score(z, w, r, e)

    def _directional_score(self, abs_imb: float, run_len: float) -> float:
        """directional_v1 score — average scale of abs(imbalance) and run_length.

        Normalize both to 0~1 and take the average. Score stays low if one side
        is strong but the other is weak.
        """
        c = self.config
        # imbalance is already in [0, 1] (absolute value)
        imb_unit = self._linear_score(
            abs_imb, c.imbalance_watch, c.imbalance_riskoff, c.imbalance_emergency,
        )
        # run_length is an integer count — normalize against the thresholds
        run_unit = self._linear_score(
            run_len, float(c.run_watch), float(c.run_riskoff), float(c.run_emergency),
        )
        # Take the weaker side (min) — strong-on-only-one-side is weak overall
        return min(imb_unit, run_unit)

    # ─────────────────────────────────────────────────────────────────
    # Direction resolution
    # ─────────────────────────────────────────────────────────────────
    def _resolve_direction(
        self,
        *,
        imbalance: float,
        cusum_pos: float,
        cusum_neg: float,
        dir_fired: bool,
    ) -> Direction:
        """Direction priority:
            1) If directional_v1 fired, use the sign of imbalance
            2) Else, the stronger cusum side
            3) If both are weak, NEUTRAL
        """
        c = self.config

        # 1) When directional fires, use the imbalance sign
        if dir_fired:
            if imbalance >= c.direction_imbalance_min:
                return Direction.UP
            if imbalance <= -c.direction_imbalance_min:
                return Direction.DOWN

        # 2) Stronger cusum side (NEUTRAL if both 0)
        if cusum_pos > cusum_neg and cusum_pos >= c.cusum_watch:
            return Direction.UP
        if cusum_neg > cusum_pos and cusum_neg >= c.cusum_watch:
            return Direction.DOWN

        # 3) Only weak imbalance present (directional did not fire but sign is clear)
        if imbalance >= c.direction_imbalance_min:
            return Direction.UP
        if imbalance <= -c.direction_imbalance_min:
            return Direction.DOWN

        return Direction.NEUTRAL
