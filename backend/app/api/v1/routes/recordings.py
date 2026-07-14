"""Recording lifecycle: open a meeting, stream chunks, finalize, process.

The upload endpoint takes *chunks*, not a finished file. A one-hour meeting is a
large blob to hold in browser memory and lose on a crash, so the extension
timeslices the recording and posts as it goes. A crash then costs the last few
seconds instead of the whole meeting.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.enums import JobType, SpeakerSource, Track
from app.db.models.meeting import Meeting
from app.db.models.recording import Recording
from app.db.models.speaker import Speaker, SpeakerEvent
from app.db.session import get_db
from app.schemas.meeting import (
    MeetingCreate,
    MeetingFinalize,
    MeetingOut,
    SpeakerEventBatch,
)
from app.services.audio import storage
from app.workers.queue import enqueue

router = APIRouter(prefix="/recordings", tags=["recordings"])


@router.post("/meetings", response_model=MeetingOut, status_code=status.HTTP_201_CREATED)
def open_meeting(payload: MeetingCreate, db: Session = Depends(get_db)) -> Meeting:
    """Open a meeting and register its participants.

    Called the moment recording starts. Idempotent on the client-generated id, so
    a retry after a flaky network does not create a second meeting.
    """
    existing = db.get(Meeting, payload.id)
    if existing:
        return existing

    meeting = Meeting(
        id=payload.id,
        title=payload.title,
        platform=payload.platform,
        meeting_url=payload.meeting_url,
        agenda=payload.agenda,
        # This endpoint is called at the moment recording starts, so now *is* the
        # start time. It was previously left null, which quietly cost us every
        # deadline in the meeting: with no anchor date, "by Friday" cannot be
        # resolved to a real one, and the extractor correctly refuses to guess.
        started_at=datetime.now(timezone.utc),
    )
    db.add(meeting)

    for s in payload.speakers:
        db.add(
            Speaker(
                meeting_id=meeting.id,
                display_name=s.display_name,
                external_ref=s.external_ref,
                is_local_user=s.is_local_user,
                # A name read from the meeting's own participant list is not an
                # inference — it is what the platform says the person is called.
                source=SpeakerSource.DOM,
            )
        )

    # One recording row per track. Kept separate rather than mixed: the mic track
    # is the local user by definition and the tab track is everyone else, which
    # hands us an exact speaker boundary with no model involved.
    for track in (Track.MIC, Track.TAB):
        db.add(Recording(meeting_id=meeting.id, track=track, mime_type="audio/webm"))

    db.commit()
    db.refresh(meeting)
    return meeting


@router.post("/meetings/{meeting_id}/chunks", status_code=status.HTTP_202_ACCEPTED)
async def upload_chunk(
    meeting_id: uuid.UUID,
    track: Track = Form(...),
    chunk: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """Append one timesliced audio chunk to a track."""
    recording = db.execute(
        select(Recording).where(
            Recording.meeting_id == meeting_id, Recording.track == track
        )
    ).scalar_one_or_none()

    if recording is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No {track.value} track for that meeting.")

    data = await chunk.read()
    path = storage.append_chunk(meeting_id, track, data)

    recording.chunk_count += 1
    recording.source_path = str(path)
    recording.size_bytes = storage.size_bytes(path)
    db.commit()

    return {"chunk_count": recording.chunk_count}


@router.post("/meetings/{meeting_id}/speaker-events", status_code=status.HTTP_202_ACCEPTED)
def post_speaker_events(
    meeting_id: uuid.UUID,
    payload: SpeakerEventBatch,
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """Record active-speaker intervals observed in the meeting UI.

    This is the primary attribution signal — real names, straight from the
    platform, with no model in the loop. Posted in batches during the meeting.

    A participant who joins mid-meeting appears here before they appear anywhere
    else, so unknown names are created rather than rejected.
    """
    speakers = {
        s.display_name: s
        for s in db.execute(
            select(Speaker).where(Speaker.meeting_id == meeting_id)
        ).scalars()
    }

    for event in payload.events:
        speaker = speakers.get(event.speaker_name)
        if speaker is None:
            speaker = Speaker(
                meeting_id=meeting_id,
                display_name=event.speaker_name,
                external_ref=event.speaker_external_ref,
                source=SpeakerSource.DOM,
            )
            db.add(speaker)
            db.flush()  # need the id below
            speakers[event.speaker_name] = speaker

        db.add(
            SpeakerEvent(
                meeting_id=meeting_id,
                speaker_id=speaker.id,
                start_ms=event.start_ms,
                end_ms=event.end_ms,
                source=SpeakerSource.DOM,
            )
        )

    db.commit()
    return {"accepted": len(payload.events)}


@router.post("/meetings/{meeting_id}/finalize", response_model=MeetingOut)
def finalize_meeting(
    meeting_id: uuid.UUID,
    payload: MeetingFinalize,
    db: Session = Depends(get_db),
) -> Meeting:
    """Close the recording and queue it for processing.

    Returns immediately with a job the extension can poll. Transcription takes
    minutes and cannot happen inside a request.
    """
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")

    meeting.ended_at = payload.ended_at
    for recording in meeting.recordings:
        # Only tracks that actually received audio. A user with no microphone
        # produces no mic chunks, and that is a legitimate meeting, not an error.
        if recording.chunk_count > 0:
            recording.is_finalized = True

    enqueue(db, meeting.id, JobType.TRANSCRIBE)
    db.commit()
    db.refresh(meeting)
    return meeting
