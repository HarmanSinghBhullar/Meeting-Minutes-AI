"""End-to-end smoke test for the transcription pipeline.

Pushes two audio files through the real pipeline — ffmpeg, Whisper, alignment,
Postgres — and prints the transcript that comes out. Exercises the plumbing
without needing the extension, so the two can be debugged independently.

    python scripts/smoke_transcribe.py <mic.wav> <tab.wav>

The mic track stands in for the local user and the tab track for the remote
participants, exactly as the extension would upload them.
"""

import shutil
import sys
import uuid
from pathlib import Path

from sqlalchemy import select

from app.db.models.enums import Platform, Track
from app.db.models.meeting import Meeting
from app.db.models.recording import Recording
from app.db.models.segment import Segment
from app.db.models.speaker import Speaker
from app.db.session import SessionLocal
from app.services.audio import storage
from app.workers.pipeline import run_transcribe


def main(mic: Path, tab: Path) -> None:
    db = SessionLocal()
    meeting_id = uuid.uuid4()

    try:
        meeting = Meeting(
            id=meeting_id,
            title="Weekly sync",
            platform=Platform.MEET,
            # Primes Whisper's decoder. Without it, "ChromaDB" reliably comes out
            # as "chroma DB", "chrome a DB", or worse — and it is exactly the kind
            # of word the minutes turn on.
            agenda="ChromaDB migration, Q3 roadmap",
        )
        db.add(meeting)
        db.add(Speaker(meeting_id=meeting_id, display_name="Harman", is_local_user=True))

        # Stage the audio where the upload endpoint would have put it.
        for track, source in ((Track.MIC, mic), (Track.TAB, tab)):
            dest = storage.track_path(meeting_id, track, suffix=source.suffix)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, dest)

            db.add(
                Recording(
                    meeting_id=meeting_id,
                    track=track,
                    source_path=str(dest),
                    mime_type="audio/wav",
                    chunk_count=1,
                    is_finalized=True,
                )
            )
        db.commit()

        print(f"Meeting {meeting_id}\nRunning pipeline...\n")
        run_transcribe(db, meeting_id)

        segments = db.execute(
            select(Segment).where(Segment.meeting_id == meeting_id).order_by(Segment.start_ms)
        ).scalars().all()

        print(f"--- transcript ({len(segments)} segments) ---")
        for seg in segments:
            speaker = seg.speaker.display_name if seg.speaker else "UNKNOWN"
            start = seg.start_ms / 1000
            print(f"[{start:6.2f}s] {speaker:>8} ({seg.speaker_source.value}): {seg.text}")

        db.refresh(meeting)
        print(f"\nDetected language: {meeting.source_language}")
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python scripts/smoke_transcribe.py <mic.wav> <tab.wav>")
    main(Path(sys.argv[1]), Path(sys.argv[2]))
