import os
from typing import Any, Dict, Optional

import streamlit as st

# Import local modules within the same `deploy` package/folder.
# This makes the app work even when run directly as a script:
#   streamlit run src/app_streamlit.py
from config import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    DOCS_FOLDER,
    FALLBACK_THRESHOLD,
    LLM_CONFIDENCE_THRESHOLD,
    LLM_MODEL,
    OFF_TOPIC_THRESHOLD,
    RELEVANCE_THRESHOLD,
    SIGMOID_MIDPOINT,
    SIGMOID_STEEPNESS,
    TOP_K_DOCUMENTS,
    get_effective_llm_temperature,
)
from document_pipeline import get_or_build_vectorstore
from advisor import ConditionalRAGAdvisor, create_advisor, monitored_query
from logging_utils import cost_tracker, experiment_tracker, logger


# ================================
# 🔧 Utility: Cached initializers
# ================================
@st.cache_resource(show_spinner="🔄 Loading vector store... This runs only once.")
def get_vectorstore():
    """
    Load the persisted ChromaDB store if it exists, otherwise build it once.

    Cached by Streamlit, so it runs only once per server process (shared across
    all sessions). Because we ship a prebuilt chroma_db, normal startups just
    read it instead of re-embedding every PDF.
    """
    # You can change the persist directory if needed
    persist_dir = "chroma_db"
    vectorstore = get_or_build_vectorstore(
        docs_folder=DOCS_FOLDER,
        persist_directory=persist_dir,
    )
    return vectorstore


@st.cache_resource(show_spinner="🤖 Initializing Trading Advisor...")
def get_advisor() -> ConditionalRAGAdvisor:
    """
    Initialize the ConditionalRAGAdvisor using the cached vector store.
    """
    vectorstore = get_vectorstore()
    advisor = create_advisor(vectorstore)
    return advisor


def ensure_api_key() -> bool:
    """
    Check if OPENAI_API_KEY is available in the environment.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        st.error(
            "❌ OPENAI_API_KEY not found. Please set it in a `.env` file or your environment "
            "before running the app."
        )
        return False
    return True


def get_experiment_config() -> Dict[str, Any]:
    """Build experiment configuration from current config.py values."""
    return {
        "chunk_size": CHUNK_SIZE,
        "chunk_overlap": CHUNK_OVERLAP,
        "top_k": TOP_K_DOCUMENTS,
        "temperature": get_effective_llm_temperature(),
        "relevance_threshold": RELEVANCE_THRESHOLD,
        "fallback_threshold": FALLBACK_THRESHOLD,
        "off_topic_threshold": OFF_TOPIC_THRESHOLD,
        "llm_confidence_threshold": LLM_CONFIDENCE_THRESHOLD,
        "sigmoid_midpoint": SIGMOID_MIDPOINT,
        "sigmoid_steepness": SIGMOID_STEEPNESS,
    }


def ensure_experiment_started() -> None:
    """
    Ensure that a hyperparameter experiment session is started for this Streamlit session.

    Note: Because the experiment_tracker is a module-level global that gets reset on each
    Streamlit rerun, we need to check if current_experiment is empty AND reinitialize if needed.
    """
    # Check if experiment_tracker has an active experiment.
    # If not, start one (this happens on every Streamlit rerun because module globals reset).
    if not experiment_tracker.current_experiment:
        experiment_tracker.start_experiment(get_experiment_config())
        logger.info("🔄 Experiment session initialized (or restored on rerun)")


def reset_experiment_tracker() -> None:
    """
    Force reset the experiment tracker to start fresh.
    
    This is called after saving experiment data to ensure the next batch
    of queries doesn't accumulate with previous ones.
    """
    experiment_tracker.start_experiment(get_experiment_config())
    logger.info("🔄 Experiment tracker reset for new batch of queries")


# ================================
# 🎨 Streamlit UI
# ================================
def main() -> None:
    """
    Main entry point for the Streamlit Trading Advisor app.
    """
    st.set_page_config(
        page_title="AI Stock Market Analyst - Trading Advisor",
        page_icon="📈",
        layout="wide",
    )

    st.title("📈 AI Stock Market Analyst - Trading Advisor")
    st.markdown(
        """
        This app is a **Trading Advisor** built on top of a **Conditional RAG + Smart Fallback**
        architecture.

        - Automatically decides between **document-based RAG** and **LLM-only knowledge**
        - Uses a **relevance score + sigmoid** to separate finance vs non‑finance queries
        - In the **Smart Fallback** zone, compares RAG vs LLM answers and selects the better one
        """
    )

    # Check API key
    if not ensure_api_key():
        return

    # Start an experiment tracking session for this Streamlit run (once per session).
    ensure_experiment_started()

    # Sidebar: Configuration overview
    with st.sidebar:
        st.header("⚙️ System Configuration")
        st.markdown("**LLM & Documents**")
        st.write(f"- Model: `{LLM_MODEL}`")
        st.write(f"- Docs folder: `{DOCS_FOLDER}`")

        st.markdown("---")
        st.markdown("**RAG Quality (Step 1)**")
        st.write(f"- Chunk size: `{CHUNK_SIZE}`")
        st.write(f"- Chunk overlap: `{CHUNK_OVERLAP}`")
        st.write(f"- Top-K documents: `{TOP_K_DOCUMENTS}`")
        temperature_display = f"{get_effective_llm_temperature()}"
        if LLM_MODEL.lower().startswith("gpt-5"):
            temperature_display += " (GPT-5 default)"
        st.write(f"- LLM temperature: `{temperature_display}`")

        st.markdown("---")
        st.markdown("**System-level (Step 2)**")
        st.write(f"- Relevance threshold: `{RELEVANCE_THRESHOLD}`")
        st.write(f"- Fallback threshold: `{FALLBACK_THRESHOLD}`")
        st.write(f"- Off-topic threshold: `{OFF_TOPIC_THRESHOLD}`")
        st.write(f"- LLM confidence threshold: `{LLM_CONFIDENCE_THRESHOLD}`")
        st.write(f"- Sigmoid midpoint: `{SIGMOID_MIDPOINT}`")
        st.write(f"- Sigmoid steepness: `{SIGMOID_STEEPNESS}`")

        st.markdown("---")
        st.markdown("**💰 Cost summary (session)**")
        summary = cost_tracker.get_summary()
        st.write(f"- Total tokens: `{summary['total_tokens']}`")
        st.write(f"- Est. cost (USD): `${summary['estimated_cost_usd']:.4f}`")

        st.markdown("---")
        st.markdown("**🧪 Experiment data logging**")
        st.caption(
            "Streamlit queries are tracked in-memory for this session. "
            "Click the button below to save hyperparameters, performance metrics, "
            "and mode distribution to `hyperparameter_experiments.csv`."
        )
        if st.button("📊 Save experiment data", key="save_experiment_data"):
            try:
                # Save current experiment metrics to CSV. Most detailed metrics are optional
                # and will be marked as 'N/A' if not provided.
                experiment_tracker.save_experiment(
                    notes="Experiment data collected via Streamlit app session."
                )
                st.success("✅ Experiment data saved to hyperparameter_experiments.csv")
                
                # Reset the experiment tracker for the next batch of queries
                # This ensures each "save" captures only queries since the last save
                reset_experiment_tracker()
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to save experiment snapshot: %s", str(exc))
                st.error(f"Failed to save experiment snapshot: {exc}")

    # Main input area
    st.subheader("💬 Ask the Trading Advisor")
    default_question = "Summarize the main points of the US stock market in October 2025."
    question = st.text_area(
        "Enter your question (stock market, trading, macro events, etc.)",
        value=default_question,
        height=100,
    )

    col1, col2 = st.columns([1, 3])
    with col1:
        clicked = st.button("Get Answer", type="primary")

    # Initialize advisor lazily (only when needed)
    advisor: Optional[ConditionalRAGAdvisor] = None
    if clicked:
        if not question.strip():
            st.warning("⚠️ Please enter a question first.")
            return

        with st.spinner("🧠 Thinking..."):
            try:
                advisor = get_advisor()
                result = monitored_query(advisor, question)
            except Exception as exc:  # noqa: BLE001
                logger.error("Error in Streamlit app: %s", str(exc))
                st.error(f"An error occurred: {exc}")
                return

        # Display mode & scores
        mode = result.get("mode")
        relevance = result.get("relevance_score", 0.0)
        llm_conf = result.get("llm_confidence")
        response_time = result.get("response_time")
        input_tokens = result.get("input_tokens")
        output_tokens = result.get("output_tokens")

        # Mode badge
        if mode == "RAG":
            mode_label = "🟢 RAG Mode (Using Documents)"
        elif mode == "FALLBACK":
            source = result.get("fallback_source")
            if source == "RAG":
                mode_label = "🟢 Smart Fallback → RAG Selected"
            else:
                mode_label = "🔵 Smart Fallback → LLM Selected"
        elif mode == "LLM_DOMAIN_CHECK":
            mode_label = "🟠 LLM Domain Check"
        elif mode == "OFF_TOPIC":
            mode_label = "🔴 Off-Topic (Auto Rejected)"
        elif mode == "NO_ANSWER":
            mode_label = "🔴 No Answer (Low Confidence)"
        elif mode == "ERROR":
            mode_label = "❌ System Error"
        else:
            mode_label = f"⚪ Unknown Mode: {mode}"

        st.markdown("---")
        st.markdown(f"**Mode:** {mode_label}")
        st.markdown(f"**Relevance Score:** `{relevance:.3f}`")
        if llm_conf is not None:
            st.markdown(f"**LLM Confidence:** `{llm_conf}/10`")
        if response_time is not None:
            st.markdown(f"**Response Time:** `{response_time:.2f} s`")
        if input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
            st.markdown(f"**Tokens (this query):** `{total_tokens}` (`{input_tokens}` in, `{output_tokens}` out)")

        # Answer box
        st.subheader("📘 Answer")
        st.write(result.get("answer", "No answer returned."))

        # Retrieved documents (for RAG only)
        if mode in {"RAG", "FALLBACK"} and result.get("retrieved_docs"):
            with st.expander("📄 Retrieved Documents (top 2 preview)"):
                for i, doc in enumerate(result["retrieved_docs"][:2], start=1):
                    preview = doc.page_content[:400].replace("\n", " ")
                    st.markdown(f"**Document {i}:**")
                    st.write(preview + " ...")

        # Fallback score comparison
        if mode == "FALLBACK" and result.get("rag_scores") and result.get("llm_scores"):
            import numpy as np  # local import to keep global namespace clean

            rag_scores = result["rag_scores"]
            llm_scores = result["llm_scores"]
            rag_overall = float(np.mean(list(rag_scores.values())))
            llm_overall = float(np.mean(list(llm_scores.values())))

            st.markdown("---")
            st.subheader("📊 Smart Fallback Score Comparison")
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**RAG Answer**")
                st.write(rag_scores)
                st.markdown(f"**Overall:** `{rag_overall:.2f}`")
            with c2:
                st.markdown("**LLM Answer**")
                st.write(llm_scores)
                st.markdown(f"**Overall:** `{llm_overall:.2f}`")


if __name__ == "__main__":
    main()


