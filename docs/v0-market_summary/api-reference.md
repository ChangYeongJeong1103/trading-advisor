# API Reference

While the primary interface is Streamlit, the core logic is designed as a reusable Python library.

## `ConditionalRAGAdvisor`

The main class located in `src/advisor.py`.

### `__init__(self, vectorstore, llm_model=...)`
Initializes the advisor with a connected Vector Store and LLM configuration.

### `query(self, question: str) -> Dict`
The primary entry point.

**Arguments:**
*   `question` (str): The user's query.

**Returns:**
```json
{
  "answer": "The generated response string...",
  "mode": "RAG" | "SMART_FALLBACK" | "LLM_FEASIBILITY_CHECK" | "OFF_TOPIC" | "NO_ANSWER",
  "relevance_score": 0.85,
  "llm_confidence": 7,
  "retrieved_docs": [...]
}
```

## `DocumentPipeline`

Located in `src/document_pipeline.py`.

### `load_documents(docs_folder: str) -> List[Document]`
Scans the folder (default: `data/`) for PDF/DOCX files and returns LangChain Document objects.

### `build_vectorstore_pipeline() -> Chroma`
Orchestrates the full loading -> splitting -> embedding -> indexing process.

## Configuration (`src/config.py`)

All hyperparameters are defined in `config.py`. See [hyperparameter-tuning.md](hyperparameter-tuning.md) for detailed tuning process.

| Constant | Default | Description |
| :--- | :--- | :--- |
| `CHUNK_SIZE` | 800 | Character count for text splitting. |
| `CHUNK_OVERLAP` | 100 | Overlap between chunks. |
| `TOP_K_DOCUMENTS` | 4 | Number of documents to retrieve. |
| `LLM_TEMPERATURE` | 0.2 | LLM temperature (lower = more focused). |
| `SIGMOID_MIDPOINT` | 0.5 | Sigmoid transformation center point. |
| `SIGMOID_STEEPNESS` | 12 | Sigmoid curve steepness. |
| `RELEVANCE_THRESHOLD` | 0.62 | Minimum score for RAG mode. |
| `FALLBACK_THRESHOLD` | 0.50 | Threshold for Smart Fallback mode. |
| `OFF_TOPIC_THRESHOLD` | 0.15 | Threshold for off-topic rejection. |
| `LLM_CONFIDENCE_THRESHOLD` | 5 | Minimum LLM confidence for answering. |
| `LLM_MODEL` | "gpt-5-mini" | OpenAI model used. |
| `DOCS_FOLDER` | "data" | Directory containing financial reports (PDF/DOCX). |

