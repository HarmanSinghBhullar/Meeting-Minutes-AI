"""The processing pipeline: what actually happens to a recording.

Stage order is forced by data dependencies:

    normalize -> transcribe -> attribute -> align -> [translate] -> minutes -> ground -> [index]

**GPU discipline.** A 4GB card cannot hold Whisper and pyannote at once. This is
not a reason to shrink Whisper — the model size is what buys us the proper nouns
the minutes are made of. Instead, only one model is resident at a time: each
stage unloads before the next loads. Paying 10-30 seconds of model load on a job
that already takes minutes is not worth optimizing.

In the common case (a browser meeting on a supported platform) pyannote never
loads at all, because the DOM gave us the speaker timeline for free, and Whisper
gets the entire card to itself.
"""

import logging
import uuid
from dataclasses import asdict
from datetime import date
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.enums import JobType, SpeakerSource, Track
from app.db.models.meeting import Meeting
from app.db.models.minutes import Minutes, MinutesItem
from app.db.models.recording import Recording
from app.db.models.segment import Segment
from app.db.models.speaker import Speaker
from app.services.alignment import AttributedSegment, align, merge_tracks
from app.services.attribution.base import SpeakerTurn
from app.services.attribution.dom_timeline import DomTimelineProvider
from app.services.attribution.pyannote_provider import PyannoteProvider
from app.services.audio import ffmpeg, storage
from app.services.minutes.extractor import ExtractedItem, extract, summarize
from app.services.minutes.grounding import verify
from app.services.transcription.base import TranscriptionResult
from app.services.transcription.faster_whisper_provider import FasterWhisperProvider
from app.services.translation import needs_translation, translate_segments
from app.workers.queue import enqueue

logger = logging.getLogger(__name__)


def build_vocabulary_prompt(db: Session, meeting: Meeting) -> str | None:
    """Assemble the ``initial_prompt`` that primes Whisper on this meeting.

    Participant names plus the title and agenda. Cheap, and it rescues exactly
    the words that carry the meaning: who, and what thing.
    """
    names = (
        db.execute(select(Speaker.display_name).where(Speaker.meeting_id == meeting.id))
        .scalars()
        .all()
    )

    parts = [p for p in (meeting.title, meeting.agenda) if p]
    if names:
        parts.append(", ".join(names))

    return ". ".join(parts) if parts else None


def run_transcribe(db: Session, meeting_id: uuid.UUID) -> None:
    """Normalize, transcribe, and attribute every track of a meeting."""
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise ValueError(f"Meeting {meeting_id} does not exist.")

    recordings = {
        r.track: r
        for r in meeting.recordings
        if r.is_finalized and r.chunk_count > 0 and r.source_path
    }
    if not recordings:
        raise RuntimeError("Meeting has no finalized audio to transcribe.")

    _normalize(db, meeting_id, recordings)
    results = _transcribe(db, meeting, recordings)

    # Whisper detects per track, but a meeting has one language. The tab track
    # carries most of the speech, so its detection is the one we trust.
    language = next(
        (results[t].language for t in (Track.TAB, Track.MIC) if t in results and results[t].language),
        None,
    )
    meeting.source_language = language

    attributed = _attribute(db, meeting, recordings, results)
    _persist_segments(db, meeting, attributed, language)

    # Translation must land before the minutes, because the minutes are written
    # from the English text.
    next_stage = JobType.TRANSLATE if needs_translation(language) else JobType.MINUTES
    enqueue(db, meeting.id, next_stage)
    db.commit()


def _normalize(
    db: Session, meeting_id: uuid.UUID, recordings: dict[Track, Recording]
) -> None:
    """Convert each uploaded WebM track into the 16kHz mono WAV Whisper reads."""
    for track, recording in recordings.items():
        source = Path(recording.source_path or "")
        dest = storage.normalized_path(meeting_id, track)

        logger.info("Normalizing %s track (%s)", track.value, source.name)
        ffmpeg.normalize(source, dest)

        recording.normalized_path = str(dest)
        recording.duration_seconds = ffmpeg.probe_duration(dest)

    db.commit()


def _transcribe(
    db: Session, meeting: Meeting, recordings: dict[Track, Recording]
) -> dict[Track, TranscriptionResult]:
    """Transcribe every track, loading the model exactly once.

    The model load is the expensive part, not the call, so both tracks go through
    a single resident Whisper. It is unloaded before we return, because whatever
    runs next may want the GPU and there is not enough VRAM to share.
    """
    vocabulary = build_vocabulary_prompt(db, meeting)
    whisper = FasterWhisperProvider()
    results: dict[Track, TranscriptionResult] = {}

    try:
        language: str | None = None
        # Tab first. It is usually the bulk of the speech, so it gives the more
        # reliable language detection — which we then force on the mic track so
        # the two halves of one conversation cannot come out in two languages.
        for track in (Track.TAB, Track.MIC):
            recording = recordings.get(track)
            if recording is None or not recording.normalized_path:
                continue

            logger.info("Transcribing %s track", track.value)
            result = whisper.transcribe(
                Path(recording.normalized_path),
                language=language,
                vocabulary_prompt=vocabulary,
            )
            results[track] = result
            language = language or result.language
    finally:
        whisper.unload()

    return results


def _attribute(
    db: Session,
    meeting: Meeting,
    recordings: dict[Track, Recording],
    results: dict[Track, TranscriptionResult],
) -> list[AttributedSegment]:
    """Attach a speaker to every word, then merge the tracks into one timeline."""
    per_track: dict[Track, list[AttributedSegment]] = {}

    # The mic track needs no inference whatsoever: it is the local user's own
    # microphone, so it is the local user. This is the entire payoff of recording
    # the tracks separately instead of mixing them.
    if Track.MIC in results:
        local = _local_speaker(db, meeting)
        per_track[Track.MIC] = align(
            results[Track.MIC].segments,
            turns=[],
            default_speaker=local.display_name,
            default_source=SpeakerSource.LOCAL_TRACK,
        )

    if Track.TAB in results:
        turns = _remote_speaker_turns(db, meeting, recordings[Track.TAB])
        per_track[Track.TAB] = align(
            results[Track.TAB].segments,
            turns=turns,
            default_speaker=None,  # heard someone, don't know who — say so
            default_source=SpeakerSource.UNKNOWN,
        )

    for track, segments in per_track.items():
        for segment in segments:
            segment.recording_id = recordings[track].id

    return merge_tracks(per_track.get(Track.MIC, []), per_track.get(Track.TAB, []))


def _remote_speaker_turns(
    db: Session, meeting: Meeting, tab_recording: Recording
) -> list[SpeakerTurn]:
    """Get the speaker timeline for the remote participants.

    The DOM timeline is preferred and is usually all we need: the meeting UI
    already told us who was talking, by name. pyannote only loads when there is
    no timeline — an unsupported platform, or a conference-room microphone — and
    it can only offer anonymous clusters that a human then has to label.
    """
    dom = DomTimelineProvider(db)
    if dom.has_timeline(meeting_id=str(meeting.id)):
        turns = dom.attribute(meeting_id=str(meeting.id))
        logger.info("Attribution: DOM timeline (%d turns, named)", len(turns))
        return turns

    if not settings.diarization_enabled:
        logger.warning("No DOM timeline and diarization disabled; speakers will be unknown.")
        return []

    logger.info("No DOM timeline; falling back to pyannote diarization.")
    pyannote = PyannoteProvider()
    try:
        return pyannote.attribute(
            meeting_id=str(meeting.id),
            audio_path=Path(tab_recording.normalized_path or ""),
        )
    finally:
        pyannote.unload()


def _local_speaker(db: Session, meeting: Meeting) -> Speaker:
    """The person running the extension.

    Normally the content script already registered them from the meeting UI (the
    tile labelled "You"). If it could not, we still know they exist — their
    microphone is producing audio — so we create a placeholder rather than
    leaving their words unattributed.
    """
    local = db.execute(
        select(Speaker).where(
            Speaker.meeting_id == meeting.id, Speaker.is_local_user.is_(True)
        )
    ).scalar_one_or_none()

    if local is None:
        local = Speaker(
            meeting_id=meeting.id,
            display_name="You",
            is_local_user=True,
            source=SpeakerSource.LOCAL_TRACK,
        )
        db.add(local)
        db.flush()

    return local


def _persist_segments(
    db: Session,
    meeting: Meeting,
    segments: list[AttributedSegment],
    language: str | None,
) -> None:
    """Write the attributed transcript.

    Existing segments are cleared first, so re-running the job repairs a meeting
    rather than duplicating it. Note that this does discard manual speaker
    corrections: a re-run recomputes the segment boundaries, so the old segment a
    correction was attached to may no longer exist. Worth revisiting if
    reprocessing becomes routine — for now, reprocessing is a repair operation.
    """
    db.execute(delete(Segment).where(Segment.meeting_id == meeting.id))

    speakers = _speaker_index(db, meeting)

    for index, segment in enumerate(segments):
        speaker = None
        if segment.speaker_label:
            speaker = _resolve_speaker(db, meeting, speakers, segment)

        db.add(
            Segment(
                meeting_id=meeting.id,
                recording_id=segment.recording_id,
                speaker_id=speaker.id if speaker else None,
                index=index,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=segment.text,
                language=language,
                words=[asdict(w) for w in segment.words],
                speaker_source=segment.speaker_source,
                avg_logprob=segment.avg_logprob,
                no_speech_prob=segment.no_speech_prob,
            )
        )

    db.commit()
    logger.info("Persisted %d segments for meeting %s", len(segments), meeting.id)


def _speaker_index(db: Session, meeting: Meeting) -> dict[str, Speaker]:
    """Speakers for this meeting, keyed by display name."""
    return {
        s.display_name: s
        for s in db.execute(
            select(Speaker).where(Speaker.meeting_id == meeting.id)
        ).scalars()
    }


def _resolve_speaker(
    db: Session,
    meeting: Meeting,
    speakers: dict[str, Speaker],
    segment: AttributedSegment,
) -> Speaker:
    """Map an attributed label onto a Speaker row, creating it if it is new.

    A new name here is normal rather than exceptional. pyannote invents labels
    (SPEAKER_00) that nobody has registered, and a participant who joins late may
    speak before the roster has caught up.
    """
    label = segment.speaker_label
    assert label is not None  # guarded by the caller

    speaker = speakers.get(label)
    if speaker is None:
        speaker = Speaker(
            meeting_id=meeting.id,
            display_name=label,
            source=segment.speaker_source,
        )
        db.add(speaker)
        db.flush()  # need the id below
        speakers[label] = speaker

    return speaker


def run_translate(db: Session, meeting_id: uuid.UUID) -> None:
    """Translate a non-English transcript into English, then queue the minutes.

    Writes ``text_en`` onto every segment and hands off to MINUTES, which reads
    the English text. The original ``text`` is left untouched — it is what the
    minutes cite.
    """
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise ValueError(f"Meeting {meeting_id} does not exist.")

    segments = list(
        db.execute(
            select(Segment).where(Segment.meeting_id == meeting_id).order_by(Segment.start_ms)
        ).scalars()
    )
    if not segments:
        raise RuntimeError("Meeting has no transcript to translate.")

    # ``source_language`` was written by the transcribe stage. Fall back to the
    # segments' own language for the (unusual) case where it was not recorded, so
    # the model still gets told what it is reading.
    language = meeting.source_language or segments[0].language or "unknown"
    translate_segments(segments, source_language=language, model=settings.translation_model)

    db.commit()
    logger.info("Translated %d segments for meeting %s", len(segments), meeting_id)

    enqueue(db, meeting_id, JobType.MINUTES)
    db.commit()


def run_minutes(db: Session, meeting_id: uuid.UUID) -> None:
    """Generate minutes from the attributed transcript."""
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise ValueError(f"Meeting {meeting_id} does not exist.")

    segments = list(
        db.execute(
            select(Segment).where(Segment.meeting_id == meeting_id).order_by(Segment.start_ms)
        ).scalars()
    )
    if not segments:
        raise RuntimeError("Meeting has no transcript; nothing to summarise.")

    items = extract(segments, meeting_date=_meeting_date(meeting))
    summary = summarize(segments)

    # Versioned rather than overwritten: minutes that have already been
    # circulated should not silently change under the people who read them.
    previous = db.execute(
        select(Minutes.version)
        .where(Minutes.meeting_id == meeting_id)
        .order_by(Minutes.version.desc())
        .limit(1)
    ).scalar_one_or_none()

    minutes = Minutes(
        meeting_id=meeting_id,
        summary=summary,
        model=settings.minutes_model,
        version=(previous or 0) + 1,
    )
    db.add(minutes)
    db.flush()  # need minutes.id below

    speakers = _speaker_index(db, meeting)

    for item in items:
        owner = speakers.get(item.owner_name) if item.owner_name else None
        db.add(
            MinutesItem(
                minutes_id=minutes.id,
                type=item.type,
                text=item.text,
                owner_speaker_id=owner.id if owner else None,
                due_date=_parse_due_date(item.due_date),
                segment_ids=item.segment_ids,
            )
        )

    db.commit()
    logger.info("Extracted %d minutes items for meeting %s", len(items), meeting_id)

    # Nothing is shown to a user until it has been checked against its citations.
    enqueue(db, meeting_id, JobType.GROUND)
    db.commit()


def run_ground(db: Session, meeting_id: uuid.UUID) -> None:
    """Verify every minutes item against the segments it cites.

    Items that fail are **flagged, not deleted**. A user who can see what the
    verifier threw out has a reason to trust what it kept; silently dropping
    items just makes the minutes mysteriously incomplete.
    """
    minutes = db.execute(
        select(Minutes)
        .where(Minutes.meeting_id == meeting_id)
        .order_by(Minutes.version.desc())
        .limit(1)
    ).scalar_one_or_none()

    if minutes is None:
        raise RuntimeError("No minutes to ground; run the minutes job first.")

    # The verifier gets the same date anchor the extractor had — and nothing else
    # about the meeting. Without it, a correctly-resolved "by Friday" would be
    # rejected as an invented date; with the full transcript, the isolation that
    # makes the check worth running would be gone.
    meeting_date = _meeting_date(minutes.meeting)
    rejected = 0

    for row in minutes.items:
        cited = list(
            db.execute(
                select(Segment)
                .where(Segment.id.in_(row.segment_ids))
                .order_by(Segment.start_ms)
            ).scalars()
        )

        item = ExtractedItem(
            type=row.type,
            text=row.text,
            owner_name=row.owner.display_name if row.owner_speaker_id and row.owner else None,
            due_date=row.due_date.isoformat() if row.due_date else None,
            segment_ids=list(row.segment_ids),
        )

        verdict = verify(item, cited, meeting_date=meeting_date)
        row.is_grounded = verdict.is_grounded
        row.grounding_note = verdict.note
        if not verdict.is_grounded:
            rejected += 1

    db.commit()

    total = len(minutes.items)
    logger.info(
        "Grounding: %d/%d items rejected for meeting %s", rejected, total, meeting_id
    )
    if total and rejected == 0:
        # Not a cause for celebration. A grounding pass that never rejects
        # anything is either lucky or broken, and it is worth knowing which.
        logger.info("Grounding rejected nothing — worth spot-checking that it is working.")


def _meeting_date(meeting: Meeting) -> date | None:
    """The day the meeting happened, for resolving relative deadlines.

    Falls back to ``created_at`` for meetings recorded before ``started_at`` was
    populated. That is a fair approximation — the row is created when recording
    starts — and it beats losing every deadline in those meetings.
    """
    stamp = meeting.started_at or meeting.created_at
    return stamp.date() if stamp else None


def _parse_due_date(value: str | None) -> date | None:
    """Parse the model's ISO date, tolerating a malformed one by dropping it.

    A wrong deadline is worse than a missing one: somebody plans around it.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        logger.warning("Discarding unparseable due date %r", value)
        return None


#: Dispatch table the worker loop reads.
HANDLERS = {
    JobType.TRANSCRIBE: run_transcribe,
    JobType.TRANSLATE: run_translate,
    JobType.MINUTES: run_minutes,
    JobType.GROUND: run_ground,
}
