"""Lazy Chroma access for the optional RAG add-on."""

from typing import Any

from app.core.config import settings

COLLECTION_NAME = "meeting_transcript_windows"


def get_collection() -> Any:
    """Open the persistent transcript collection."""
    try:
        import chromadb
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            'RAG dependencies are not installed. Run pip install -e ".[rag]".'
        ) from exc

    settings.chroma_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(settings.chroma_dir))
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=SentenceTransformerEmbeddingFunction(
            model_name=settings.embedding_model
        ),
        metadata={"hnsw:space": "cosine"},
    )
