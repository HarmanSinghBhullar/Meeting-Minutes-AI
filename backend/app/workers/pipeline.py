"""The processing pipeline: what actually happens to a recording.

Stage order is forced by data dependencies:

    normalize -> transcribe -> attribute -> align -> [translate] -> [MAPPING] -> minutes -> ground

Only ``transcribe``, ``translate``, ``minutes`` and ``ground`` are jobs. The rest
are function calls inside ``run_transcribe``, because they share the audio and
the model and there is nothing to gain from making the worker re-load both.

**The mapping gate.** Diarization can tell us that three distinct voices spoke;
it cannot tell us their names. So a diarized meeting stops after the transcript
and waits for a human to say which cluster is Priya — and only then are the
minutes written. This is deliberate and it is the point of the whole design:
minutes are read for "who owns this", and minutes attributing a decision to
SPEAKER_01 are not a rough draft of the right answer, they are a wrong one that
looks authoritative. Better to ask a question than to publish a guess.

The gate is ``Speaker.source == DIARIZATION`` — an unnamed cluster row *is* the
unmapped state. Naming it clears the gate, and ``run_minutes`` is enqueued by
whoever cleared the last one (see ``api/v1/routes/speakers.py``).

**GPU discipline.** A 4GB card cannot hold Whisper and pyannote at once. This is
not a reason to shrink Whisper — the model size is what buys us the proper nouns
the minutes are made of. Instead, only one model is resident at a time: each
stage unloads before the next loads. Paying 10-30 seconds of model load on a job
that already takes minutes is not worth optimizing.

VRAM is not the only reason they cannot share, though, and the other reason is
harsher: **faster-whisper (CTranslate2) and pyannote (torch) cannot both
initialise cuDNN in one process.** Once Whisper has touched the GPU, loading
torch's cuDNN kills the interpreter outright — exit 127, no traceback, nothing to
catch. Diarization therefore runs in a child process; see
``services/attribution/diarize_cli``. Nothing in this module may import torch.
"""

import logging
import uuid
from dataclasses import asdict
from datetime import date
from pathlib import Path

from sqlalchemy import delete, func, select
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
from app.services.attribution.mapping import unmapped_cluster_count
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
        (
            results[t].language
            for t in (Track.TAB, Track.MIC)
            if t in results and results[t].language
        ),
        None,
    )
    meeting.source_language = language

    attributed = _attribute(db, meeting, recordings, results)
    _persist_segments(db, meeting, attributed, language)

    # Translation must land before the minutes, because the minutes are written
    # from the English text. It is speaker-independent — it transduces text, and
    # does not care who said it — so it runs ahead of the mapping gate rather than
    # behind it. That ordering is what lets the user read the transcript in
    # English while they are naming the voices in it.
    if needs_translation(language):
        enqueue(db, meeting.id, JobType.TRANSLATE)
    else:
        _enqueue_minutes_unless_unmapped(db, meeting.id)
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
    """Get the speaker timeline for the remote participants, by diarizing.

    This used to prefer the DOM active-speaker timeline, which was free and
    already carried real names. It was dropped because of *how* it failed. The
    timeline came from scraping per-frame CSS classes out of the meeting UI, and
    the platforms obfuscate and reskin those without notice. When that happened
    the scrape did not error — it simply stopped matching, emitted nothing, and
    every remote speaker came out "Unknown". A signal that breaks silently, in
    production, on the one axis users check first, is not a signal worth
    preferring: the failure surfaces days later in a set of minutes with no
    owners, and the fix is a reverse-engineering session against a live call.

    Diarization is the trade taken instead. It reads the audio, so no CSS change
    can break it; it costs GPU minutes and cannot produce names, so a human maps
    the clusters afterwards. A predictable manual step beats an unpredictable
    silent failure.

    The mic track never comes here — it is the local user by definition — so
    these clusters are always remote participants, and the user is never asked to
    identify themselves.
    """
    if not settings.diarization_enabled:
        logger.warning(
            "DIARIZATION_ENABLED is false; the tab track has no attribution source, "
            "so every remote speaker will be Unknown."
        )
        return []

    pyannote = PyannoteProvider()
    try:
        return pyannote.attribute(
            meeting_id=str(meeting.id),
            audio_path=Path(tab_recording.normalized_path or ""),
            max_speakers=_remote_speaker_bound(db, meeting),
        )
    finally:
        pyannote.unload()


def _remote_speaker_bound(db: Session, meeting: Meeting) -> int | None:
    """An upper bound on how many distinct remote voices the tab track can hold.

    The roster is the thing the DOM still gives us reliably — it is read once,
    from the participant list, not sampled from a per-frame CSS class — so it is
    worth spending on the one diarization parameter that most affects the result.
    Unbounded clustering is what turns one person whose microphone drifts into two
    speakers, and a duplicate is the error users notice, because it lands in front
    of them as the same face twice in the mapping UI.

    Bounded, never exact, and with a spare seat: the roster is a snapshot from the
    start of the call and can genuinely undercount (someone joins late, two people
    share a laptop). Passing ``num_speakers`` on a roster that is wrong forces the
    diarizer to split or merge real people to hit the number; a slightly generous
    ceiling only removes the absurd end of the space. Returns None for an empty
    roster, which leaves pyannote unconstrained — the honest answer when we know
    nothing.
    """
    roster = db.execute(
        select(func.count())
        .select_from(Speaker)
        .where(
            Speaker.meeting_id == meeting.id,
            Speaker.is_local_user.is_(False),
            # Clusters left over from an earlier run of this same meeting are not
            # roster entries, and counting them would inflate the bound every
            # time a meeting is reprocessed.
            Speaker.source != SpeakerSource.DIARIZATION,
        )
    ).scalar_one()

    return roster + 1 if roster else None


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


def _enqueue_minutes_unless_unmapped(db: Session, meeting_id: uuid.UUID) -> None:
    """Queue the minutes, unless the meeting is still waiting on speaker mapping.

    Stopping here is a successful outcome, not a failure: the job that called this
    did its work, and the pipeline is now legitimately waiting on a person. So it
    logs and returns rather than raising — a failed job would light the meeting up
    red on the dashboard and invite someone to retry it, when what is actually
    needed is for a human to name three voices.

    The queue restarts from ``routes/speakers.py`` when the last cluster is named.
    """
    pending = unmapped_cluster_count(db, meeting_id)
    if pending:
        logger.info(
            "Meeting %s has %d unnamed speaker cluster(s); holding the minutes "
            "until they are mapped.",
            meeting_id,
            pending,
        )
        return

    enqueue(db, meeting_id, JobType.MINUTES)


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

    # Clusters from a previous run of this same meeting go with the segments that
    # referenced them. Cluster numbering is not stable across runs — SPEAKER_01 in
    # this run need not be SPEAKER_01 in the last — so keeping the old rows would
    # let a fresh cluster inherit a name that was checked against different audio.
    # A wrong name nobody was asked to confirm is worse than an honest re-ask.
    db.execute(
        delete(Speaker).where(
            Speaker.meeting_id == meeting.id,
            Speaker.source == SpeakerSource.DIARIZATION,
        )
    )

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

    The handoff is conditional: a meeting whose clusters are still unnamed stops
    here and waits for the mapping UI.
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

    _enqueue_minutes_unless_unmapped(db, meeting_id)
    db.commit()


def run_minutes(db: Session, meeting_id: uuid.UUID) -> None:
    """Generate minutes from the attributed and speaker-mapped transcript."""
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise ValueError(f"Meeting {meeting_id} does not exist.")

    # The API refuses to queue this job while clusters are unnamed, so reaching
    # here means something bypassed it. Fail loudly rather than write minutes that
    # credit SPEAKER_01: this is the one guarantee the mapping gate exists to make,
    # and a guarantee enforced only at the edge is not enforced.
    unmapped = unmapped_cluster_count(db, meeting_id)
    if unmapped:
        raise RuntimeError(
            f"Meeting {meeting_id} has {unmapped} unnamed speaker cluster(s). "
            "Map them to participants before generating minutes."
        )

    segments = list(
        db.execute(
            select(Segment).where(Segment.meeting_id == meeting_id).order_by(Segment.start_ms)
        ).scalars()
    )
    if not segments:
        raise RuntimeError("Meeting has no transcript; nothing to summarise.")

    minuted = _minutable(segments)
    if not minuted:
        raise RuntimeError(
            "Every segment of this meeting belongs to a speaker marked 'not a "
            "participant'; there is nothing to summarise."
        )

    items = extract(minuted, meeting_date=_meeting_date(meeting))
    summary = summarize(minuted)

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


def _minutable(segments: list[Segment]) -> list[Segment]:
    """Drop segments belonging to speakers a human marked as not-a-participant.

    A cluster can legitimately be a shared YouTube clip, hold music, or a
    speakerphone carrying another room's meeting. Its words were genuinely in the
    recording, so they stay in the transcript — the transcript's job is to reflect
    the audio. But the minutes are a record of what *this meeting* decided, and a
    played video does not decide anything, let alone accept an action item.

    Excluding here rather than in the extractor also removes the excluded speaker
    from the roster the model is shown, which is what stops it assigning owners
    that were never in the room.
    """
    kept = [s for s in segments if not (s.speaker and s.speaker.is_excluded)]

    dropped = len(segments) - len(kept)
    if dropped:
        logger.info("Withholding %d segment(s) from excluded speakers", dropped)
    return kept


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
