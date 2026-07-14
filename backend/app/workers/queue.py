"""Postgres-backed job queue.

No Redis, no broker. ``SELECT ... FOR UPDATE SKIP LOCKED`` gives us a queue with
at-least-once delivery, crash recovery, and multiple workers, using the database
we already run. Adding a broker would buy throughput we do not need — the GPU is
the bottleneck and it can only do one thing at a time anyway.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.enums import JobStatus, JobType
from app.db.models.job import Job

MAX_ATTEMPTS = 3


def enqueue(db: Session, meeting_id: uuid.UUID, job_type: JobType) -> Job:
    """Add a job to the queue."""
    job = Job(meeting_id=meeting_id, type=job_type, status=JobStatus.PENDING)
    db.add(job)
    db.commit()
    return job


def claim_next(db: Session) -> Job | None:
    """Atomically claim the oldest pending job.

    ``SKIP LOCKED`` is what makes this safe to run from several workers at once:
    each one takes a different row instead of blocking on the same one.
    """
    job = db.execute(
        select(Job)
        .where(Job.status == JobStatus.PENDING)
        .order_by(Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()

    if job is None:
        return None

    job.status = JobStatus.RUNNING
    job.attempts += 1
    job.started_at = datetime.now(timezone.utc)
    db.commit()
    return job


def succeed(db: Session, job: Job) -> None:
    """Mark a job done."""
    job.status = JobStatus.SUCCEEDED
    job.progress = 100
    job.finished_at = datetime.now(timezone.utc)
    db.commit()


def fail(db: Session, job: Job, error: str) -> None:
    """Record a failure, retrying until ``MAX_ATTEMPTS``.

    The error text is kept on the row rather than only in the logs: the extension
    shows it, because "transcription failed because ffmpeg is not installed" is
    something the user can act on and "failed" is not.
    """
    job.error = error[:4000]
    if job.attempts < MAX_ATTEMPTS:
        job.status = JobStatus.PENDING  # back on the queue
    else:
        job.status = JobStatus.FAILED
        job.finished_at = datetime.now(timezone.utc)
    db.commit()


def set_progress(db: Session, job: Job, progress: int) -> None:
    """Update a job's progress, for the extension's progress bar."""
    job.progress = max(0, min(100, progress))
    db.commit()
