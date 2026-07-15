"""Audio blob storage.

Audio lives on disk; Postgres stores the path. Putting multi-hundred-megabyte
WebM blobs in the database would bloat backups and slow every query that touches
the meetings table, for no benefit.

Chunks arrive while the meeting is still running and are appended to a single
per-track file. WebM is a streaming container, so appending the chunks a
timesliced ``MediaRecorder`` emits produces a valid file — which is what lets us
survive a browser crash with only the last few seconds lost.

The interface is deliberately narrow so an S3 backend can replace it later.
"""

import shutil
import uuid
from pathlib import Path

from app.core.config import settings
from app.db.models.enums import Track


def _meeting_dir(meeting_id: uuid.UUID) -> Path:
    return settings.storage_dir / str(meeting_id)


def delete_meeting(meeting_id: uuid.UUID) -> None:
    """Remove every stored file for a meeting. Idempotent.

    Called when a meeting is deleted from the dashboard: the database row cascades
    away on its own, but the audio blobs live on disk and would otherwise leak.
    """
    directory = _meeting_dir(meeting_id)
    if directory.is_dir():
        shutil.rmtree(directory)


def track_path(meeting_id: uuid.UUID, track: Track, suffix: str = ".webm") -> Path:
    """Path to the assembled source file for one track of a meeting."""
    return _meeting_dir(meeting_id) / f"{track.value}{suffix}"


def normalized_path(meeting_id: uuid.UUID, track: Track) -> Path:
    """Path to the ffmpeg-normalized WAV Whisper reads."""
    return _meeting_dir(meeting_id) / f"{track.value}.16k.wav"


def append_chunk(meeting_id: uuid.UUID, track: Track, data: bytes) -> Path:
    """Append one uploaded chunk to its track file, creating it if needed.

    Returns the path written to.
    """
    path = track_path(meeting_id, track)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as fh:
        fh.write(data)
    return path


def size_bytes(path: Path) -> int:
    """Size of a stored file, or 0 if it does not exist yet."""
    return path.stat().st_size if path.exists() else 0
