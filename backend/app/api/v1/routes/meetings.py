"""Read-side routes: meetings, transcripts, and speaker corrections."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.enums import SpeakerSource
from app.db.models.meeting import Meeting
from app.db.models.segment import Segment
from app.db.session import get_db
from app.schemas.meeting import MeetingOut
from app.schemas.transcript import SegmentCorrection, SegmentOut

router = APIRouter(prefix="/meetings", tags=["meetings"])


@router.get("", response_model=list[MeetingOut])
def list_meetings(db: Session = Depends(get_db), limit: int = 50) -> list[Meeting]:
    """List recent meetings, newest first."""
    return list(
        db.execute(
            select(Meeting).order_by(Meeting.created_at.desc()).limit(limit)
        ).scalars()
    )


@router.get("/{meeting_id}", response_model=MeetingOut)
def get_meeting(meeting_id: uuid.UUID, db: Session = Depends(get_db)) -> Meeting:
    """Fetch one meeting with its tracks, participants, and job status."""
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")
    return meeting


@router.get("/{meeting_id}/transcript", response_model=list[SegmentOut])
def get_transcript(meeting_id: uuid.UUID, db: Session = Depends(get_db)) -> list[Segment]:
    """The full attributed transcript, in time order."""
    return list(
        db.execute(
            select(Segment)
            .where(Segment.meeting_id == meeting_id)
            .order_by(Segment.start_ms)
        ).scalars()
    )


@router.patch("/{meeting_id}/segments/{segment_id}/speaker", response_model=SegmentOut)
def correct_speaker(
    meeting_id: uuid.UUID,
    segment_id: uuid.UUID,
    payload: SegmentCorrection,
    db: Session = Depends(get_db),
) -> Segment:
    """Reassign a segment to a different speaker.

    Marks the attribution MANUAL, which outranks every inferred source and is not
    overwritten if the meeting is reprocessed. Human corrections are also the raw
    material of the evaluation set: they are, by definition, labelled data about
    exactly the cases the pipeline got wrong.
    """
    segment = db.get(Segment, segment_id)
    if segment is None or segment.meeting_id != meeting_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Segment not found.")

    segment.speaker_id = payload.speaker_id
    segment.speaker_source = SpeakerSource.MANUAL
    db.commit()
    db.refresh(segment)
    return segment
