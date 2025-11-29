# Hyperparameter Tuning

To ensure the `ConditionalRAGAdvisor` makes the right decisions, we didn't just guess the thresholds—we tuned them using a data-driven approach.

## Methodology

We created a **Grid Search** experiment involving:
1.  **Test Dataset**: 6 queries labeled by category (RAG-able, General Finance, Off-topic).
2.  **Hyperparameters Tuned**: All system hyperparameters were tuned using the test dataset. Key parameters include:
    *   **Chunking**: `CHUNK_SIZE` (800), `CHUNK_OVERLAP` (100)
    *   **Retrieval**: `TOP_K_DOCUMENTS` (4)
    *   **LLM**: `LLM_TEMPERATURE` (0.2)
    *   **Sigmoid**: `SIGMOID_MIDPOINT` (0.5), `SIGMOID_STEEPNESS` (12)
    *   **Thresholds**: `RELEVANCE_THRESHOLD` (0.62), `FALLBACK_THRESHOLD` (0.50), `OFF_TOPIC_THRESHOLD` (0.15)
    *   **Confidence**: `LLM_CONFIDENCE_THRESHOLD` (5)

## Experiment Process

The tuning was conducted systematically in multiple phases, with results logged in `experiments/logs_*` and `experiments/hyperparameter_experiments_*.csv`:

**Phase 1-2: Initial Hyperparameter Search**
*   Explored **chunking** (400-1200 chars), **retrieval** (TOP_K: 2-8), and **LLM temperature** (0.0-0.7)
*   Tested 16 configurations with 5-6 queries
*   Measured: avg_improvement_pct, response_time, token usage, mode distribution

**Phase A: Sigmoid Steepness Tuning**
*   Systematically varied `SIGMOID_STEEPNESS` from 5 to 20 (63 experiments)
*   Fixed `SIGMOID_MIDPOINT` at 0.4, 0.42, 0.45, 0.48, 0.5
*   Selected optimal: **SIGMOID_STEEPNESS = 12, SIGMOID_MIDPOINT = 0.5**

**Phase B: Relevance Threshold Tuning**
*   Tested `RELEVANCE_THRESHOLD` from 0.55 to 0.8 (10 experiments)
*   Selected optimal: **RELEVANCE_THRESHOLD = 0.62**

**Phase C: Fallback Threshold Tuning**
*   Tested `FALLBACK_THRESHOLD` from 0.45 to 0.58 (12 experiments)
*   Selected optimal: **FALLBACK_THRESHOLD = 0.50**

**Phase D: Off-Topic Threshold Tuning**
*   Tested `OFF_TOPIC_THRESHOLD` from 0.1 to 0.25 (7 experiments)
*   Selected optimal: **OFF_TOPIC_THRESHOLD = 0.15**

**Phase E: LLM Confidence Threshold Tuning**
*   Tested `LLM_CONFIDENCE_THRESHOLD` from 3 to 8 (9 experiments)
*   Selected optimal: **LLM_CONFIDENCE_THRESHOLD = 5**

**Phase 3-4: Final Validation**
*   Verified optimal hyperparameters with all 6 queries
*   Confirmed consistent performance across all tiers

## Final Optimized Values

After analyzing the results (saved in `notebooks/experiments/`), we settled on the following configuration:

| Parameter | Value | Reason |
| :--- | :--- | :--- |
| **CHUNK_SIZE** | `800` | Balanced context size - not too small (fragmented), not too large (noisy). |
| **CHUNK_OVERLAP** | `100` | Preserves context continuity across chunk boundaries. |
| **TOP_K_DOCUMENTS** | `4` | Enough context without introducing excessive noise. |
| **LLM_TEMPERATURE** | `0.2` | Low temperature for focused, consistent financial advice. |
| **SIGMOID_MIDPOINT** | `0.5` | Center point for score transformation. |
| **SIGMOID_STEEPNESS** | `12` | Sharp decision boundary between relevant/irrelevant documents. |
| **RELEVANCE_THRESHOLD** | `0.62` | RAG Mode - high confidence threshold for direct document use. |
| **FALLBACK_THRESHOLD** | `0.50` | Smart Fallback - captures "grey area" for RAG vs LLM comparison. |
| **OFF_TOPIC_THRESHOLD** | `0.15` | Auto-reject clearly off-topic queries without LLM call. |
| **LLM_CONFIDENCE_THRESHOLD** | `5` | Minimum confidence (out of 10) for LLM to provide an answer. |

## Sigmoid Transformation Function

The scoring system uses a sigmoid function to transform raw similarity scores ($x$) into a decision probability ($y$):

$$ y = \frac{1}{1 + e^{-k(x - x_0)}} $$

Where:
*   $k$ = Steepness (12.0)
*   $x_0$ = Midpoint (0.5)

This ensures that scores slightly above 0.5 are quickly boosted to high confidence, while scores slightly below are suppressed, reducing ambiguity.

