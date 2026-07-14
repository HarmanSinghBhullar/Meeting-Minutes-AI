"""Speaker attribution interface.

"Attribution" rather than "diarization" on purpose. Diarization is one *way* to
attribute speech, and in this product it is the worse one — it produces anonymous
clusters (SPEAKER_00, SPEAKER_01) that a human then has to name. The meeting UI
already knows who is talking and displays their real name, so our primary source
is the DOM timeline the content script records, and pyannote is the fallback for
audio that has no such timeline.

Both implement this interface, so the worker can pick a source per meeting.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.db.models.enums import SpeakerSource


@dataclass(slots=True)
class SpeakerTurn:
    """One interval attributed to one speaker.

    Times are in milliseconds from the start of the recording, matching the
    clock the content script and the transcript both use.
    """

    speaker_label: str  # a real name where we have one, else "SPEAKER_01"
    start_ms: int
    end_ms: int
    source: SpeakerSource


class AttributionProvider(Protocol):
    """Produces a timeline of who spoke when."""

    def attribute(self, *, meeting_id: str, audio_path: Path | None = None) -> list[SpeakerTurn]:
        """Return the speaker timeline for a meeting, ordered by start time."""
        ...
