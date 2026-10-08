"""The speaker-mapping gate.

One question — "is this meeting still waiting for a human to name a voice?" — and
it is asked from three places: the worker, before handing off to the minutes; the
regenerate route, before re-queuing them; and the resolve route, to notice it has
just been answered.

It lives here, rather than in ``workers/pipeline``, for an unglamorous but load-
bearing reason: the API imports it. ``pipeline`` imports faster-whisper at module
scope, so a route importing from it would pull torch and a Whisper backend into
the API process at startup — a multi-second import and a hard dependency, in a
process that never transcribes anything. This module imports only the ORM.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.enums import JobStatus, JobType, SpeakerSource
from app.db.models.job import Job
from app.db.models.speaker import Speaker


def unmapped_cluster_count(db: Session, meeting_id: uuid.UUID) -> int:
    """How many diarization clusters are still waiting for a human to name them.

    ``source == DIARIZATION`` means "pyannote found this voice and nobody has said
    whose it is". Every way of resolving one moves it off that value — a merge
    deletes the row outright, a rename or an ignore rewrites it to MANUAL — so
    this count reaching zero *is* the mapping being complete. There is deliberately
    no second ``needs_mapping`` flag: it would be a copy of this fact, free to
    drift out of step with it, and needing a backfill to exist at all.
    """
    return db.execute(
        select(func.count())
        .select_from(Speaker)
        .where(
            Speaker.meeting_id == meeting_id,
            Speaker.source == SpeakerSource.DIARIZATION,
        )
    ).scalar_one()


def minutes_already_owned(db: Session, meeting_id: uuid.UUID) -> bool:
    """True when some other stage is already going to queue the minutes.

    There are two doors into the minutes job and they can both be standing open at
    once. A non-English meeting hands off to TRANSLATE, and ``run_translate``
    queues the minutes when it finishes. But the user can finish naming the
    speakers *while that translation is still running* — and then the resolve
    route would queue the minutes too, and the meeting would be summarised twice:
    two LLM runs, two bills, two versions of the truth circulated to readers.

    So the resolve route asks this first and defers to the pipeline when the
    pipeline still owes work. Nothing is lost by deferring: TRANSLATE re-checks
    the gate on its way out, and by then it is clear.

    Deliberately *not* consulted by ``run_translate`` itself — its own job row is
    RUNNING while it executes, so it would see itself and never hand off.
    """
    active = (JobStatus.PENDING, JobStatus.RUNNING)
    return (
        db.execute(
            select(Job.id).where(
                Job.meeting_id == meeting_id,
                Job.type.in_((JobType.TRANSLATE, JobType.MINUTES)),
                Job.status.in_(active),
            )
        ).first()
        is not None
    )
