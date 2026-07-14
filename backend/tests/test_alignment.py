"""Tests for word-to-speaker alignment.

The case that matters is the one in the module docstring: a Whisper segment that
straddles a speaker change. Getting this wrong puts one person's words in another
person's mouth, which is the error that destroys trust in a set of minutes, so it
is the first thing worth a test.
"""

from app.db.models.enums import SpeakerSource
from app.services.alignment import align, merge_tracks
from app.services.attribution.base import SpeakerTurn
from app.services.transcription.base import TranscribedSegment, Word


def _word(text: str, start: float, end: float) -> Word:
    return Word(word=text, start=start, end=end, probability=0.9)


def test_segment_straddling_a_speaker_change_is_split() -> None:
    """One Whisper segment, two speakers: it must be cut at the boundary."""
    segment = TranscribedSegment(
        start=0.0,
        end=4.0,
        text=" I think Priya should own that sure I'll take it",
        words=[
            _word(" I", 0.0, 0.4),
            _word(" think", 0.4, 0.8),
            _word(" Priya", 0.8, 1.2),
            _word(" should", 1.2, 1.6),
            _word(" own", 1.6, 2.0),
            _word(" that", 2.0, 2.4),
            # speaker changes here
            _word(" sure", 2.6, 3.0),
            _word(" I'll", 3.0, 3.4),
            _word(" take", 3.4, 3.7),
            _word(" it", 3.7, 4.0),
        ],
    )

    turns = [
        SpeakerTurn("Deepak", 0, 2500, SpeakerSource.DOM),
        SpeakerTurn("Priya", 2500, 4000, SpeakerSource.DOM),
    ]

    result = align([segment], turns)

    assert len(result) == 2

    assert result[0].speaker_label == "Deepak"
    assert result[0].text == "I think Priya should own that"

    assert result[1].speaker_label == "Priya"
    assert result[1].text == "sure I'll take it"
    # The point of the whole exercise: Priya accepting the work is attributed to
    # Priya, not to whoever happened to speak most of the segment.
    assert result[1].speaker_source is SpeakerSource.DOM


def test_mic_track_needs_no_turns() -> None:
    """The microphone is the local user by definition — no inference required."""
    segment = TranscribedSegment(
        start=0.0,
        end=1.0,
        text=" sounds good",
        words=[_word(" sounds", 0.0, 0.5), _word(" good", 0.5, 1.0)],
    )

    result = align(
        [segment],
        turns=[],
        default_speaker="Harman",
        default_source=SpeakerSource.LOCAL_TRACK,
    )

    assert len(result) == 1
    assert result[0].speaker_label == "Harman"
    assert result[0].speaker_source is SpeakerSource.LOCAL_TRACK


def test_word_is_assigned_to_the_turn_it_overlaps_most() -> None:
    """A word spanning a boundary goes to whoever holds more of it.

    Overlap rather than a midpoint test, because the DOM's active-speaker
    indicator lags the audio slightly and overlap absorbs that.
    """
    segment = TranscribedSegment(
        start=0.0,
        end=1.0,
        text=" yes",
        words=[_word(" yes", 0.0, 1.0)],  # 0-1000ms
    )

    turns = [
        SpeakerTurn("Deepak", 0, 300, SpeakerSource.DOM),  # holds 300ms
        SpeakerTurn("Priya", 300, 2000, SpeakerSource.DOM),  # holds 700ms
    ]

    result = align([segment], turns)

    assert len(result) == 1
    assert result[0].speaker_label == "Priya"


def test_segments_with_no_matching_turn_fall_back_to_the_default() -> None:
    """Speech nobody claims is 'unknown', not silently mislabelled."""
    segment = TranscribedSegment(
        start=10.0,
        end=11.0,
        text=" hello",
        words=[_word(" hello", 10.0, 11.0)],
    )

    result = align([segment], turns=[SpeakerTurn("Priya", 0, 5000, SpeakerSource.DOM)])

    assert len(result) == 1
    assert result[0].speaker_label is None
    assert result[0].speaker_source is SpeakerSource.UNKNOWN


def test_tracks_merge_in_time_order() -> None:
    """The two tracks interleave chronologically; overlapping speech survives."""
    mic = align(
        [TranscribedSegment(2.0, 3.0, "and I agree", [_word(" and I agree", 2.0, 3.0)])],
        turns=[],
        default_speaker="Harman",
        default_source=SpeakerSource.LOCAL_TRACK,
    )
    tab = align(
        [TranscribedSegment(0.0, 1.0, "let's ship it", [_word(" let's ship it", 0.0, 1.0)])],
        turns=[SpeakerTurn("Priya", 0, 1000, SpeakerSource.DOM)],
    )

    merged = merge_tracks(mic, tab)

    assert [s.speaker_label for s in merged] == ["Priya", "Harman"]
