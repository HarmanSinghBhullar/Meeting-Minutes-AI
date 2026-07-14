"""Structured-output schemas for the minutes pipeline.

These are the shapes the model is *forced* to return. Validation happens at the
API layer — the request declares the JSON schema and the response is guaranteed
to match it — so nothing here has to parse prose or repair malformed JSON.

That matters beyond convenience. The single most valuable field is ``cites``: by
making it a required part of the schema, an item without a citation becomes
impossible to express, rather than merely discouraged.
"""

from typing import Literal

from pydantic import BaseModel, Field

#: The kinds of item we extract. Mirrors MinutesItemType on the ORM side; kept
#: as a Literal here because the structured-output schema needs a plain enum.
ItemType = Literal["decision", "action_item", "open_question", "risk", "topic"]


class ExtractedItemModel(BaseModel):
    """One item the model pulled out of a chunk of transcript."""

    type: ItemType
    text: str = Field(
        description="The decision, action, question, or risk, stated in one sentence."
    )
    owner: str | None = Field(
        default=None,
        description=(
            "For action items: the exact name of the participant who took it on. "
            "Must be one of the participants listed. Null if nobody explicitly took it."
        ),
    )
    due_date: str | None = Field(
        default=None,
        description="ISO date (YYYY-MM-DD) if one was stated. Null otherwise. Do not guess.",
    )
    cites: list[int] = Field(
        description=(
            "The [n] line numbers this came from. Required — every item must be "
            "traceable to the lines that support it."
        )
    )


class ExtractionResult(BaseModel):
    """Everything found in one chunk of transcript."""

    items: list[ExtractedItemModel]


class SummaryResult(BaseModel):
    """The prose overview, written once over the whole meeting."""

    summary: str = Field(description="A short prose overview of what the meeting covered.")


class GroundingVerdict(BaseModel):
    """The verifier's judgement on a single item.

    ``is_grounded`` is the gate. ``note`` explains a rejection so a human can see
    what the verifier objected to — a rejection nobody can inspect is just as
    opaque as a hallucination nobody can catch.
    """

    is_grounded: bool = Field(
        description=(
            "True only if the quoted lines actually state this. False if they are "
            "merely consistent with it, or if the owner is not the person who "
            "committed."
        )
    )
    note: str | None = Field(
        default=None,
        description="If not grounded, one sentence on what the lines fail to support.",
    )
