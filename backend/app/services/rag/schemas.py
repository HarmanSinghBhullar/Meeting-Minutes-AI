"""Structured output contracts for grounded RAG answers."""

from pydantic import BaseModel, Field


class QuestionAnswer(BaseModel):
    """An answer whose supporting retrieved-window numbers are explicit."""

    answer: str = Field(description="Answer only from the supplied evidence.")
    citations: list[int] = Field(
        description="Evidence-window numbers supporting the answer; empty if unknown."
    )
