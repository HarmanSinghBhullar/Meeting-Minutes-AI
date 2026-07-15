"""Pydantic DTOs for the meeting and recording lifecycle."""

import uuid
from datetime import datetime

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.db.models.enums import JobStatus, JobType, Platform, SpeakerSource, Track


class SpeakerIn(BaseModel):
    """A participant, as reported by the content-script adapter."""

    display_name: str
    external_ref: str | None = None
    is_local_user: bool = False


class MeetingCreate(BaseModel):
    """Opens a meeting. Sent when recording starts, before any audio arrives.

    The extension generates the id client-side so it can start buffering audio
    immediately without waiting for a round trip.
    """

    id: uuid.UUID
    title: str | None = None
    platform: Platform = Platform.OTHER
    meeting_url: str | None = None
    agenda: str | None = None
    #: Participants read from the meeting UI at start. Also used to prime
    #: Whisper's decoder with the names it would otherwise mangle.
    speakers: list[SpeakerIn] = []


class SpeakerEventIn(BaseModel):
    """One active-speaker interval observed in the meeting UI.

    Times are milliseconds from the start of the recording. The content script
    batches these and posts them periodically rather than one at a time.
    """

    speaker_external_ref: str | None = None
    speaker_name: str
    start_ms: int
    end_ms: int
    #: How the name was determined. ``dom`` (the default) is the active-speaker
    #: signal; ``presenter`` marks audio attributed to the screen-sharer, which
    #: alignment ranks below any real speaking turn.
    source: SpeakerSource = SpeakerSource.DOM


class SpeakerEventBatch(BaseModel):
    """A batch of active-speaker events."""

    events: list[SpeakerEventIn]


class MeetingUpdate(BaseModel):
    """User-editable meeting fields. Rename is the only one for now."""

    #: Stripped before length-checking, so a whitespace-only name is rejected
    #: rather than silently stored as an empty title.
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]


class MeetingFinalize(BaseModel):
    """Closes a meeting: every chunk is uploaded, processing may begin."""

    ended_at: datetime | None = None


class SpeakerOut(BaseModel):
    """A participant."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    display_name: str
    source: SpeakerSource
    is_local_user: bool


class RecordingOut(BaseModel):
    """One audio track."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    track: Track
    duration_seconds: float | None
    chunk_count: int
    is_finalized: bool


class JobOut(BaseModel):
    """Pipeline job status, for the extension's progress display."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: JobType
    status: JobStatus
    progress: int
    error: str | None


class MeetingOut(BaseModel):
    """A meeting with its tracks, participants, and processing state."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    platform: Platform
    started_at: datetime | None
    ended_at: datetime | None
    source_language: str | None
    speakers: list[SpeakerOut] = []
    recordings: list[RecordingOut] = []
    jobs: list[JobOut] = []
