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


def test_presenter_turn_names_screen_share_audio() -> None:
    """A screen-share with audio has no speaking signal, so the sharer names it.

    This is the "played video" case: nobody is shown speaking, but the UI does say
    who is presenting, and attributing the audio to them beats "Unknown".
    """
    segment = TranscribedSegment(
        start=0.0,
        end=1.0,
        text=" welcome to the workshop",
        words=[_word(" welcome to the workshop", 0.0, 1.0)],
    )

    # One long presenter turn spanning the whole share, and no speaking turn.
    turns = [SpeakerTurn("Harman", 0, 60_000, SpeakerSource.PRESENTER)]

    result = align([segment], turns)

    assert len(result) == 1
    assert result[0].speaker_label == "Harman"
    assert result[0].speaker_source is SpeakerSource.PRESENTER


def test_real_speaking_overrides_a_presenter_turn() -> None:
    """When someone actually speaks over a share, the speaker wins, not the sharer.

    The presenter turn spans everything, so on raw overlap it would swallow the
    word; it must lose to a genuine speaking turn that also covers it.
    """
    segment = TranscribedSegment(
        start=0.0,
        end=1.0,
        text=" quick question",
        words=[_word(" quick question", 0.0, 1.0)],
    )

    turns = [
        SpeakerTurn("Harman", 0, 60_000, SpeakerSource.PRESENTER),  # sharing all along
        SpeakerTurn("Priya", 0, 1000, SpeakerSource.DOM),  # actually speaking here
    ]

    result = align([segment], turns)

    assert len(result) == 1
    assert result[0].speaker_label == "Priya"
    assert result[0].speaker_source is SpeakerSource.DOM


def test_word_in_the_seam_between_turns_goes_to_the_nearest_speaker() -> None:
    """A word overlapping no turn at all is still attributed, if it is close.

    pyannote's turns are tight around speech, so the gaps between them routinely
    swallow a word that Whisper timed slightly differently. Nobody is in any doubt
    about who spoke here; the two models just disagree about when by 100ms.
    """
    segment = TranscribedSegment(
        start=5.1,
        end=5.2,
        text=" right",
        words=[_word(" right", 5.1, 5.2)],  # 5100-5200ms
    )

    # Priya's turn ends 100ms before the word starts, and nothing else is near.
    result = align([segment], turns=[SpeakerTurn("Priya", 0, 5000, SpeakerSource.DOM)])

    assert len(result) == 1
    assert result[0].speaker_label == "Priya"
    assert result[0].speaker_source is SpeakerSource.DOM


def test_a_word_in_genuine_silence_is_still_unknown() -> None:
    """The tolerance must not become "hand it to whoever spoke last".

    This is the guard on the previous test. Half a second past the end of the only
    turn is no longer clock disagreement — it is speech nobody claims, quite
    possibly a Whisper hallucination over silence, and inventing an owner for it
    is exactly the confident wrong answer the mapping gate exists to prevent.
    """
    segment = TranscribedSegment(
        start=5.5,
        end=6.0,
        text=" thank you",
        words=[_word(" thank you", 5.5, 6.0)],  # 500ms past the turn
    )

    result = align([segment], turns=[SpeakerTurn("Priya", 0, 5000, SpeakerSource.DOM)])

    assert len(result) == 1
    assert result[0].speaker_label is None
    assert result[0].speaker_source is SpeakerSource.UNKNOWN


def test_a_one_word_flip_inside_one_speaker_is_absorbed() -> None:
    """Deepak does not stop talking so Priya can say one word and vanish.

    That shape is boundary jitter between two models' clocks. Cut on it and the
    transcript fragments and credits a word to the wrong person.
    """
    segment = TranscribedSegment(
        start=0.0,
        end=2.4,
        text=" I think yeah we ship",
        words=[
            _word(" I", 0.0, 0.4),
            _word(" think", 0.4, 0.9),
            _word(" yeah", 1.0, 1.15),  # 150ms, lands in Priya's sliver
            _word(" we", 1.3, 1.8),
            _word(" ship", 1.8, 2.4),
        ],
    )

    turns = [
        SpeakerTurn("Deepak", 0, 1000, SpeakerSource.DOM),
        SpeakerTurn("Priya", 1000, 1200, SpeakerSource.DOM),  # 200ms sliver
        SpeakerTurn("Deepak", 1200, 3000, SpeakerSource.DOM),
    ]

    result = align([segment], turns)

    assert len(result) == 1
    assert result[0].speaker_label == "Deepak"
    assert result[0].text == "I think yeah we ship"


def test_a_long_enough_interjection_survives_smoothing() -> None:
    """Smoothing is for jitter, not for anything short.

    The guard on the previous test: enclosed by one speaker either side is not on
    its own enough to disbelieve a speaker change, or a real interruption would be
    silently handed to whoever was talking over it.
    """
    segment = TranscribedSegment(
        start=0.0,
        end=2.6,
        text=" I think yeah actually we ship",
        words=[
            _word(" I", 0.0, 0.4),
            _word(" think", 0.4, 0.9),
            _word(" yeah", 1.0, 1.2),
            _word(" actually", 1.2, 1.55),  # run is 550ms — long enough to believe
            _word(" we", 1.7, 2.2),
            _word(" ship", 2.2, 2.6),
        ],
    )

    turns = [
        SpeakerTurn("Deepak", 0, 1000, SpeakerSource.DOM),
        SpeakerTurn("Priya", 1000, 1600, SpeakerSource.DOM),
        SpeakerTurn("Deepak", 1600, 3000, SpeakerSource.DOM),
    ]

    result = align([segment], turns)

    assert [s.speaker_label for s in result] == ["Deepak", "Priya", "Deepak"]
    assert result[1].text == "yeah actually"


def test_a_short_run_at_the_segment_edge_is_left_alone() -> None:
    """A segment opening with someone else's word has no enclosure to judge it by.

    Whisper groups by its own convenience, so a reply can easily be grouped with
    the sentence it answers. With no speaker after it to say the floor went back,
    absorbing it would be a guess.
    """
    segment = TranscribedSegment(
        start=0.0,
        end=2.0,
        text=" no I think we ship",
        words=[
            _word(" no", 0.0, 0.15),  # 150ms, but nothing precedes it
            _word(" I", 0.3, 0.6),
            _word(" think", 0.6, 1.1),
            _word(" we", 1.1, 1.5),
            _word(" ship", 1.5, 2.0),
        ],
    )

    turns = [
        SpeakerTurn("Priya", 0, 200, SpeakerSource.DOM),
        SpeakerTurn("Deepak", 200, 3000, SpeakerSource.DOM),
    ]

    result = align([segment], turns)

    assert [s.speaker_label for s in result] == ["Priya", "Deepak"]
    assert result[0].text == "no"


def test_turns_are_sorted_before_alignment() -> None:
    """Out-of-order turns must not silently mis-attribute.

    Both searches stop early on the assumption that turns ascend by start time, so
    an unsorted timeline would not raise — it would quietly attribute everything
    before the first turn to nobody.
    """
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
            _word(" sure", 2.6, 3.0),
            _word(" I'll", 3.0, 3.4),
            _word(" take", 3.4, 3.7),
            _word(" it", 3.7, 4.0),
        ],
    )

    reversed_turns = [
        SpeakerTurn("Priya", 2500, 4000, SpeakerSource.DOM),
        SpeakerTurn("Deepak", 0, 2500, SpeakerSource.DOM),
    ]

    result = align([segment], reversed_turns)

    assert [s.speaker_label for s in result] == ["Deepak", "Priya"]


def test_a_near_miss_on_a_speaker_beats_the_presenter() -> None:
    """A word just off someone's turn is them mistimed, not the screen share.

    The presenter spans everything, so without this the seam tolerance would be
    pointless on exactly the calls that have a share running.
    """
    segment = TranscribedSegment(
        start=1.1,
        end=1.2,
        text=" exactly",
        words=[_word(" exactly", 1.1, 1.2)],  # 100ms past Priya's turn
    )

    turns = [
        SpeakerTurn("Harman", 0, 60_000, SpeakerSource.PRESENTER),
        SpeakerTurn("Priya", 0, 1000, SpeakerSource.DOM),
    ]

    result = align([segment], turns)

    assert len(result) == 1
    assert result[0].speaker_label == "Priya"
    assert result[0].speaker_source is SpeakerSource.DOM


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
