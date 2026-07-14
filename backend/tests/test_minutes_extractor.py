"""Tests for the parts of minutes extraction that don't need the model.

The LLM calls are the obvious part; these are the parts that quietly decide
whether the output is trustworthy. Citation resolution and the owner check in
particular are the last line of defence against a fabricated commitment, so they
get tested rather than assumed.
"""

import uuid
from datetime import date

import pytest

from app.db.models.enums import MinutesItemType
from app.db.models.segment import Segment
from app.db.models.speaker import Speaker
from app.services.minutes.extractor import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    ExtractedItem,
    _deduplicate,
    chunk_segments,
    format_meeting_date,
    participant_names,
    render_transcript,
)
from app.workers.pipeline import _parse_due_date


def _segment(index: int, text: str, speaker_name: str | None = None) -> Segment:
    speaker = Speaker(id=uuid.uuid4(), display_name=speaker_name) if speaker_name else None
    return Segment(
        id=uuid.uuid4(),
        index=index,
        start_ms=index * 1000,
        end_ms=(index + 1) * 1000,
        text=text,
        speaker=speaker,
    )


def test_transcript_is_rendered_with_citable_line_numbers() -> None:
    """The [n] prefix is what the model cites, so it must be there and be right."""
    segments = [
        _segment(0, "Can you own the migration?", "Deepak"),
        _segment(1, "Yes, by Friday.", "Priya"),
    ]

    rendered = render_transcript(segments)

    assert rendered == (
        "[0] Deepak: Can you own the migration?\n[1] Priya: Yes, by Friday."
    )


def test_unattributed_speech_is_rendered_as_unknown() -> None:
    """A segment with no speaker must not silently borrow the previous one's name."""
    rendered = render_transcript([_segment(0, "Sounds good.")])
    assert rendered == "[0] Unknown: Sounds good."


def test_english_translation_is_preferred_when_present() -> None:
    """Minutes are written in English, but the original text is never destroyed."""
    seg = _segment(0, "Sí, lo haré el viernes.", "Priya")
    seg.text_en = "Yes, I'll do it on Friday."

    assert "Yes, I'll do it on Friday." in render_transcript([seg])
    assert seg.text == "Sí, lo haré el viernes."  # original survives


def test_participants_are_only_those_who_spoke() -> None:
    """The roster an action item's owner must come from."""
    segments = [
        _segment(0, "a", "Priya"),
        _segment(1, "b", "Deepak"),
        _segment(2, "c", "Priya"),
        _segment(3, "d"),  # unattributed — not a participant
    ]

    assert participant_names(segments) == ["Deepak", "Priya"]


def test_short_transcripts_are_a_single_chunk() -> None:
    segments = [_segment(i, f"line {i}") for i in range(10)]
    assert len(chunk_segments(segments)) == 1


def test_long_transcripts_are_chunked_with_overlap() -> None:
    """The overlap is load-bearing: an item is often stated in one line and
    accepted in the next, and a hard boundary between them loses the owner."""
    segments = [_segment(i, f"line {i}") for i in range(CHUNK_SIZE * 2)]

    chunks = chunk_segments(segments)

    assert len(chunks) > 1
    # Consecutive chunks must genuinely share segments.
    first_ids = {s.id for s in chunks[0]}
    second_ids = {s.id for s in chunks[1]}
    assert len(first_ids & second_ids) == CHUNK_OVERLAP


def _item(text: str, owner: str | None = None, due: str | None = None) -> ExtractedItem:
    return ExtractedItem(
        type=MinutesItemType.ACTION_ITEM,
        text=text,
        owner_name=owner,
        due_date=due,
        segment_ids=[uuid.uuid4()],
    )


def test_duplicate_items_across_chunks_are_merged_not_repeated() -> None:
    """Overlapping chunks see the same item twice. The minutes must not."""
    a = _item("Priya owns the ChromaDB migration.")
    b = _item("priya owns the chromadb migration.")  # same item, different casing

    merged = _deduplicate([a, b])

    assert len(merged) == 1


def test_merging_unions_the_citations() -> None:
    """A claim supported in two places is twice as verifiable — keep both."""
    a = _item("Priya owns the migration.")
    b = _item("Priya owns the migration.")

    merged = _deduplicate([a, b])

    assert len(merged) == 1
    assert set(merged[0].segment_ids) == set(a.segment_ids) | set(b.segment_ids)


def test_merging_recovers_an_owner_the_first_chunk_missed() -> None:
    """This is exactly what the overlap exists to catch: the commitment is stated
    in one chunk and accepted in the next."""
    without_owner = _item("Take the ChromaDB migration.", owner=None)
    with_owner = _item("Take the ChromaDB migration.", owner="Priya", due="2026-07-17")

    merged = _deduplicate([without_owner, with_owner])

    assert len(merged) == 1
    assert merged[0].owner_name == "Priya"
    assert merged[0].due_date == "2026-07-17"


def test_distinct_items_are_not_merged() -> None:
    merged = _deduplicate([_item("Priya owns the migration."), _item("Deepak owns the API.")])
    assert len(merged) == 2


def test_meeting_date_is_given_with_its_weekday() -> None:
    """The weekday is the load-bearing part.

    "By Friday" cannot be resolved from "2026-07-14" alone without the model
    doing calendar arithmetic in its head, which is how a deadline ends up a week
    out. Naming the day removes the guesswork.
    """
    rendered = format_meeting_date(date(2026, 7, 14))

    assert "Tuesday" in rendered
    assert "2026-07-14" in rendered


def test_missing_meeting_date_is_stated_not_omitted() -> None:
    """The model is told the date is unknown, so it knows not to resolve
    relative deadlines rather than quietly inventing an anchor."""
    rendered = format_meeting_date(None)

    assert "unknown" in rendered.lower()
    assert "do not resolve" in rendered.lower()


@pytest.mark.parametrize("value", [None, "", "next Friday", "2026-13-45", "soon"])
def test_unparseable_due_dates_are_dropped(value: str | None) -> None:
    """A wrong deadline is worse than a missing one — somebody plans around it."""
    assert _parse_due_date(value) is None


def test_valid_due_date_is_kept() -> None:
    parsed = _parse_due_date("2026-07-17")
    assert parsed is not None
    assert parsed.isoformat() == "2026-07-17"
