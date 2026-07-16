"""Speaker mapping: turning anonymous voices into named people.

This is the manual step diarization forces on us, and the pipeline deliberately
stops in front of it. ``run_transcribe`` leaves a meeting holding a set of
clusters — SPEAKER_00, SPEAKER_01 — and no minutes. The last resolution posted
here clears the gate and queues them.

That the *user* restarts the pipeline, rather than a poller noticing the gate is
clear, is the design: the click that names the last voice is the click that means
"this is right, go". There is no separate confirm step to forget.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models.enums import JobType, SpeakerSource
from app.db.models.meeting import Meeting
from app.db.models.segment import Segment
from app.db.models.speaker import Speaker
from app.db.session import get_db
from app.schemas.speaker import SpeakerClusterOut, SpeakerMappingOut, SpeakerResolution
from app.services.attribution.mapping import minutes_already_owned, unmapped_cluster_count
from app.workers.queue import enqueue

router = APIRouter(prefix="/meetings", tags=["speakers"])

#: How many of a cluster's lines to show. Enough to recognise a voice, few enough
#: to read at a glance — the user is identifying a person, not reading the
#: transcript, which is on the same page anyway.
SAMPLE_COUNT = 3

#: Samples are for recognition, not comprehension. A long quote is truncated
#: rather than dropped: the first clause is usually the identifying part.
SAMPLE_MAX_CHARS = 160


@router.get("/{meeting_id}/speaker-mapping", response_model=SpeakerMappingOut)
def get_speaker_mapping(
    meeting_id: uuid.UUID, db: Session = Depends(get_db)
) -> SpeakerMappingOut:
    """The unnamed clusters and the participants they might be.

    One request rather than two, because the UI is useless with half of it: a
    cluster with no candidate list is unanswerable, and a candidate list with no
    clusters has nothing to answer.
    """
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")

    return _mapping_state(db, meeting)


@router.post(
    "/{meeting_id}/speakers/{speaker_id}/resolve",
    response_model=SpeakerMappingOut,
)
def resolve_speaker(
    meeting_id: uuid.UUID,
    speaker_id: uuid.UUID,
    payload: SpeakerResolution,
    db: Session = Depends(get_db),
) -> SpeakerMappingOut:
    """Identify one cluster, and queue the minutes if it was the last one.

    Returns the whole mapping state rather than the row that changed: a merge
    deletes a speaker and may complete the gate, so "what did that do" is a
    question about the meeting, not about the row.
    """
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found.")

    cluster = db.get(Speaker, speaker_id)
    if cluster is None or cluster.meeting_id != meeting_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Speaker not found.")

    if cluster.source != SpeakerSource.DIARIZATION:
        # Already resolved — most likely a double-click, or two tabs open on the
        # same meeting. Not an error worth alarming anyone about, but not a no-op
        # either: silently re-resolving would let the second tab's stale choice
        # overwrite the first tab's correct one.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"'{cluster.display_name}' is not an unnamed cluster; it has already "
            "been identified.",
        )

    if payload.target_speaker_id is not None:
        _merge_into(db, cluster, payload.target_speaker_id, meeting_id)
    elif payload.ignore:
        _mark_excluded(db, cluster)
    else:
        assert payload.display_name is not None  # guaranteed by the validator
        _name_cluster(db, cluster, payload.display_name.strip())

    db.flush()

    # The gate is cleared by whoever resolves the last cluster, so the check has
    # to happen here, inside the same transaction as the resolution.
    #
    # ...unless an earlier stage still owes work and will queue the minutes on its
    # own way out. A non-English meeting is translating while the user names its
    # speakers, and queuing here as well would summarise the meeting twice.
    queued = False
    if unmapped_cluster_count(db, meeting_id) == 0 and not minutes_already_owned(
        db, meeting_id
    ):
        enqueue(db, meeting_id, JobType.MINUTES)
        queued = True

    db.commit()
    db.refresh(meeting)

    state = _mapping_state(db, meeting)
    state.minutes_queued = queued
    return state


def _merge_into(
    db: Session, cluster: Speaker, target_id: uuid.UUID, meeting_id: uuid.UUID
) -> None:
    """This voice belongs to a participant we already know about.

    The cluster row is deleted, not renamed. Renaming it would leave the meeting
    with two Speaker rows for one person — the roster's "Priya Nair" and a second
    "Priya Nair" that used to be SPEAKER_01 — and every consumer that groups by
    speaker would then show her twice. The row was only ever a placeholder for an
    unanswered question; once answered, it has nothing left to hold.
    """
    target = db.get(Speaker, target_id)
    if target is None or target.meeting_id != meeting_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That participant is not in this meeting.",
        )
    if target.id == cluster.id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "A cluster cannot be merged into itself."
        )
    if target.source == SpeakerSource.DIARIZATION:
        # Two clusters are two voices by construction. Merging one into the other
        # would be asserting the diarizer over-split — which may well be true, but
        # it is not what this endpoint means, and allowing it here would let a user
        # collapse the roster into a single unnamed blob.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Map this voice to a participant, not to another unnamed cluster.",
        )

    db.execute(
        update(Segment)
        .where(Segment.speaker_id == cluster.id)
        .values(speaker_id=target.id, speaker_source=SpeakerSource.MANUAL)
    )
    db.delete(cluster)


def _name_cluster(db: Session, cluster: Speaker, display_name: str) -> None:
    """This voice is a real person the roster missed. Give the row their name."""
    cluster.display_name = display_name
    cluster.source = SpeakerSource.MANUAL
    _mark_segments_manual(db, cluster)


def _mark_excluded(db: Session, cluster: Speaker) -> None:
    """This voice is not a participant — a shared video, hold music, another room.

    Kept rather than deleted, so its segments keep a speaker and stay readable in
    the transcript. ``is_excluded`` is what withholds them from the minutes.
    """
    cluster.source = SpeakerSource.MANUAL
    cluster.is_excluded = True
    _mark_segments_manual(db, cluster)


def _mark_segments_manual(db: Session, cluster: Speaker) -> None:
    """Record that these segments' attribution is now a human's assertion.

    The source matters downstream: MANUAL outranks every inferred source, and it
    is also the label on the only data we have about cases the pipeline got wrong.
    """
    db.execute(
        update(Segment)
        .where(Segment.speaker_id == cluster.id)
        .values(speaker_source=SpeakerSource.MANUAL)
    )


def _mapping_state(db: Session, meeting: Meeting) -> SpeakerMappingOut:
    """Assemble the clusters and candidates for one meeting."""
    stats = _cluster_stats(db, meeting.id)

    clusters = [
        SpeakerClusterOut(
            id=speaker.id,
            display_name=speaker.display_name,
            segment_count=count,
            total_ms=total_ms,
            samples=_samples(db, speaker.id),
        )
        for speaker, count, total_ms in stats
    ]

    candidates = [
        s
        for s in meeting.speakers
        # The local user is never a candidate: their speech is the mic track,
        # which is attributed by construction and never diarized. Offering them
        # would invite a mapping that cannot be right.
        if s.source != SpeakerSource.DIARIZATION and not s.is_local_user and not s.is_excluded
    ]
    candidates.sort(key=lambda s: s.display_name.lower())

    return SpeakerMappingOut(clusters=clusters, candidates=candidates)


def _cluster_stats(db: Session, meeting_id: uuid.UUID) -> list[tuple[Speaker, int, int]]:
    """Unnamed clusters with their segment count and speaking time.

    Ordered by how long the voice spoke, descending. The person who talked for
    twenty minutes is both the easiest to identify and the most costly to leave
    unnamed, so they are the one to ask about first.
    """
    rows = db.execute(
        select(
            Speaker,
            func.count(Segment.id),
            func.coalesce(func.sum(Segment.end_ms - Segment.start_ms), 0),
        )
        .outerjoin(Segment, Segment.speaker_id == Speaker.id)
        .where(
            Speaker.meeting_id == meeting_id,
            Speaker.source == SpeakerSource.DIARIZATION,
        )
        .group_by(Speaker.id)
        .order_by(func.coalesce(func.sum(Segment.end_ms - Segment.start_ms), 0).desc())
    ).all()

    return [(speaker, int(count), int(total)) for speaker, count, total in rows]


def _samples(db: Session, speaker_id: uuid.UUID) -> list[str]:
    """A few representative lines from one cluster.

    The longest lines, not the first. A meeting opens with "hi", "can you hear
    me", "one sec" — the least identifying speech anyone produces all call. The
    longest utterances carry the names, the topics, and the commitments that let
    someone say "that's Priya" without hesitating.
    """
    rows = db.execute(
        select(Segment.text, Segment.text_en)
        .where(Segment.speaker_id == speaker_id)
        .order_by((Segment.end_ms - Segment.start_ms).desc())
        .limit(SAMPLE_COUNT)
    ).all()

    samples = []
    for text, text_en in rows:
        # The original, not the translation: the user is matching a voice they
        # heard, and they heard it in the language it was spoken in.
        line = (text or "").strip() or (text_en or "").strip()
        if not line:
            continue
        if len(line) > SAMPLE_MAX_CHARS:
            line = line[:SAMPLE_MAX_CHARS].rstrip() + "…"
        samples.append(line)

    return samples
