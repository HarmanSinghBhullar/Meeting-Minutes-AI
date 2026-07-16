"""Speaker attribution interface.

"Attribution" rather than "diarization" on purpose: diarization is one *way* to
attribute speech, and the interface should not be named after the implementation
that happens to be behind it today.

There is one implementation right now — pyannote. There used to be a second, a
provider that read the active-speaker timeline scraped out of the meeting UI's
DOM, and it was preferred because it produced real names for free. It was removed
in favour of diarizing every meeting: the scrape depended on obfuscated CSS class
names that the platforms change without notice, and when they changed it failed
*silently*, attributing everyone to "Unknown" until somebody noticed the minutes
had no owners. Naming clusters by hand is a known, bounded cost; that was not.

The Protocol stays because the contract is worth stating explicitly, and because
the next source of attribution (a per-participant audio track, if a platform ever
offers one) would slot in here.
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

    def attribute(
        self,
        *,
        meeting_id: str,
        audio_path: Path | None = None,
        max_speakers: int | None = None,
    ) -> list[SpeakerTurn]:
        """Return the speaker timeline for a meeting, ordered by start time.

        Args:
            meeting_id: The meeting being attributed.
            audio_path: The normalized track, for providers that read audio.
            max_speakers: Upper bound on distinct speakers, where the caller knows
                one. A hint, never a guarantee — a provider may return more.
        """
        ...
