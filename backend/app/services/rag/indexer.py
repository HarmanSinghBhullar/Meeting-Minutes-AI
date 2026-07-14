"""RAG indexing — the phase-2 add-on.

Deliberately last. Q&A over past meetings is only as good as the transcripts
underneath it, so there is nothing to gain from building it before attribution
and minutes are accurate, and quite a lot to lose.

Two design notes that decide whether this feature is good or useless:

**Chunk by window, not by segment.** A single Whisper segment is a fragment —
"yeah, exactly" retrieves nothing and answers nothing. A whole meeting is too
coarse to cite. The right unit is a rolling window of consecutive segments,
overlapping, carrying ``meeting_id``, ``speaker``, and ``start_ms`` as metadata
so an answer can say "Priya, 14 May, 23:41" and link straight back to the audio.

**Postgres is the source of truth; Chroma is a derived index.** It will drift,
it will get corrupted, and a schema change will invalidate it. So the reindex
path is not a maintenance script written later under duress — it is the primary
entry point, and building the index is just reindexing from empty.
"""

import uuid

from sqlalchemy.orm import Session

#: Segments per retrieval window, with overlap. Tuned so a window holds a
#: coherent exchange rather than a single utterance.
WINDOW_SIZE = 12
WINDOW_OVERLAP = 4


def index_meeting(db: Session, meeting_id: uuid.UUID) -> int:
    """Index one meeting's segments into the vector store.

    Returns the number of chunks written.

    TODO(phase-2): implement.
      - Build overlapping windows over the meeting's segments in time order.
      - Embed the English text (``text_en or text``) so cross-language meetings
        are searchable from an English question.
      - Store metadata: meeting_id, meeting title, date, speakers in the window,
        start_ms, and the segment ids — the last of these is what turns a
        retrieved chunk into a citation.
      - Make it idempotent: delete this meeting's existing chunks first, so a
        re-run repairs rather than duplicates.
    """
    raise NotImplementedError("RAG indexing: phase 2, after minutes are accurate.")


def reindex_all(db: Session) -> int:
    """Rebuild the entire vector store from Postgres.

    The index is derived data. This is the function that proves it.
    """
    raise NotImplementedError("RAG indexing: phase 2, after minutes are accurate.")
