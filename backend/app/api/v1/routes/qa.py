"""Grounded Q&A over indexed meeting transcript windows."""

from fastapi import APIRouter, HTTPException, status

from app.schemas.transcript import AnswerOut, CitationOut, QuestionIn
from app.services.rag.retriever import answer_question

router = APIRouter(prefix="/qa", tags=["qa"])


@router.post("", response_model=AnswerOut)
def ask(payload: QuestionIn) -> AnswerOut:
    """Answer a question over previous meetings."""
    try:
        answer = answer_question(
            payload.question, top_k=payload.top_k, meeting_id=payload.meeting_id
        )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    # ``Citation`` is a service-layer dataclass; map it explicitly at the API
    # boundary rather than relying on Pydantic to infer attributes from it.
    return AnswerOut(
        answer=answer.text,
        citations=[
            CitationOut(
                meeting_id=citation.meeting_id,
                meeting_title=citation.meeting_title,
                speaker=citation.speaker,
                start_ms=citation.start_ms,
                text=citation.text,
            )
            for citation in answer.citations
        ],
    )
