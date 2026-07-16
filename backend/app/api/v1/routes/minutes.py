"""Minutes routes — the product."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.enums import JobType
from app.db.models.minutes import Minutes
from app.db.session import get_db
from app.schemas.meeting import JobOut
from app.schemas.transcript import MinutesOut
from app.services.attribution.mapping import unmapped_cluster_count
from app.workers.queue import enqueue

router = APIRouter(prefix="/meetings", tags=["minutes"])


@router.get("/{meeting_id}/minutes", response_model=MinutesOut)
def get_minutes(meeting_id: uuid.UUID, db: Session = Depends(get_db)) -> Minutes:
    """The latest minutes for a meeting.

    Items carry their citations and their grounding verdict, so the UI can show
    the transcript line behind any claim and flag anything the verifier could not
    support. Minutes you cannot check are minutes you end up not trusting.
    """
    minutes = db.execute(
        select(Minutes)
        .where(Minutes.meeting_id == meeting_id)
        .order_by(Minutes.version.desc())
        .limit(1)
    ).scalar_one_or_none()

    if minutes is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No minutes yet. They are generated after transcription completes.",
        )
    return minutes


@router.post("/{meeting_id}/minutes/regenerate", response_model=JobOut, status_code=202)
def regenerate_minutes(meeting_id: uuid.UUID, db: Session = Depends(get_db)) -> object:
    """Re-run minutes generation over the existing transcript.

    Writes a new version rather than overwriting: minutes that have already been
    circulated should not silently change underneath the people who read them.

    Refuses while any speaker cluster is unnamed. This is the second door into the
    minutes job — the pipeline's own handoff is the first — and a gate that only
    covers one door is decoration.
    """
    unmapped = unmapped_cluster_count(db, meeting_id)
    if unmapped:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{unmapped} speaker{'' if unmapped == 1 else 's'} in this meeting "
            "still need identifying. Map them to participants first — minutes that "
            "credit 'SPEAKER_01' are worse than none.",
        )

    return enqueue(db, meeting_id, JobType.MINUTES)
