"""Align transcript words against the speaker timeline.

This is the step nobody puts in the requirements and everybody needs.

Whisper produces segments whose boundaries are chosen for acoustic and linguistic
convenience. The speaker timeline — whether it came from the DOM or from pyannote
— has boundaries chosen by who was talking. The two do not agree. A single
Whisper segment routinely straddles a speaker change:

    Whisper:  [------------ "yeah I think Priya should own that -- sure, I'll take it" ------------]
    Speakers: [---------- Deepak ----------][------------------ Priya ------------------]

Attributing that whole segment to whoever spoke most of it puts Priya's words in
Deepak's mouth, and that is exactly the class of error that destroys trust in a
set of minutes. So we align at the *word* level and split segments at speaker
boundaries, which is what the word timestamps from Whisper are for.
"""

import uuid
from dataclasses import dataclass

from app.db.models.enums import SpeakerSource
from app.services.attribution.base import SpeakerTurn
from app.services.transcription.base import TranscribedSegment, Word


@dataclass(slots=True)
class AttributedSegment:
    """A segment of transcript with a speaker attached, ready to persist."""

    start_ms: int
    end_ms: int
    text: str
    words: list[Word]
    speaker_label: str | None
    speaker_source: SpeakerSource
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    #: Which track this came from. Set by the caller after alignment, and carried
    #: through ``merge_tracks`` so that a merged segment still knows its origin.
    recording_id: uuid.UUID | None = None


def _speaker_for_word(word: Word, turns: list[SpeakerTurn]) -> SpeakerTurn | None:
    """Find the speaker turn a word belongs to.

    A word is assigned to the turn it overlaps most, rather than the turn
    containing its midpoint: the DOM's active-speaker indicator lags the audio
    slightly, and overlap is more forgiving of that than a point test.
    """
    start_ms = int(word.start * 1000)
    end_ms = int(word.end * 1000)

    best: SpeakerTurn | None = None
    best_overlap = 0

    for turn in turns:
        if turn.start_ms >= end_ms:
            break  # turns are sorted; nothing later can overlap
        overlap = min(end_ms, turn.end_ms) - max(start_ms, turn.start_ms)
        if overlap > best_overlap:
            best_overlap = overlap
            best = turn

    return best


def align(
    segments: list[TranscribedSegment],
    turns: list[SpeakerTurn],
    *,
    default_speaker: str | None = None,
    default_source: SpeakerSource = SpeakerSource.UNKNOWN,
) -> list[AttributedSegment]:
    """Attribute transcript segments to speakers, splitting on speaker changes.

    Args:
        segments: Whisper output, with word timings.
        turns: The speaker timeline, sorted by start time. May be empty — for the
            microphone track it should be, since that track is the local user by
            definition and needs no inference at all.
        default_speaker: Used when a word matches no turn (or none were supplied).
            For the mic track this is the local user's name.
        default_source: The attribution source to record for defaulted words.

    Returns:
        Segments split so that each one has exactly one speaker.
    """
    out: list[AttributedSegment] = []

    for seg in segments:
        # No word timings (very short segment, or VAD trimmed it) — fall back to
        # attributing the segment whole. Rare, and better than dropping it.
        if not seg.words:
            turn = _speaker_for_word(
                Word(word=seg.text, start=seg.start, end=seg.end), turns
            )
            out.append(
                AttributedSegment(
                    start_ms=int(seg.start * 1000),
                    end_ms=int(seg.end * 1000),
                    text=seg.text,
                    words=[],
                    speaker_label=turn.speaker_label if turn else default_speaker,
                    speaker_source=turn.source if turn else default_source,
                    avg_logprob=seg.avg_logprob,
                    no_speech_prob=seg.no_speech_prob,
                )
            )
            continue

        # Walk the words, starting a new segment every time the speaker changes.
        run: list[Word] = []
        run_label: str | None = None
        run_source: SpeakerSource = default_source

        def flush() -> None:
            if not run:
                return
            out.append(
                AttributedSegment(
                    start_ms=int(run[0].start * 1000),
                    end_ms=int(run[-1].end * 1000),
                    text="".join(w.word for w in run).strip(),
                    words=list(run),
                    speaker_label=run_label,
                    speaker_source=run_source,
                    avg_logprob=seg.avg_logprob,
                    no_speech_prob=seg.no_speech_prob,
                )
            )
            run.clear()

        for word in seg.words:
            turn = _speaker_for_word(word, turns)
            label = turn.speaker_label if turn else default_speaker
            source = turn.source if turn else default_source

            if run and label != run_label:
                flush()

            run_label = label
            run_source = source
            run.append(word)

        flush()

    out.sort(key=lambda s: s.start_ms)
    return out


def merge_tracks(
    mic: list[AttributedSegment], tab: list[AttributedSegment]
) -> list[AttributedSegment]:
    """Interleave the two tracks into one chronological transcript.

    The tracks are transcribed independently — that is the whole benefit of
    keeping them separate — so the final transcript is produced by merging them
    on time. Overlapping speech (someone talking over the call) survives this,
    which is correct: it really did overlap.
    """
    return sorted([*mic, *tab], key=lambda s: s.start_ms)
