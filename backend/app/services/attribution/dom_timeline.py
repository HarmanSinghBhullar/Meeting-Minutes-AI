"""Primary attribution: the speaker timeline scraped from the meeting UI.

Google Meet, Zoom web, and Teams web all render a participant list with real
names and an active-speaker indicator. A content script logs the transitions,
which gives us a near-ground-truth timeline with actual human names in it. No
model, no GPU, no inference error.

This is the single biggest accuracy advantage of building the product as a
browser extension rather than a server that ingests audio files, and it is why
pyannote is the fallback here rather than the main path.

The work happens in the extension; this module just reads what it recorded and
hands it to the alignment step in the same shape pyannote would.
"""

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.speaker import Speaker, SpeakerEvent
from app.services.attribution.base import SpeakerTurn


class DomTimelineProvider:
    """Reads the recorded active-speaker events for a meeting."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def attribute(
        self, *, meeting_id: str, audio_path: Path | None = None
    ) -> list[SpeakerTurn]:
        """Return the DOM-derived speaker timeline, ordered by start time.

        ``audio_path`` is unused — this provider needs no audio at all, which is
        rather the point. It is in the signature to satisfy ``AttributionProvider``.

        Returns an empty list when the meeting has no recorded events, which is
        the worker's signal to fall back to diarization.
        """
        rows = self._db.execute(
            select(SpeakerEvent, Speaker)
            .join(Speaker, SpeakerEvent.speaker_id == Speaker.id)
            .where(SpeakerEvent.meeting_id == uuid.UUID(meeting_id))
            .order_by(SpeakerEvent.start_ms)
        ).all()

        return [
            SpeakerTurn(
                speaker_label=speaker.display_name,
                start_ms=event.start_ms,
                end_ms=event.end_ms,
                source=event.source,
            )
            for event, speaker in rows
        ]

    def has_timeline(self, *, meeting_id: str) -> bool:
        """Whether this meeting has a usable DOM timeline.

        The worker calls this to decide whether it needs to load pyannote at all.
        In the common case (a supported platform in the browser) it does not, and
        Whisper gets the whole GPU to itself.
        """
        return bool(
            self._db.execute(
                select(SpeakerEvent.id)
                .where(SpeakerEvent.meeting_id == uuid.UUID(meeting_id))
                .limit(1)
            ).first()
        )
