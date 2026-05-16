# Error Handling & Reliability

Robustness is critical for any production system. This system implements several layers of protection against failures.

## 1. API Reliability

External APIs (like OpenAI) can fail. We use a custom **Retry Decorator** (`@retry_on_api_error`) in `src/logging_utils.py`.

```python
@retry_on_api_error(max_retries=2, delay=3)
def call_llm():
    # ...
```

*   **Mechanism**: Exponential backoff.
*   **Coverage**: Applied to Embedding generation and LLM inference.

## 2. Safe Document Loading

File parsing is prone to errors (corrupt PDFs, bad encoding). The `document_pipeline.py` uses a `safe_file_load` wrapper:
*   Tries to load the file.
*   If it fails, logs the error and **skips the file** instead of crashing the entire pipeline.
*   Ensures the system starts even when 1 out of 100 files is bad.

## 3. "No Answer" Safety Net

Even if a query passes the relevance check, the LLM might hallucinate. We implemented a final **Self-Confidence Check**.

1.  The LLM generates an answer.
2.  The system asks the LLM: *"Rate your confidence in this answer from 0-10."*
3.  If **Confidence < 5**, the answer is discarded and replaced with:
    > "Information not available. This question cannot be answered with confidence."

## 4. Logging & Monitoring

Every interaction is logged to `logs/` with detailed metadata:
*   **Latency**: How long did the query take?
*   **Cost**: Token usage estimation.
*   **Decision Mode**: Which tier was selected?

This allows post-mortem analysis of any incorrect decisions.

