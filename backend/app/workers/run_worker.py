"""Worker entry point.

Run alongside the API:

    python -m app.workers.run_worker

Separate process on purpose. Transcription pins the GPU for minutes at a time,
and it must not be able to starve the API that the extension is uploading chunks
to while the meeting is still running.
"""

import logging
import time

from app.db.session import SessionLocal
from app.workers.pipeline import HANDLERS
from app.workers.queue import claim_next, fail, succeed

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2.0


def main() -> None:
    """Claim and run jobs until interrupted."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger.info("Worker started.")

    while True:
        db = SessionLocal()
        try:
            job = claim_next(db)
            if job is None:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            handler = HANDLERS.get(job.type)
            if handler is None:
                fail(db, job, f"No handler registered for job type {job.type.value}.")
                continue

            logger.info("Running %s for meeting %s", job.type.value, job.meeting_id)
            try:
                handler(db, job.meeting_id)
                succeed(db, job)
                logger.info("Finished %s for meeting %s", job.type.value, job.meeting_id)
            except Exception as exc:  # noqa: BLE001 — a worker must survive any job
                logger.exception("Job %s failed", job.id)
                fail(db, job, str(exc))
        finally:
            db.close()


if __name__ == "__main__":
    main()
