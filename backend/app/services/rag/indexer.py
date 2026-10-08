"""Build Chroma's derived, citation-preserving meeting transcript index."""

import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db.models.meeting import Meeting
from app.db.models.segment import Segment
from app.services.rag.store import get_collection

WINDOW_SIZE = 12
WINDOW_OVERLAP = 4


def index_meeting(db: Session, meeting_id: uuid.UUID) -> int:
    """Replace one meeting's vector windows from its authoritative segments."""
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise ValueError(f"Meeting {meeting_id} does not exist.")
    segments = list(
        db.execute(
            select(Segment)
            .where(Segment.meeting_id == meeting_id)
            .order_by(Segment.start_ms, Segment.index)
            .options(joinedload(Segment.speaker))
        ).scalars()
    )
    collection = get_collection()
    collection.delete(where={"meeting_id": str(meeting_id)})
    if not segments:
        return 0

    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict[str, str | int]] = []
    for number, start in enumerate(range(0, len(segments), WINDOW_SIZE - WINDOW_OVERLAP)):
        window = segments[start : start + WINDOW_SIZE]
        names: list[str] = []
        lines: list[str] = []
        for segment in window:
            speaker = segment.speaker.display_name if segment.speaker else "Unknown"
            if speaker not in names:
                names.append(speaker)
            lines.append(f"[{segment.start_ms}ms] {speaker}: {segment.text_en or segment.text}")
        ids.append(f"{meeting.id}:{number}")
        documents.append("\n".join(lines))
        metadatas.append(
            {
                "meeting_id": str(meeting.id),
                "meeting_title": meeting.title or "Untitled meeting",
                "meeting_date": meeting.started_at.isoformat() if meeting.started_at else "",
                "speakers": ", ".join(names),
                "start_ms": window[0].start_ms,
                "segment_ids": json.dumps([str(segment.id) for segment in window]),
            }
        )
    collection.add(ids=ids, documents=documents, metadatas=metadatas)
    return len(ids)


def reindex_all(db: Session) -> int:
    """Rebuild every vector window from PostgreSQL, the source of truth."""
    collection = get_collection()
    existing = collection.get(include=[])
    if existing["ids"]:
        collection.delete(ids=existing["ids"])
    meeting_ids = db.execute(select(Meeting.id)).scalars()
    return sum(index_meeting(db, meeting_id) for meeting_id in meeting_ids)
