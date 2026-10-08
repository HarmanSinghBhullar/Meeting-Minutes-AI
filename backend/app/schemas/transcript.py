"""Pydantic DTOs for transcripts and minutes."""

import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models.enums import MinutesItemType, SpeakerSource


class SegmentOut(BaseModel):
    """One attributed utterance."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    index: int
    start_ms: int
    end_ms: int
    text: str
    text_en: str | None
    speaker_id: uuid.UUID | None
    #: Lets the UI distinguish a name it can trust from one worth confirming.
    speaker_source: SpeakerSource


class SegmentCorrection(BaseModel):
    """A human correcting a speaker attribution.

    Sets ``speaker_source`` to MANUAL, which outranks every inferred source and
    is never overwritten by a re-run.
    """

    speaker_id: uuid.UUID


class MinutesItemOut(BaseModel):
    """One decision, action item, open question, risk, or topic."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: MinutesItemType
    text: str
    owner_speaker_id: uuid.UUID | None
    due_date: date | None
    #: The transcript lines this came from. The UI puts them one click away, so a
    #: reader can check any claim rather than taking it on faith.
    segment_ids: list[uuid.UUID]
    #: False means the grounding pass could not support this from its citations.
    is_grounded: bool | None
    grounding_note: str | None


class MinutesOut(BaseModel):
    """A generated set of minutes."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    meeting_id: uuid.UUID
    summary: str | None
    version: int
    items: list[MinutesItemOut] = []


class QuestionIn(BaseModel):
    """A grounded question over all indexed meetings or one specified meeting."""

    question: str = Field(min_length=1, max_length=2_000)
    top_k: int = Field(default=8, ge=1, le=20)
    #: Omit this to search the user's whole meeting library. The meeting page
    #: always supplies it: a question about *this* call must not be answered by
    #: a similarly worded discussion from another call.
    meeting_id: uuid.UUID | None = None

    @field_validator("question")
    @classmethod
    def question_has_content(cls, value: str) -> str:
        """Reject whitespace-only queries before they reach retrieval."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Question must not be blank.")
        return cleaned


class CitationOut(BaseModel):
    """Where an answer came from."""

    meeting_id: uuid.UUID
    meeting_title: str | None
    speaker: str | None
    start_ms: int
    text: str


class AnswerOut(BaseModel):
    """An answer, with the meeting moments that back it."""

    answer: str
    citations: list[CitationOut] = []
