"""RAG Q&A routes — the phase-2 add-on.

Stubbed out deliberately. Q&A over past meetings inherits every error in the
transcripts underneath it, so there is nothing to be gained by building it before
attribution and minutes are accurate.
"""

from fastapi import APIRouter, HTTPException, status

from app.schemas.transcript import AnswerOut, QuestionIn

router = APIRouter(prefix="/qa", tags=["qa"])


@router.post("", response_model=AnswerOut)
def ask(payload: QuestionIn) -> AnswerOut:
    """Answer a question over previously recorded meetings."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        "RAG Q&A is phase 2. It lands once the transcripts it would read are accurate.",
    )
