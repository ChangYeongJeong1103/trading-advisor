import csv
import logging
import os
import time
import tiktoken
from datetime import datetime
from functools import wraps
from typing import Any, Callable, Dict, List, Optional

"""
Logging, cost tracking, and experiment utilities for the Trading Advisor.

This module centralizes:
- Logging configuration
- Token/cost tracking for LLM calls
- Simple retry decorator for fragile operations (e.g., API calls)
- Optional experiment tracking (for hyperparameter tuning)
"""


# ========================================
# 📝 LOGGING SETUP
# ========================================
# Determine the directory where this module (logging_utils.py) is located.
# This ensures consistent paths regardless of where the app is run from.
MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

# Ensure logs directory exists (inside the same folder as this module)
LOGS_DIR = os.path.join(MODULE_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

# Reset existing handlers to avoid duplicate logs if re-imported
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)
    try:
        handler.close()
    except Exception:
        pass

log_filename = os.path.join(LOGS_DIR, f"trading_advisor_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_filename),
        logging.StreamHandler(),
    ],
    force=True,
)

logger = logging.getLogger(__name__)
logger.info(f"📝 Logging initialized: {log_filename}")


# ========================================
# 💰 COST TRACKING
# ========================================
class CostTracker:
    """
    Track token usage and estimated API costs.

    Pricing is approximate and can be adjusted as needed.
    """

    # Pricing per 1M tokens (USD)
    PRICING: Dict[str, Dict[str, float]] = {
        "gpt-5": {"input": 1.25, "output": 10.00},
        "gpt-5-mini": {"input": 0.25, "output": 2.00},
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    }

    def __init__(self, model_name: str = "gpt-4o-mini") -> None:
        """Initialize a cost tracker for a specific model."""
        self.model_name = model_name
        self.encoding = tiktoken.encoding_for_model(model_name)
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0

    def count_tokens(self, text: str) -> int:
        """Count tokens in a text string."""
        return len(self.encoding.encode(text))

    def add_tokens(self, input_text: str, output_text: str) -> tuple[int, int]:
        """
        Add token counts for input and output and update totals.

        Returns:
            (input_tokens, output_tokens)
        """
        input_tokens = self.count_tokens(input_text)
        output_tokens = self.count_tokens(output_text)

        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens

        return input_tokens, output_tokens

    def get_cost(self) -> float:
        """Calculate total estimated cost in USD."""
        if self.model_name not in self.PRICING:
            return 0.0

        pricing = self.PRICING[self.model_name]
        input_cost = (self.total_input_tokens / 1_000_000) * pricing["input"]
        output_cost = (self.total_output_tokens / 1_000_000) * pricing["output"]

        return input_cost + output_cost

    def get_summary(self) -> Dict[str, Any]:
        """Return a summary of token usage and cost."""
        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "estimated_cost_usd": self.get_cost(),
        }

    def reset(self) -> None:
        """Reset token counters."""
        self.total_input_tokens = 0
        self.total_output_tokens = 0


# Global cost tracker instance (can be shared across modules)
cost_tracker = CostTracker()


# ========================================
# 🛡️ ERROR HANDLING / RETRY
# ========================================
def retry_on_api_error(max_retries: int = 2, delay: int = 2) -> Callable:
    """
    Decorator to retry a function on generic exceptions (e.g., transient API errors).

    Args:
        max_retries: Maximum number of retry attempts.
        delay: Delay in seconds between retries.
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Optional[Exception] = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    last_exception = exc
                    error_type = type(exc).__name__
                    if attempt < max_retries:
                        logger.warning(
                            "⚠️ %s on attempt %d/%d: %s",
                            error_type,
                            attempt + 1,
                            max_retries + 1,
                            str(exc),
                        )
                        logger.info("🔄 Retrying in %d seconds...", delay)
                        time.sleep(delay)
                    else:
                        logger.error(
                            "❌ Failed after %d attempts: %s",
                            max_retries + 1,
                            str(exc),
                        )

            if last_exception is not None:
                raise last_exception

        return wrapper

    return decorator


def safe_file_load(file_path: str, loader_class: Callable) -> List[Any]:
    """
    Safely load a file with error handling.

    Args:
        file_path: Path to file.
        loader_class: LangChain loader class (e.g., PyPDFLoader, Docx2txtLoader).

    Returns:
        List of loaded document objects, or an empty list on failure.
    """
    try:
        loader = loader_class(file_path)
        documents = loader.load()
        logger.info("✅ Loaded: %s (%d pages/sections)", os.path.basename(file_path), len(documents))
        return documents
    except FileNotFoundError:
        logger.error("❌ File not found: %s", file_path)
    except PermissionError:
        logger.error("❌ Permission denied: %s", file_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Error loading %s: %s - %s", os.path.basename(file_path), type(exc).__name__, str(exc))
    return []


# ========================================
# 📊 EXPERIMENT TRACKING (optional)
# ========================================
class ExperimentTracker:
    """Track and save hyperparameter experiment results."""

    def __init__(self, csv_filename: str = "hyperparameter_experiments.csv") -> None:
        """
        Initialize experiment tracker.
        
        Args:
            csv_filename: Name of CSV file (will be saved in the same directory as this module).
        """
        # Store the full path (inside the same directory as this module)
        self.csv_filename = os.path.join(MODULE_DIR, csv_filename)
        self.current_experiment: Dict[str, Any] = {}

        if not os.path.exists(self.csv_filename):
            with open(self.csv_filename, "w", newline="") as file:
                writer = csv.DictWriter(
                    file,
                    fieldnames=[
                        "timestamp",
                        # RAG Quality (Step 1)
                        "chunk_size",
                        "chunk_overlap",
                        "top_k",
                        "temperature",
                        # System Architecture (Step 2)
                        "relevance_threshold",
                        "fallback_threshold",
                        "off_topic_threshold",
                        "llm_confidence_threshold",
                        "sigmoid_midpoint",
                        "sigmoid_steepness",
                        # Performance
                        "avg_response_time_sec",
                        "total_tokens",
                        "estimated_cost_usd",
                        # Mode Distribution
                        "mode_rag_count",
                        "mode_fallback_count",
                        "mode_llm_domain_check_count",
                        "mode_off_topic_count",
                        "mode_no_answer_count",
                        "mode_error_count",
                        # Improvement Metrics
                        "avg_improvement_pct",
                        "improvement_std",
                        "improvement_min",
                        "improvement_max",
                        # Score Metrics
                        "avg_relevance_score",
                        "avg_rag_score",
                        "avg_llm_score",
                        "avg_rag_specificity",
                        "avg_rag_relevance",
                        "avg_rag_factuality",
                        "avg_llm_specificity",
                        "avg_llm_relevance",
                        "avg_llm_factuality",
                        "notes",
                    ],
                )
                writer.writeheader()

    def start_experiment(self, config: Dict[str, Any]) -> None:
        """Start tracking a new experiment."""
        self.current_experiment = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            **config,
            "response_times": [],
            "relevance_scores": [],
            "rag_scores_list": [],
            "llm_scores_list": [],
            "mode_counts": {
                "RAG": 0,
                "FALLBACK": 0,
                "LLM_DOMAIN_CHECK": 0,
                "OFF_TOPIC": 0,
                "NO_ANSWER": 0,
                "ERROR": 0,
            },
        }
        logger.info("🧪 Starting experiment with config: %s", config)

    def log_query(
        self,
        response_time: float,
        mode: str,
        relevance_score: Optional[float] = None,
        rag_scores: Optional[Dict[str, float]] = None,
        llm_scores: Optional[Dict[str, float]] = None,
    ) -> None:
        """
        Log a single query result into the current experiment.

        Args:
            response_time: Time taken to process the query (seconds).
            mode: Decision mode (RAG, FALLBACK, etc.).
            relevance_score: Relevance score from the query result.
            rag_scores: RAG answer quality scores (if available).
            llm_scores: LLM answer quality scores (if available).
        """
        if not self.current_experiment:
            return
        self.current_experiment["response_times"].append(response_time)
        self.current_experiment["mode_counts"][mode] += 1

        if relevance_score is not None:
            self.current_experiment["relevance_scores"].append(relevance_score)
        if rag_scores is not None:
            self.current_experiment["rag_scores_list"].append(rag_scores)
        if llm_scores is not None:
            self.current_experiment["llm_scores_list"].append(llm_scores)

    def save_experiment(
        self,
        improvement_pct: Optional[float] = None,
        improvement_std: Optional[float] = None,
        improvement_min: Optional[float] = None,
        improvement_max: Optional[float] = None,
        avg_relevance_score: Optional[float] = None,
        avg_rag_score: Optional[float] = None,
        avg_llm_score: Optional[float] = None,
        avg_rag_specificity: Optional[float] = None,
        avg_rag_relevance: Optional[float] = None,
        avg_rag_factuality: Optional[float] = None,
        avg_llm_specificity: Optional[float] = None,
        avg_llm_relevance: Optional[float] = None,
        avg_llm_factuality: Optional[float] = None,
        notes: str = "",
    ) -> None:
        """Save experiment results to CSV with detailed metrics."""
        if not self.current_experiment:
            return

        cost_summary = cost_tracker.get_summary()
        response_times: List[float] = self.current_experiment.get("response_times", [])
        avg_response_time = sum(response_times) / len(response_times) if response_times else 0.0

        # Auto-compute metrics from collected data if not explicitly provided
        relevance_scores = self.current_experiment.get("relevance_scores", [])
        if relevance_scores and avg_relevance_score is None:
            avg_relevance_score = sum(relevance_scores) / len(relevance_scores)

        rag_scores_list = self.current_experiment.get("rag_scores_list", [])
        if rag_scores_list and avg_rag_score is None:
            # Average of overall RAG scores
            import numpy as np
            overall_scores = [float(np.mean(list(s.values()))) for s in rag_scores_list]
            avg_rag_score = sum(overall_scores) / len(overall_scores)
            # Also compute component averages
            if avg_rag_specificity is None:
                avg_rag_specificity = sum(s.get("specificity", 0) for s in rag_scores_list) / len(rag_scores_list)
            if avg_rag_relevance is None:
                avg_rag_relevance = sum(s.get("relevance", 0) for s in rag_scores_list) / len(rag_scores_list)
            if avg_rag_factuality is None:
                avg_rag_factuality = sum(s.get("factuality", 0) for s in rag_scores_list) / len(rag_scores_list)

        llm_scores_list = self.current_experiment.get("llm_scores_list", [])
        if llm_scores_list and avg_llm_score is None:
            import numpy as np
            overall_scores = [float(np.mean(list(s.values()))) for s in llm_scores_list]
            avg_llm_score = sum(overall_scores) / len(overall_scores)
            # Also compute component averages
            if avg_llm_specificity is None:
                avg_llm_specificity = sum(s.get("specificity", 0) for s in llm_scores_list) / len(llm_scores_list)
            if avg_llm_relevance is None:
                avg_llm_relevance = sum(s.get("relevance", 0) for s in llm_scores_list) / len(llm_scores_list)
            if avg_llm_factuality is None:
                avg_llm_factuality = sum(s.get("factuality", 0) for s in llm_scores_list) / len(llm_scores_list)

        row = {
            "timestamp": self.current_experiment["timestamp"],
            # RAG Quality (Step 1)
            "chunk_size": self.current_experiment.get("chunk_size"),
            "chunk_overlap": self.current_experiment.get("chunk_overlap"),
            "top_k": self.current_experiment.get("top_k"),
            "temperature": self.current_experiment.get("temperature"),
            # System Architecture (Step 2)
            "relevance_threshold": self.current_experiment.get("relevance_threshold"),
            "fallback_threshold": self.current_experiment.get("fallback_threshold"),
            "off_topic_threshold": self.current_experiment.get("off_topic_threshold"),
            "llm_confidence_threshold": self.current_experiment.get("llm_confidence_threshold"),
            "sigmoid_midpoint": self.current_experiment.get("sigmoid_midpoint"),
            "sigmoid_steepness": self.current_experiment.get("sigmoid_steepness"),
            # Performance
            "avg_response_time_sec": f"{avg_response_time:.2f}",
            "total_tokens": cost_summary["total_tokens"],
            "estimated_cost_usd": f"${cost_summary['estimated_cost_usd']:.4f}",
            # Mode Distribution
            "mode_rag_count": self.current_experiment["mode_counts"]["RAG"],
            "mode_fallback_count": self.current_experiment["mode_counts"]["FALLBACK"],
            "mode_llm_domain_check_count": self.current_experiment["mode_counts"]["LLM_DOMAIN_CHECK"],
            "mode_off_topic_count": self.current_experiment["mode_counts"]["OFF_TOPIC"],
            "mode_no_answer_count": self.current_experiment["mode_counts"]["NO_ANSWER"],
            "mode_error_count": self.current_experiment["mode_counts"]["ERROR"],
            # Improvement Metrics
            "avg_improvement_pct": f"{improvement_pct:.1f}%" if improvement_pct is not None else "N/A",
            "improvement_std": f"{improvement_std:.2f}%" if improvement_std is not None else "N/A",
            "improvement_min": f"{improvement_min:.1f}%" if improvement_min is not None else "N/A",
            "improvement_max": f"{improvement_max:.1f}%" if improvement_max is not None else "N/A",
            # Score Metrics
            "avg_relevance_score": f"{avg_relevance_score:.3f}" if avg_relevance_score is not None else "N/A",
            "avg_rag_score": f"{avg_rag_score:.2f}" if avg_rag_score is not None else "N/A",
            "avg_llm_score": f"{avg_llm_score:.2f}" if avg_llm_score is not None else "N/A",
            "avg_rag_specificity": f"{avg_rag_specificity:.2f}" if avg_rag_specificity is not None else "N/A",
            "avg_rag_relevance": f"{avg_rag_relevance:.2f}" if avg_rag_relevance is not None else "N/A",
            "avg_rag_factuality": f"{avg_rag_factuality:.2f}" if avg_rag_factuality is not None else "N/A",
            "avg_llm_specificity": f"{avg_llm_specificity:.2f}" if avg_llm_specificity is not None else "N/A",
            "avg_llm_relevance": f"{avg_llm_relevance:.2f}" if avg_llm_relevance is not None else "N/A",
            "avg_llm_factuality": f"{avg_llm_factuality:.2f}" if avg_llm_factuality is not None else "N/A",
            "notes": notes,
        }

        with open(self.csv_filename, "a", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=row.keys())
            writer.writerow(row)

        logger.info("💾 Experiment saved to %s", self.csv_filename)
        logger.info(
            "📊 Summary: Avg time=%0.2fs, Cost=$%0.4f, Improvement=%s",
            avg_response_time,
            cost_summary["estimated_cost_usd"],
            f"{improvement_pct:.1f}%" if improvement_pct is not None else "N/A",
        )


# Global experiment tracker instance (optional use)
experiment_tracker = ExperimentTracker()


if __name__ == "__main__":
    logger.info("Logging utilities and trackers are ready.")
    print(f"Logs will be written to: {log_filename}")


