"""Retrieve evidence windows and answer only from that evidence."""

from dataclasses import dataclass
import logging
import uuid

from app.core.config import settings
from app.services.minutes.providers import get_provider
from app.services.rag.schemas import QuestionAnswer
from app.services.rag.store import get_collection

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Citation:
    meeting_id: str
    meeting_title: str | None
    speaker: str | None
    start_ms: int
    text: str


@dataclass(slots=True)
class Answer:
    text: str
    citations: list[Citation]


def answer_question(
    question: str, *, top_k: int = 8, meeting_id: uuid.UUID | None = None
) -> Answer:
    """Answer a question from cited windows, optionally limited to one meeting."""
    query = question.strip()
    if not query:
        raise ValueError("Question must not be blank.")
    query_args: dict[str, object] = {
        "query_texts": [query],
        "n_results": top_k,
        "include": ["documents", "metadatas"],
    }
    if meeting_id is not None:
        query_args["where"] = {"meeting_id": str(meeting_id)}
    result = get_collection().query(**query_args)
    documents = result.get("documents", [[]])[0] or []
    metadatas = result.get("metadatas", [[]])[0] or []
    if not documents:
        return Answer(
            "I could not find an indexed meeting transcript that answers that question.", []
        )

    evidence = "\n\n".join(
        f"[Evidence {number}]\n{document}" for number, document in enumerate(documents, 1)
    )
    try:
        response = get_provider().complete(
            system=("Answer only from the supplied meeting evidence. Do not infer missing facts. "
                    "If it does not answer the question, say so and return no citations."),
            prompt=f"Question: {query}\n\nEvidence:\n{evidence}",
            schema=QuestionAnswer,
            model=settings.grounding_model,
            effort="low",
            max_tokens=1_000,
        )
    except Exception as exc:
        # Network, account, and retired-model errors belong to the configured
        # provider, not to a malformed user question. The API maps this to 503
        # so the extension can show a useful error rather than a raw 500.
        logger.exception("Configured answer provider failed while answering a RAG question.")
        raise RuntimeError("The configured answer provider could not complete the request.") from exc
    if response is None:
        return Answer("I could not answer that from the retrieved meeting evidence.", [])
    indices = sorted({number for number in response.citations if 1 <= number <= len(documents)})
    if not indices:
        return Answer("I could not answer that from the retrieved meeting evidence.", [])
    citations = [
        Citation(
            meeting_id=str(metadatas[number - 1]["meeting_id"]),
            meeting_title=str(metadatas[number - 1].get("meeting_title") or "") or None,
            speaker=str(metadatas[number - 1].get("speakers") or "") or None,
            start_ms=int(metadatas[number - 1]["start_ms"]),
            text=documents[number - 1],
        )
        for number in indices
    ]
    return Answer(response.answer, citations)
