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

**Two clocks, not one.** The subtlety underneath all of this is that the word
times and the turn times come from different models, and neither is ground truth.
Whisper infers word boundaries from cross-attention; pyannote infers turn
boundaries from a segmentation network at its own frame rate. They disagree by
something on the order of 100-200ms even when they agree perfectly about *who*
spoke. Taken literally, that disagreement produces two artifacts, and both of
them are handled here rather than pretended away:

* a word can land in a *seam* between two turns and overlap neither, and
* a word either side of a boundary can be pulled across it.

The first would come out ``UNKNOWN`` in the middle of a sentence whose speaker is
not in any doubt; the second invents a one-word segment credited to the wrong
person. Neither is a real disagreement about the audio, so neither should reach
the minutes as though it were. See ``NEAREST_TURN_TOLERANCE_MS`` and
``MIN_RUN_MS`` — both are deliberately tight, because the cost of over-applying
them is silently absorbing speech that *was* someone else's.
"""

import uuid
from dataclasses import dataclass

from app.db.models.enums import SpeakerSource
from app.services.attribution.base import SpeakerTurn
from app.services.transcription.base import TranscribedSegment, Word

#: How far outside every turn a word may fall and still be attributed to the
#: nearest one.
#:
#: This exists for the seam described above: pyannote's turns are tight around
#: speech, so the gaps between them routinely swallow a word that Whisper timed a
#: fraction of a second differently. Without a tolerance that word matches no turn
#: at all and is attributed to nobody.
#:
#: Deliberately far shorter than a conversational pause. A word sitting in genuine
#: silence — the hallucination Whisper produces over a long gap — is *supposed* to
#: come out UNKNOWN, and widening this until those get absorbed would hand them to
#: whoever happened to speak last.
NEAREST_TURN_TOLERANCE_MS = 250

#: The shortest run of words that can be believed as a real speaker change, when
#: the same speaker holds the floor either side of it.
#:
#: Boundary jitter shows up as a word or two flipping to a neighbouring speaker
#: and flipping straight back. Nobody says one word in the middle of someone
#: else's sentence and vanishes; that shape is an artifact of the two clocks, and
#: cutting on it fragments the transcript and credits the wrong person.
#:
#: Only applied to runs *enclosed* by a single other speaker (see
#: ``_absorb_short_runs``), which is what keeps it from eating short-but-real
#: speech: a genuine two-word interjection is normally answered, so it is not
#: enclosed by one voice. 300ms is under a spoken word and a half.
MIN_RUN_MS = 300


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
    containing its midpoint: the speaker timeline and the word timings come from
    different models, and overlap is more forgiving of that than a point test.
    Failing that, it goes to the nearest turn within ``NEAREST_TURN_TOLERANCE_MS``
    — the seam case from the module docstring, where the two clocks disagree by
    enough that a word falls between two turns and overlaps neither.

    Presenter turns are a fallback, not a peer. Someone screen-sharing a video
    produces a single turn spanning the whole share, so if it competed on overlap
    it would swallow every word — including ones a real speaker briefly said over
    the top. So we resolve against genuine *speaking* turns first and only consult
    the presenter when no one was speaking at all — including a *near-miss* on a
    speaking turn, which beats the presenter: a word 100ms off someone's turn is
    that person mistimed, not the screen share.

    ``turns`` must be sorted by start time; ``align`` guarantees it.
    """
    start_ms = int(word.start * 1000)
    end_ms = int(word.end * 1000)

    def best_overlapping(candidates: list[SpeakerTurn]) -> SpeakerTurn | None:
        best: SpeakerTurn | None = None
        best_overlap = 0
        for turn in candidates:
            if turn.start_ms >= end_ms:
                break  # sorted by start; nothing later can overlap
            overlap = min(end_ms, turn.end_ms) - max(start_ms, turn.start_ms)
            if overlap > best_overlap:
                best_overlap = overlap
                best = turn
        return best

    def nearest_within_tolerance(candidates: list[SpeakerTurn]) -> SpeakerTurn | None:
        best: SpeakerTurn | None = None
        best_gap = NEAREST_TURN_TOLERANCE_MS + 1  # so any hit must beat the bound
        for turn in candidates:
            if turn.start_ms - end_ms > NEAREST_TURN_TOLERANCE_MS:
                # Sorted by start, so every later turn begins further away still.
                break
            gap = max(turn.start_ms - end_ms, start_ms - turn.end_ms, 0)
            if gap < best_gap:
                best_gap = gap
                best = turn
        return best

    speaking = [t for t in turns if t.source is not SpeakerSource.PRESENTER]
    presenting = [t for t in turns if t.source is SpeakerSource.PRESENTER]

    for candidates in (speaking, presenting):
        match = best_overlapping(candidates) or nearest_within_tolerance(candidates)
        if match is not None:
            return match

    return None


#: A word's resolved attribution: who said it, and on whose authority.
_Label = tuple[str | None, SpeakerSource]


def _runs(labels: list[_Label]) -> list[tuple[int, int]]:
    """The maximal same-label spans of ``labels``, as half-open index ranges."""
    spans: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(labels) + 1):
        if i == len(labels) or labels[i] != labels[start]:
            spans.append((start, i))
            start = i
    return spans


def _absorb_short_runs(words: list[Word], labels: list[_Label]) -> None:
    """Rewrite one-off speaker flips back to the speaker surrounding them.

    Mutates ``labels`` in place.

    A run is absorbed only when it is *enclosed* — the same speaker holds the
    floor immediately before and immediately after it — and shorter than
    ``MIN_RUN_MS``. Both conditions matter. The duration alone would eat real
    backchannel ("mm-hm", "right"), which is short by nature; the enclosure is
    what marks it as jitter instead, because a real speaker change is answered
    rather than reverted mid-sentence.

    Runs at the edges of a Whisper segment are deliberately left alone. A segment
    that *opens* with a short burst from another speaker has no "before" to be
    enclosed by, and it is just as likely to be a genuine reply that Whisper
    happened to group with the previous sentence. Absorbing it would be a guess;
    leaving it is at worst the behaviour we already had.
    """
    for start, end in _runs(labels):
        if start == 0 or end == len(labels):
            continue  # an edge run is not enclosed by anything

        before = labels[start - 1]
        if before != labels[end]:
            continue  # different speakers either side: a real change, not a flip

        duration_ms = int((words[end - 1].end - words[start].start) * 1000)
        if duration_ms >= MIN_RUN_MS:
            continue  # long enough to be believed

        for i in range(start, end):
            labels[i] = before


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
        turns: The speaker timeline. May be empty — for the microphone track it
            should be, since that track is the local user by definition and needs
            no inference at all.
        default_speaker: Used when a word matches no turn (or none were supplied).
            For the mic track this is the local user's name.
        default_source: The attribution source to record for defaulted words.

    Returns:
        Segments split so that each one has exactly one speaker.
    """
    # Both searches in `_speaker_for_word` stop early on the assumption that turns
    # ascend by start time. pyannote happens to emit them that way, which is
    # exactly why this is worth doing here rather than trusting it: a provider that
    # quietly returned them unsorted would not fail, it would silently mis-attribute
    # from the first out-of-order turn onward.
    turns = sorted(turns, key=lambda t: t.start_ms)

    def label_for(word: Word) -> _Label:
        turn = _speaker_for_word(word, turns)
        return (
            (turn.speaker_label, turn.source)
            if turn is not None
            else (default_speaker, default_source)
        )

    out: list[AttributedSegment] = []

    for seg in segments:
        # No word timings (very short segment, or VAD trimmed it) — fall back to
        # attributing the segment whole. Rare, and better than dropping it.
        if not seg.words:
            label, source = label_for(Word(word=seg.text, start=seg.start, end=seg.end))
            out.append(
                AttributedSegment(
                    start_ms=int(seg.start * 1000),
                    end_ms=int(seg.end * 1000),
                    text=seg.text,
                    words=[],
                    speaker_label=label,
                    speaker_source=source,
                    avg_logprob=seg.avg_logprob,
                    no_speech_prob=seg.no_speech_prob,
                )
            )
            continue

        # Resolve every word, then smooth, then cut. Smoothing has to see the
        # whole segment's labels at once to tell an enclosed flip from a real
        # change, so it cannot be folded into the walk that builds the runs.
        labels = [label_for(word) for word in seg.words]
        _absorb_short_runs(seg.words, labels)

        run: list[Word] = []
        run_label: _Label = (default_speaker, default_source)

        def flush() -> None:
            if not run:
                return
            speaker_label, speaker_source = run_label
            out.append(
                AttributedSegment(
                    start_ms=int(run[0].start * 1000),
                    end_ms=int(run[-1].end * 1000),
                    text="".join(w.word for w in run).strip(),
                    words=list(run),
                    speaker_label=speaker_label,
                    speaker_source=speaker_source,
                    avg_logprob=seg.avg_logprob,
                    no_speech_prob=seg.no_speech_prob,
                )
            )
            run.clear()

        for word, label in zip(seg.words, labels, strict=True):
            if run and label != run_label:
                flush()  # reads run_label before it is reassigned, on purpose
            run_label = label
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
