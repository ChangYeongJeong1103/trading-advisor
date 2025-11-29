# System Architecture

This document provides a deep dive into the architecture of the **Trading Advisor**. The system is designed to be modular, scalable, and robust using a **4-Tier Conditional RAG** approach.

## 1. High-Level Architecture Diagram

The following diagram illustrates the end-to-end data flow, from user query to the final response, highlighting the decision-making process within the Logic Layer.

```mermaid
flowchart TD
    %% Styles
    classDef user fill:#f9f,stroke:#333,stroke-width:2px;
    classDef ui fill:#e1f5fe,stroke:#0277bd,stroke-width:2px;
    classDef logic fill:#fff9c4,stroke:#fbc02d,stroke-width:2px;
    classDef data fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px;
    classDef ext fill:#f3e5f5,stroke:#7b1fa2,stroke-width:2px;

    User((User)):::user
    
    subgraph "Presentation Layer"
        UI[Streamlit Interface]:::ui
    end

    subgraph LogicLayer ["Logic Layer"]
        Controller[Query Processor]:::logic
        Scorer{Relevance Score<br/>Sigmoid}:::logic
        
        Tier1["Tier 1: RAG Mode<br/>(Score >= 0.62)"]:::logic
        Tier2["Tier 2: Smart Fallback<br/>(0.50 <= Score < 0.62)"]:::logic
        Tier3["Tier 3: LLM Feasibility Check<br/>(0.15 <= Score < 0.50)"]:::logic
        Tier4["Tier 4: Off-Topic<br/>(Score < 0.15)"]:::logic
    end

    subgraph "Data Layer"
        Chroma[(ChromaDB)]:::data
    end

    subgraph "Model Layer"
        LLM["LLM (OpenAI)"]:::ext
    end

    %% Interaction Flow
    User -->|1. Query| UI
    UI -->|2. Request| Controller
    Controller -->|3. Retrieve Context| Chroma
    Chroma -.->|4. Docs & Similarity| Controller
    
    %% Decision Logic
    Controller --> Scorer
    
    Scorer -->|High Confidence| Tier1
    Scorer -->|Ambiguous| Tier2
    Scorer -->|Low Confidence| Tier3
    Scorer -->|Irrelevant| Tier4
    
    %% Execution
    Tier1 -->|Context + Prompt| LLM
    Tier2 -->|Judge: RAG vs LLM| LLM
    Tier3 -->|Domain Filter| LLM
    Tier4 -->|Hard Reject| UI
    
    LLM -->|5. Final Answer| UI
```

## 2. High-Level Components

The application consists of three main layers:

1.  **Presentation Layer (Streamlit)**: Handles user interaction and displays visualization.
2.  **Logic Layer (Advisor)**: The core brain that orchestrates the RAG pipeline, LLM calls, and decision logic.
3.  **Data Layer (Vector Store & Docs)**: Manages the ingestion, storage, and retrieval of unstructured financial data.

## 3. Data Pipeline (Ingestion)

Before the system can answer questions, documents must be processed. This is handled by `src/document_pipeline.py`.

1.  **Loading**: Reads `.pdf` and `.docx` files from the `data/` directory.
2.  **Splitting**: Breaks documents into chunks of **800 characters** (with 100 overlap) to fit into the LLM context window.
3.  **Embedding**: Converts text chunks into vector representations using `Finance2_embedding_small_en-V1.5`.
4.  **Indexing**: Stores vectors in **ChromaDB** for fast similarity search.

```mermaid
flowchart LR
    A[PDF Reports] -->|Load| B[Raw Text]
    B -->|Split| C[Text Chunks]
    C -->|Embed| D[Vectors]
    D -->|Store| E[(ChromaDB)]
```

## 4. Query Processing Flow

When a user asks a question, the `ConditionalRAGAdvisor` (`src/advisor.py`) executes the following workflow:

### Step 1: Relevance Scoring
The system first retrieves the top-k relevant document chunks and calculates a **semantic similarity score**. This score is passed through a **Sigmoid function** to sharpen the distinction between relevant and irrelevant queries.

### Step 2: Decision Logic (The 4 Tiers)
Based on the score, one of four modes is selected:

| Tier | Condition | Action | Description |
| :--- | :--- | :--- | :--- |
| **1. RAG Mode** | `Score >= 0.62` | **Retrieve & Answer** | High confidence that documents contain the answer. |
| **2. Smart Fallback** | `0.50 <= Score < 0.62` | **Compare RAG vs LLM** | Documents might be weak. Generate both answers and let an "LLM Judge" pick the best one. |
| **3. LLM Feasibility Check** | `0.15 <= Score < 0.50` | **LLM Knowledge** | Question is likely general finance (not in docs). Ask LLM but verify it's finance-related. |
| **4. Off-Topic** | `Score < 0.15` | **Reject** | Question is clearly unrelated (e.g., "Who won the World Cup?"). |

### Step 3: Response Generation
*   **RAG Response**: Constructed using the retrieved context + System Prompt.
*   **LLM Response**: Constructed using the LLM's internal training data.

## 5. Component Details

### `src/advisor.py`
Contains the `ConditionalRAGAdvisor` class. It encapsulates the LangChain retrieval chains and the custom scoring logic.

### `src/config.py`
Centralizes all hyperparameters (thresholds, model names, chunk sizes) to ensure consistency across the application.

### `src/logging_utils.py`
Handles experiment tracking and cost estimation. It logs every query's latency, token usage, and decision mode for future optimization.
