from __future__ import annotations

import json
import math
import re
import time
from typing import Any, Dict

import numpy as np
from langchain.chains import RetrievalQA
from langchain.prompts import PromptTemplate
from langchain_openai import ChatOpenAI

# Local imports (same folder). We use non-relative imports so that this module
# can be used when running `streamlit run app_streamlit.py` from the src directory
from config import (
    FALLBACK_THRESHOLD,
    LLM_CONFIDENCE_THRESHOLD,
    LLM_MODEL,
    LLM_TEMPERATURE,
    OFF_TOPIC_THRESHOLD,
    RELEVANCE_THRESHOLD,
    SIGMOID_MIDPOINT,
    SIGMOID_STEEPNESS,
    TOP_K_DOCUMENTS,
    get_effective_llm_temperature,
)
from logging_utils import cost_tracker, experiment_tracker, logger, retry_on_api_error

"""
Core Conditional RAG Advisor logic.

This module contains:
- ConditionalRAGAdvisor: main decision engine (RAG / Fallback / LLM / Off-topic)
- monitored_query: wrapper adding monitoring and error handling
- ask: simple helper that uses a global advisor instance (if created)

These functions are currently used by the Streamlit app and are designed so they
can also be reused by other frontends in the future (e.g., FastAPI or CLI tools).
"""


def create_llm(model_name: str, temperature: float) -> ChatOpenAI:
    """
    Create the chat model used by the advisor.

    GPT-5 family models only accept the default temperature. For this RAG app,
    use minimal reasoning effort so the interface stays responsive.
    """
    is_gpt5_family = model_name.lower().startswith("gpt-5")
    effective_temperature = (
        get_effective_llm_temperature(model_name)
        if is_gpt5_family
        else temperature
    )
    if is_gpt5_family:
        return ChatOpenAI(
            model=model_name,
            temperature=effective_temperature,
            model_kwargs={"reasoning_effort": "minimal"},
        )

    return ChatOpenAI(model=model_name, temperature=effective_temperature)


class ConditionalRAGAdvisor:
    """
    Conditional RAG system with a 4-tier logic to decide the best response strategy.

    Decision logic based on relevance score:
    - TIER 1 (High Relevance): Use RAG directly (documents + LLM).
    - TIER 2 (Medium Relevance): Smart Fallback (compare RAG vs LLM-only).
    - TIER 3 (Low Relevance): LLM with domain-specific check.
    - TIER 4 (Very Low Relevance): Auto-reject as off-topic.
    """

    def __init__(
        self,
        vectorstore: Any,
        llm_model: str = LLM_MODEL,
        relevance_threshold: float = RELEVANCE_THRESHOLD,
        fallback_threshold: float = FALLBACK_THRESHOLD,
        off_topic_threshold: float = OFF_TOPIC_THRESHOLD,
        top_k: int = TOP_K_DOCUMENTS,
        llm_confidence_threshold: int = LLM_CONFIDENCE_THRESHOLD,
        temperature: float = LLM_TEMPERATURE,
        sigmoid_midpoint: float = SIGMOID_MIDPOINT,
        sigmoid_steepness: float = SIGMOID_STEEPNESS,
    ) -> None:
        """Initialize the Conditional RAG Advisor with Smart Fallback."""
        self.vectorstore = vectorstore
        self.relevance_threshold = relevance_threshold
        self.fallback_threshold = fallback_threshold
        self.off_topic_threshold = off_topic_threshold
        self.llm_confidence_threshold = llm_confidence_threshold
        self.top_k = top_k
        self.sigmoid_midpoint = sigmoid_midpoint
        self.sigmoid_steepness = sigmoid_steepness

        # Initialize LLM and cost tracking with the same model.
        self.llm = create_llm(model_name=llm_model, temperature=temperature)
        cost_tracker.set_model(llm_model)

        # Custom prompt for RAG chain: force using context documents
        rag_prompt_template = """
You are a financial analyst assistant. Answer the question based on the provided context documents.

INSTRUCTIONS:
1. Use the information from the context below to answer the question.
2. Synthesize information from the context - you don't need exact quotes or lists.
3. If the context discusses related topics, provide a helpful answer based on what's available.
4. Only say "I don't have enough information" if the context is truly unrelated to the question.
5. DO NOT refuse to answer based on date/time restrictions - the documents may contain information about any time period.
6. Focus on being helpful - provide the best answer you can from the available context.

Context: {context}

Question: {question}

Answer:
""".strip()

        rag_prompt = PromptTemplate(
            template=rag_prompt_template,
            input_variables=["context", "question"],
        )

        # Create RAG chain with custom prompt
        self.rag_chain = RetrievalQA.from_chain_type(
            llm=self.llm,
            chain_type="stuff",
            retriever=vectorstore.as_retriever(search_kwargs={"k": top_k}),
            chain_type_kwargs={"prompt": rag_prompt},
        )

    def get_relevance_score(self, query: str) -> float:
        """
        Calculate the RELEVANCE SCORE for a query (0.0 - 1.0).

        Steps:
        1. Retrieve top-K documents with similarity scores.
        2. Compute average similarity.
        3. Apply sigmoid transformation to sharpen separation between
           finance vs non-finance queries.
        """
        docs_with_scores = self.vectorstore.similarity_search_with_score(
            query,
            k=self.top_k,
        )
        if not docs_with_scores:
            return 0.0

        scores = []
        # Detect if vectorstore is Chroma (which returns distances)
        is_chroma = "chroma" in self.vectorstore.__class__.__name__.lower()

        for _, score in docs_with_scores:
            if is_chroma:
                # Chroma default: L2 distance (lower is better)
                # Convert to Similarity (higher is better, 0-1)
                # Formula for normalized vectors: Similarity = 1 - (distance^2 / 2)
                converted_score = 1.0 - (score**2 / 2.0)
                scores.append(max(0.0, min(1.0, converted_score)))
            else:
                # Assume standard similarity (higher is better)
                scores.append(score)

        avg_score = sum(scores) / len(scores)

        # Sigmoid transformation to amplify separation
        sigmoid_score = 1.0 / (
            1.0 + math.exp(-self.sigmoid_steepness * (avg_score - self.sigmoid_midpoint))
        )
        return float(sigmoid_score)

    def llm_with_domain_check(self, query: str) -> str:
        """
        Call LLM with domain-aware prompt to handle borderline relevance queries.

        Used when relevance score is low but not zero (OFF_TOPIC_THRESHOLD ≤ score < FALLBACK_THRESHOLD).
        """
        domain_prompt = f"""
You are a STOCK TRADING ADVISOR specializing in financial markets and trading.

IMPORTANT RULES:
1. You can ONLY answer questions about:
   - Stock markets, equities, trading strategies
   - Economic policies, Federal Reserve decisions, interest rates
   - Company earnings, financial analysis, valuations
   - Market trends, predictions, technical/fundamental analysis
   - Investment advice, portfolio management

2. If the question is about OTHER TOPICS (medicine, physics, sports, cooking, history, etc.):
   → Respond EXACTLY: "I'm a stock trading advisor and can only answer questions about financial markets and trading. Your question about [TOPIC] is outside my expertise. Please ask about stock market or trading-related topics."

3. If you're unsure whether it's stock-related:
   → If it has ANY connection to finance/markets → answer it
   → If it's clearly unrelated → reject it politely

Question: {query}

Answer:
""".strip()

        try:
            response = self.llm.invoke(domain_prompt)
            answer = response.content if hasattr(response, "content") else str(response)
            logger.info("🔍 Domain check LLM response (truncated): %s", answer[:100])
            return answer
        except Exception as exc:  # noqa: BLE001
            logger.error("❌ Error in domain check LLM: %s", str(exc))
            return "I apologize, but I encountered an error. Please try again."

    def assess_llm_confidence(self, question: str, answer: str) -> int:
        """
        Ask the LLM to rate its own confidence in the answer (0–10).
        """
        confidence_prompt = f"""
Rate your confidence in the following answer on a scale of 0-10:

Question: {question}

Your Answer: {answer}

Instructions:
- If you provided specific, factual information you're confident about: 7-10
- If you provided general knowledge but aren't fully certain: 4-6
- If you don't have enough information or are guessing: 0-3

Respond with ONLY a single number from 0 to 10, nothing else.

Confidence score:
""".strip()

        try:
            response = self.llm.invoke(confidence_prompt).content.strip()
            match = re.search(r"\d+", response)
            if match:
                confidence = int(match.group())
                return max(0, min(10, confidence))
            return 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("Confidence assessment failed (%s), assuming low confidence", exc)
            return 0

    def score_answer(self, question: str, answer: str) -> Dict[str, float]:
        """
        Calculate RAG SCORE or LLM SCORE (1-10) for an answer.

        Evaluates specificity, relevance, and factuality via LLM-as-a-judge.
        """
        evaluation_prompt = f"""
You are an expert evaluator. Score the following answer on these three criteria (scale 1-10):

1. SPECIFICITY: How specific and detailed is the answer? (1 = vague, 10 = very specific with details)
2. RELEVANCE: How relevant is the answer to the question? (1 = off-topic, 10 = directly answers question)
3. FACTUALITY: Does the answer contain verifiable facts and data? (1 = no facts/opinions only, 10 = rich with facts and data)

Question: {question}

Answer: {answer}

Respond ONLY with a JSON object in this exact format (no other text):
{{"specificity": <score>, "relevance": <score>, "factuality": <score>}}
""".strip()

        try:
            response = self.llm.invoke(evaluation_prompt).content
            start_idx = response.find("{")
            end_idx = response.rfind("}") + 1
            json_str = response[start_idx:end_idx]
            scores = json.loads(json_str)
            return scores
        except Exception:
            # Fallback if parsing fails
            return {"specificity": 5.0, "relevance": 5.0, "factuality": 5.0}

    def query(self, question: str) -> Dict[str, Any]:
        """
        Main query method implementing the 4-tier decision system.

        Returns:
            dict with answer, mode, relevance_score, llm_confidence, etc.
        """
        relevance_score = self.get_relevance_score(question)

        result: Dict[str, Any] = {
            "answer": None,
            "mode": None,
            "relevance_score": relevance_score,
            "llm_confidence": None,
            "retrieved_docs": [],
            "fallback_source": None,
            "rag_scores": None,
            "llm_scores": None,
        }

        # TIER 1: RAG MODE
        if relevance_score >= self.relevance_threshold:
            result["mode"] = "RAG"
            result["answer"] = self.rag_chain.invoke({"query": question})["result"]
            result["retrieved_docs"] = self.vectorstore.similarity_search(
                question,
                k=self.top_k,
            )

        # TIER 2: SMART FALLBACK
        elif relevance_score >= self.fallback_threshold:
            result["mode"] = "FALLBACK"

            rag_answer = self.rag_chain.invoke({"query": question})["result"]
            result["retrieved_docs"] = self.vectorstore.similarity_search(
                question,
                k=self.top_k,
            )

            llm_prompt = f"Answer the following question about stock market and trading:\n\nQuestion: {question}\n\nAnswer:"
            llm_answer = self.llm.invoke(llm_prompt).content

            result["llm_confidence"] = self.assess_llm_confidence(question, llm_answer)

            # Always compute scores for experiment tracking (even if we skip comparison)
            rag_scores = self.score_answer(question, rag_answer)
            llm_scores = self.score_answer(question, llm_answer)
            result["rag_scores"] = rag_scores
            result["llm_scores"] = llm_scores

            if result["llm_confidence"] < self.llm_confidence_threshold:
                # LLM confidence too low → use RAG without comparison
                result["answer"] = rag_answer
                result["fallback_source"] = "RAG"
            else:
                # LLM confidence sufficient → compare scores
                rag_overall = float(np.mean(list(rag_scores.values())))
                llm_overall = float(np.mean(list(llm_scores.values())))

                if rag_overall >= llm_overall:
                    result["answer"] = rag_answer
                    result["fallback_source"] = "RAG"
                else:
                    result["answer"] = llm_answer
                    result["fallback_source"] = "LLM"

        # TIER 3 & 4: LOWER RELEVANCE
        else:
            if relevance_score < self.off_topic_threshold:
                # TIER 4: OFF-TOPIC AUTO-REJECT
                result["mode"] = "OFF_TOPIC"
                result[
                    "answer"
                ] = (
                    "I'm a stock trading advisor and can only answer questions about financial markets and trading. "
                    "Your question appears to be outside my area of expertise. "
                    "Please ask about stock market, trading, economic policies, or financial analysis."
                )
                logger.info(
                    "🚫 Off-topic rejection: relevance=%0.3f < %0.3f",
                    relevance_score,
                    self.off_topic_threshold,
                )
            else:
                # TIER 3: LLM DOMAIN CHECK
                result["mode"] = "LLM_DOMAIN_CHECK"
                result["answer"] = self.llm_with_domain_check(question)
                result["llm_confidence"] = self.assess_llm_confidence(
                    question,
                    result["answer"],
                )

                if result["llm_confidence"] < self.llm_confidence_threshold:
                    result["mode"] = "NO_ANSWER"
                    result[
                        "answer"
                    ] = f"Information not available. This question cannot be answered with confidence. (LLM confidence: {result['llm_confidence']}/10)"

                logger.info(
                    "🔍 Domain check mode: relevance=%0.3f, confidence=%s",
                    relevance_score,
                    result["llm_confidence"],
                )

        return result


def monitored_query(
    advisor: ConditionalRAGAdvisor,
    question: str,
    track_cost: bool = True,
    log_to_experiment: bool = True,
) -> Dict[str, Any]:
    """
    Enhanced query function with automatic monitoring and error handling.

    Wraps advisor.query() with:
    - Timing (response_time)
    - Optional token/cost tracking
    - Optional experiment logging
    """
    start_time = time.time()

    try:
        logger.info("🔍 Processing query: %s...", question[:50])

        @retry_on_api_error(max_retries=2, delay=2)
        def query_with_retry() -> Dict[str, Any]:
            return advisor.query(question)

        result = query_with_retry()

        response_time = time.time() - start_time
        result["response_time"] = response_time

        if track_cost:
            input_tokens, output_tokens = cost_tracker.add_tokens(
                question,
                result["answer"],
            )
            result["input_tokens"] = input_tokens
            result["output_tokens"] = output_tokens

        mode = result["mode"]
        logger.info(
            "✅ Query completed in %0.2fs | Mode: %s | Relevance: %0.3f",
            response_time,
            mode,
            result.get("relevance_score", 0.0),
        )

        if log_to_experiment and experiment_tracker.current_experiment:
            experiment_tracker.log_query(
                response_time=response_time,
                mode=mode,
                relevance_score=result.get("relevance_score"),
                rag_scores=result.get("rag_scores"),
                llm_scores=result.get("llm_scores"),
            )

        return result

    except Exception as exc:  # noqa: BLE001
        response_time = time.time() - start_time
        logger.error(
            "❌ Query failed after %0.2fs: %s - %s",
            response_time,
            type(exc).__name__,
            str(exc),
        )
        return {
            "mode": "ERROR",
            "answer": f"Sorry, an error occurred while processing your question: {str(exc)}",
            "relevance_score": 0.0,
            "response_time": response_time,
            "error": str(exc),
        }


def create_advisor(vectorstore: Any) -> ConditionalRAGAdvisor:
    """
    Helper to create a ConditionalRAGAdvisor using the global config values.
    """
    return ConditionalRAGAdvisor(vectorstore=vectorstore)


# Optional global advisor instance (to be set by the app)
_GLOBAL_ADVISOR: ConditionalRAGAdvisor | None = None


def set_global_advisor(advisor: ConditionalRAGAdvisor) -> None:
    """Set the global advisor used by the `ask` helper."""
    global _GLOBAL_ADVISOR
    _GLOBAL_ADVISOR = advisor


def ask(question: str) -> Dict[str, Any]:
    """
    Convenience helper: use the global advisor (if set) with monitoring.

    Example:
        from document_pipeline import build_vectorstore_pipeline
        from advisor import create_advisor, set_global_advisor, ask

        vs = build_vectorstore_pipeline()
        adv = create_advisor(vs)
        set_global_advisor(adv)
        result = ask("What happened in the market?")
    """
    if _GLOBAL_ADVISOR is None:
        raise RuntimeError(
            "Global advisor is not set. Call set_global_advisor(advisor) first."
        )
    return monitored_query(_GLOBAL_ADVISOR, question)


if __name__ == "__main__":
    print("This module defines the ConditionalRAGAdvisor. Import it from your app code.")


