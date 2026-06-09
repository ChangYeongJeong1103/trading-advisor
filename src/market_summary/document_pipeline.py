from __future__ import annotations

import glob
import os
from typing import List

from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import Docx2txtLoader, PyPDFLoader
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma

# Local imports (same folder). We avoid relative imports so that the module
# works when run via `streamlit run app_streamlit.py`.
from config import CHUNK_OVERLAP, CHUNK_SIZE, DOCS_FOLDER
from logging_utils import logger, retry_on_api_error, safe_file_load

"""
Document loading, chunking, and ChromaDB vector store creation.
This module turns your local PDF/DOCX documents into a searchable vector store that the advisor can use for RAG.
"""


def load_documents(docs_folder: str = DOCS_FOLDER):
    """
    Load all PDF and DOCX documents from the provided folder.

    Returns:
        List of LangChain Document objects.
    """
    logger.info("📂 Scanning folder: %s", docs_folder)

    if not os.path.exists(docs_folder):
        logger.error("❌ Docs folder not found: %s", docs_folder)
        raise FileNotFoundError(f"Documents folder '{docs_folder}' does not exist")

    pdf_files = glob.glob(os.path.join(docs_folder, "*.pdf"))
    docx_files = glob.glob(os.path.join(docs_folder, "*.docx"))

    logger.info("Found %d PDFs and %d DOCX files", len(pdf_files), len(docx_files))
    print(f"📂 Found {len(pdf_files)} PDF files and {len(docx_files)} DOCX files in {docs_folder}/")

    if len(pdf_files) == 0 and len(docx_files) == 0:
        logger.warning("⚠️ No PDF or DOCX files found in %s/", docs_folder)
        print(f"⚠️ WARNING: No documents found. Please add PDF or DOCX files to {docs_folder}/")

    all_documents = []

    print("\n📄 Loading PDF files...")
    for pdf_file in pdf_files:
        documents = safe_file_load(pdf_file, PyPDFLoader)
        all_documents.extend(documents)

    print("\n📄 Loading DOCX files...")
    for docx_file in docx_files:
        documents = safe_file_load(docx_file, Docx2txtLoader)
        all_documents.extend(documents)

    if len(all_documents) == 0:
        logger.error("❌ No documents were successfully loaded")
        raise ValueError(
            "Failed to load any documents. Please check file formats and permissions."
        )

    print(f"\n✅ Total documents loaded: {len(all_documents)}")
    logger.info("Successfully loaded %d document sections", len(all_documents))

    return all_documents


def split_documents(documents) -> List:
    """
    Split documents into smaller text chunks using RecursiveCharacterTextSplitter.

    Args:
        documents: List of LangChain Document objects.

    Returns:
        List of chunked Document objects.
    """
    try:
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=CHUNK_SIZE,
            chunk_overlap=CHUNK_OVERLAP,
        )
        docs = text_splitter.split_documents(documents)
        print(f"🔹 Total chunks created: {len(docs)}")
        logger.info(
            "Created %d text chunks (size=%d, overlap=%d)",
            len(docs),
            CHUNK_SIZE,
            CHUNK_OVERLAP,
        )
        return docs
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Error splitting documents: %s", str(exc))
        raise


def _detect_device() -> str:
    """
    Detect the best available device (MPS, CUDA, CPU) for the embedding model.
    """
    try:
        import torch

        if torch.backends.mps.is_available():
            print("✅ Using Apple MPS (Metal Performance Shaders) - GPU acceleration")
            return "mps"
        if torch.cuda.is_available():
            print("✅ Using CUDA (NVIDIA GPU) - GPU acceleration")
            return "cuda"
    except Exception:  # noqa: BLE001
        # If torch is not available or any error happens, fall back to CPU.
        pass

    print("ℹ️ Using CPU (slower, but works on all machines - no GPU acceleration)")
    return "cpu"


def create_embeddings_model() -> HuggingFaceEmbeddings:
    """
    Create the finance-specific HuggingFace embeddings model.

    Returns:
        Initialized HuggingFaceEmbeddings instance.
    """
    print("🔄 Loading finance-specific embedding model... (first time may take 1-2 minutes)")
    device = _detect_device()

    try:
        embeddings = HuggingFaceEmbeddings(
            model_name="baconnier/Finance2_embedding_small_en-V1.5",
            model_kwargs={"device": device},
            encode_kwargs={"normalize_embeddings": True},
        )
        print("📊 Model loaded: Finance2_embedding_small_en-V1.5 (FINANCE-SPECIFIC)")
        print("🏦 This model is trained on financial texts for better domain separation")
        logger.info("✅ Finance-specific embeddings initialized")
        print("✅ Finance-specific embedding model loaded successfully")
        return embeddings
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Error initializing finance-specific embeddings: %s", str(exc))
        print(
            "❌ Error: Could not load finance-specific embedding model. "
            "Please ensure 'sentence-transformers' is installed."
        )
        raise


@retry_on_api_error(max_retries=2, delay=3)
def create_vectorstore_with_chroma(
    docs,
    persist_directory: str = "chroma_db",
) -> Chroma:
    """
    Convert document chunks into embeddings and create a ChromaDB vector store.

    This function is wrapped with retry logic for robustness.
    """
    embeddings = create_embeddings_model()

    print(f"🔄 Embedding {len(docs)} document chunks... (this may take a while)")
    logger.info("Starting embedding process for %d chunks", len(docs))

    # Chroma will handle embedding and indexing internally
    vectorstore = Chroma.from_documents(
        documents=docs,
        embedding=embeddings,
        persist_directory=persist_directory,
    )

    print("✅ Vector store created successfully (ChromaDB)")
    print(f"📊 Total indexed chunks: {len(docs)}")
    logger.info(
        "Vector store ready with %d indexed chunks (persist_directory=%s)",
        len(docs),
        persist_directory,
    )

    return vectorstore


def build_vectorstore_pipeline(
    docs_folder: str = DOCS_FOLDER,
    persist_directory: str = "chroma_db",
) -> Chroma:
    """
    High-level helper: load documents, split into chunks, and build a ChromaDB vector store.

    This is what your Streamlit or CLI app will usually call once on startup.
    """
    raw_docs = load_documents(docs_folder)
    chunked_docs = split_documents(raw_docs)
    vectorstore = create_vectorstore_with_chroma(
        chunked_docs,
        persist_directory=persist_directory,
    )
    return vectorstore


def load_existing_vectorstore(persist_directory: str = "chroma_db") -> Chroma:
    """
    Open a previously persisted ChromaDB store WITHOUT re-embedding documents.

    Re-uses the embeddings on disk. We still build the embedding model because
    Chroma needs it to embed the *query* at retrieval time (not the documents).

    Args:
        persist_directory: Folder that contains the chroma.sqlite3 store.

    Returns:
        A Chroma vector store backed by the existing on-disk data.
    """
    embeddings = create_embeddings_model()
    vectorstore = Chroma(
        persist_directory=persist_directory,
        embedding_function=embeddings,
    )
    logger.info("♻️ Loaded existing Chroma store from %s (no re-embedding)", persist_directory)
    return vectorstore


def get_or_build_vectorstore(
    docs_folder: str = DOCS_FOLDER,
    persist_directory: str = "chroma_db",
) -> Chroma:
    """
    Default entry point for the app: load the persisted store if it exists,
    otherwise build it once from the source documents.

    This matches the intended design — the chroma_db is created ahead of time
    and shipped with the app, so normal startups just *read* it instead of
    re-embedding every PDF on each cold start.

    Args:
        docs_folder: Source PDFs (only used when we have to build from scratch).
        persist_directory: Where the Chroma store lives / will be created.

    Returns:
        A ready-to-use Chroma vector store.
    """
    # The presence of chroma.sqlite3 is our signal that a built store exists.
    sqlite_path = os.path.join(persist_directory, "chroma.sqlite3")

    if os.path.exists(sqlite_path):
        try:
            return load_existing_vectorstore(persist_directory)
        except Exception as exc:  # noqa: BLE001
            # Corrupt / version-mismatched store: fall back to a clean rebuild
            # instead of crashing the whole app.
            logger.warning(
                "⚠️ Could not load existing Chroma store (%s). Rebuilding from documents.",
                str(exc),
            )

    logger.info(
        "🆕 No usable Chroma store at %s — building it from documents (one-time).",
        persist_directory,
    )
    return build_vectorstore_pipeline(
        docs_folder=docs_folder,
        persist_directory=persist_directory,
    )


if __name__ == "__main__":
    # Simple manual test when running this file directly
    vs = get_or_build_vectorstore()
    print(vs)


