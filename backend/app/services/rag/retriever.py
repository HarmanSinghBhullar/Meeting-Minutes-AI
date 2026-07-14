"""RAG retrieval and question answering — the phase-2 add-on."""

from dataclasses import dataclass


@dataclass(slots=True)
class Citation:
    """Where an answer came from. Non-optional: an uncited answer is a rumour."""

    meeting_id: str
    meeting_title: str | None
    speaker: str | None
    start_ms: int
    text: str


@dataclass(slots=True)
class Answer:
    """An answer to a question about past meetings."""

    text: str
    citations: list[Citation]


def answer_question(question: str, *, top_k: int = 8) -> Answer:
    """Answer a question over previously recorded meetings.

    TODO(phase-2): implement.
      - Retrieve top-k windows from Chroma.
      - Answer *only* from the retrieved windows, and say so when they do not
        contain the answer. The same rule as the minutes: a confident wrong
        answer about what was agreed in a meeting is worse than no answer.
      - Return citations with timestamps so the user can jump to the audio and
        hear it themselves.
    """
    raise NotImplementedError("RAG Q&A: phase 2, after minutes are accurate.")
