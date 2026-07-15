"""Tests for the parts of translation that don't need the model.

The model call itself is the obvious part. These are the parts that quietly keep
the output usable: that batches cover the transcript exactly once with no
overlap, that the line boundaries the minutes cite are preserved through the
round trip, and that a mangled model response degrades to the original text
rather than corrupting a neighbour.
"""

import uuid

import pytest

from app.db.models.segment import Segment
from app.db.models.speaker import Speaker
from app.services.translation import (
    CHUNK_SIZE,
    TranslatedLine,
    _apply_translations,
    _render_batch,
    needs_translation,
    translate_segments,
)


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


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (None, False),
        ("", False),
        ("en", False),
        ("english", False),  # startswith("en")
        ("es", True),
        ("hi", True),
        ("de", True),
    ],
)
def test_needs_translation(language: str | None, expected: bool) -> None:
    """English (in any spelling that starts 'en') never needs translating; others do."""
    assert needs_translation(language) is expected


def test_batch_is_rendered_with_indices_and_speakers() -> None:
    """The [n] index is what the model echoes back, so it must be present and right."""
    batch = [
        _segment(0, "¿Puedes con la migración?", "Deepak"),
        _segment(1, "Sí, para el viernes.", "Priya"),
    ]

    rendered = _render_batch(batch)

    assert rendered == (
        "[0] Deepak: ¿Puedes con la migración?\n[1] Priya: Sí, para el viernes."
    )


def test_unattributed_line_is_rendered_as_unknown() -> None:
    """A speakerless line must not silently borrow the previous speaker's name."""
    assert _render_batch([_segment(0, "Vale.")]) == "[0] Unknown: Vale."


def test_apply_writes_english_without_destroying_the_original() -> None:
    """The whole contract: text_en is populated, text is left as evidence."""
    batch = [_segment(0, "Sí, lo haré el viernes.", "Priya")]

    _apply_translations(batch, [TranslatedLine(index=0, text_en="Yes, I'll do it Friday.")])

    assert batch[0].text_en == "Yes, I'll do it Friday."
    assert batch[0].text == "Sí, lo haré el viernes."  # original survives


def test_apply_maps_strictly_by_index() -> None:
    """Line boundaries are load-bearing — a translation lands only on its own line."""
    batch = [_segment(0, "uno"), _segment(1, "dos"), _segment(2, "tres")]

    # Deliberately out of order, and only two of the three lines.
    _apply_translations(
        batch,
        [
            TranslatedLine(index=2, text_en="three"),
            TranslatedLine(index=0, text_en="one"),
        ],
    )

    assert batch[0].text_en == "one"
    assert batch[1].text_en is None  # untranslated line keeps None, not a neighbour's text
    assert batch[2].text_en == "three"


def test_apply_ignores_out_of_range_indices() -> None:
    """A hallucinated line number must not raise or shift the mapping."""
    batch = [_segment(0, "uno")]

    _apply_translations(
        batch,
        [
            TranslatedLine(index=5, text_en="stray"),
            TranslatedLine(index=-1, text_en="also stray"),
            TranslatedLine(index=0, text_en="one"),
        ],
    )

    assert batch[0].text_en == "one"


def test_apply_drops_blank_translations() -> None:
    """An empty rendering falls back to the original rather than blanking the line."""
    batch = [_segment(0, "uno")]

    _apply_translations(batch, [TranslatedLine(index=0, text_en="   ")])

    assert batch[0].text_en is None


def test_translate_segments_of_empty_input_is_a_noop() -> None:
    """No segments, no model call, no error."""
    translate_segments([], source_language="es", model="test-model")  # must not raise


def test_batches_partition_the_transcript_without_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlike extraction, translation batches must not overlap: a line is
    translated exactly once, so every segment appears in exactly one batch and
    no segment is skipped."""
    segments = [_segment(i, f"linea {i}") for i in range(CHUNK_SIZE * 2 + 5)]

    seen: list[uuid.UUID] = []

    def _record(batch: list[Segment], **_kwargs: object) -> None:
        seen.extend(s.id for s in batch)

    monkeypatch.setattr("app.services.translation._translate_batch", _record)
    translate_segments(segments, source_language="es", model="test-model")

    # Every segment covered exactly once, in order.
    assert seen == [s.id for s in segments]
