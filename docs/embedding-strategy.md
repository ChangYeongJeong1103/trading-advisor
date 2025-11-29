# Embedding Strategy

This project deliberately moves away from generic embedding models (like OpenAI's `text-embedding-3-small`) in favor of a **domain-specific financial model**.

## The Model: `Finance2_embedding_small_en-V1.5`

We utilized the open-source model **baconnier/Finance2_embedding_small_en-V1.5** hosted on Hugging Face.

### Why specific embeddings?

In generic models, the word "Apple" might be semantically close to "fruit", "banana", or "farm" without clear distinction. In a financial embedding space, "Apple" is strongly clustered with "AAPL stock", "earnings report", "market cap", "tech sector", and "dividend yield".

| Feature | Generic Model (e.g., OpenAI Ada) | Finance-Specific Model |
| :--- | :--- | :--- |
| **Training Data** | Common Crawl, Wikipedia, General Web | Financial Reports, SEC Filings, Earnings Calls |
| **Jargon Understanding** | Weak (e.g., "Bull" = Animal) | Strong (e.g., "Bull" = Upward Trend) |
| **Relevance Scoring** | Average for finance queries | **High Precision** for finance queries |
| **Cost** | Paid API calls | **Free & Local** (runs in Docker) |

## Implementation Details

The embedding model is initialized in `src/document_pipeline.py` using `LangChain`'s HuggingFace integration.

```python
device = _detect_device()  # Automatically selects MPS (Apple) / CUDA (NVIDIA) / CPU
embeddings = HuggingFaceEmbeddings(
    model_name="baconnier/Finance2_embedding_small_en-V1.5",
    model_kwargs={"device": device},
    encode_kwargs={"normalize_embeddings": True}
)
```

### Performance Impact

Switching to this specific model resulted in:
*   **Better clustering** of related financial concepts.
*   **Higher separation** between finance and non-finance queries (crucial for our conditional logic).
*   **Zero API latency** for embeddings since it runs on-device (no external API calls).

