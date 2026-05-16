import os
from dotenv import load_dotenv

"""
Configuration module for the Trading Advisor.

This file centralizes all hyperparameters and paths so they can be
re-used by the notebook, CLI tools, or a Streamlit/FastAPI app.
"""

# Load environment variables from .env (if present)
load_dotenv()


# ========================================
# 📁 General Configuration
# ========================================
import os

# Get the directory of the current script (deploy folder)
_current_dir = os.path.dirname(os.path.abspath(__file__))
# Set DOCS_FOLDER relative to the script's directory (parent directory -> data)
DOCS_FOLDER: str = os.path.join(os.path.dirname(_current_dir), "data")

# LLM model name (can be overridden via environment variable)
LLM_MODEL: str = os.getenv("LLM_MODEL", "gpt-4o-mini")


# ========================================
# 🔧 STEP 1: RAG Quality Parameters
# ========================================
# 1️⃣ Document Splitting Settings
CHUNK_SIZE: int = 800        # Characters per chunk
CHUNK_OVERLAP: int = 100     # Overlap between chunks (characters)

# 2️⃣ Retrieval Settings
TOP_K_DOCUMENTS: int = 4     # Number of documents to retrieve

# 3️⃣ LLM Temperature
LLM_TEMPERATURE: float = 0.2  # Lower = focused, Higher = creative


# ========================================
# ⚙️ STEP 2: System-level Parameters
# ========================================
# 1️⃣ Sigmoid Transformation Settings (for relevance score)
SIGMOID_MIDPOINT: float = 0.5    # Center point of sigmoid
SIGMOID_STEEPNESS: float = 12.0  # How sharp the transition is

# 2️⃣ Conditional RAG Thresholds
RELEVANCE_THRESHOLD: float = 0.62   # Score ≥ this: RAG-only
FALLBACK_THRESHOLD: float = 0.50    # Between this and RELEVANCE: Smart Fallback
OFF_TOPIC_THRESHOLD: float = 0.15   # Below this: auto-reject as off-topic

# 3️⃣ LLM Confidence Settings
LLM_CONFIDENCE_THRESHOLD: int = 5   # "No Answer" if confidence < this


# ========================================
# 🧪 Optional: Test Queries (for evaluation / debugging)
# ========================================
TEST_QUERIES = [
    # 1. 🟢 TIER 1: RAG Mode (relevance ≥ RELEVANCE_THRESHOLD)
    "Summarize the main points of the US stock market in October 2025",

    # 2. 🟢/🔵 TIER 2: Smart Fallback (FALLBACK_THRESHOLD ≤ relevance < RELEVANCE_THRESHOLD)
    "List the two major tech companies addressed in the 2025 market review and predict their performance in 2026",

    # 3. 🟠 TIER 3: LLM Domain Check (OFF_TOPIC_THRESHOLD ≤ relevance < FALLBACK_THRESHOLD)
    "How to make profit in volatile market in 2026 that is highly affected by tariff & FED policy on inflation, and earnings of AI companies?",

    # 4. 🟠/🔴 TIER 3: LLM Domain Check — low confidence / prediction
    "What will AAPL stock price be in 2026 based on the current market situation?",

    # 5. 🔴 TIER 3: Off-topic but finance-adjacent
    "Who will be the Apple CEO in 2030?",

    # 6. 🔴 TIER 4: Clearly off-topic
    "Explain the concept of quantum entanglement and its applications in quantum computing."
]


def validate_config() -> None:
    """
    Validate that threshold relationships and parameter ranges are sensible.

    Raises:
        ValueError: If any configuration is inconsistent.
    """
    if FALLBACK_THRESHOLD >= RELEVANCE_THRESHOLD:
        raise ValueError(
            f"FALLBACK_THRESHOLD ({FALLBACK_THRESHOLD}) must be < "
            f"RELEVANCE_THRESHOLD ({RELEVANCE_THRESHOLD}). "
            "Thresholds must satisfy: OFF_TOPIC < FALLBACK < RELEVANCE."
        )

    if OFF_TOPIC_THRESHOLD >= FALLBACK_THRESHOLD:
        raise ValueError(
            f"OFF_TOPIC_THRESHOLD ({OFF_TOPIC_THRESHOLD}) must be < "
            f"FALLBACK_THRESHOLD ({FALLBACK_THRESHOLD}). "
            "Thresholds must satisfy: OFF_TOPIC < FALLBACK < RELEVANCE."
        )

    if not (0.0 <= SIGMOID_MIDPOINT <= 1.0):
        raise ValueError(
            f"SIGMOID_MIDPOINT must be between 0.0 and 1.0, got {SIGMOID_MIDPOINT}"
        )

    if SIGMOID_STEEPNESS <= 0:
        raise ValueError(
            f"SIGMOID_STEEPNESS must be positive, got {SIGMOID_STEEPNESS}"
        )


if __name__ == "__main__":
    # Simple sanity check when running this module directly
    validate_config()
    print("=" * 80)
    print("✅ Configuration validated successfully")
    print("=" * 80)
    print(f"📂 Documents Folder: {DOCS_FOLDER}")
    print(f"🤖 LLM Model: {LLM_MODEL}")
    print(f"📏 Chunk Size: {CHUNK_SIZE} (Overlap: {CHUNK_OVERLAP})")
    print(f"🎯 RAG Threshold: {RELEVANCE_THRESHOLD}")
    print(f"🔄 Fallback Threshold: {FALLBACK_THRESHOLD}")
    print(f"🚫 Off-Topic Threshold: {OFF_TOPIC_THRESHOLD}")
    print(f"🧠 LLM Confidence Threshold: {LLM_CONFIDENCE_THRESHOLD}")
    print(f"📈 Sigmoid Midpoint: {SIGMOID_MIDPOINT}")
    print(f"📈 Sigmoid Steepness: {SIGMOID_STEEPNESS}")


