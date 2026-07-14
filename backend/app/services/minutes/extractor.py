"""Extract structured minutes from an attributed transcript.

Design rules, in priority order:

1. **Every item cites its segments.** The model is given numbered lines and must
   return the numbers each item came from. The citation is a required field of
   the output schema, so an item without one cannot be expressed at all.

2. **Structured, not prose.** Decisions, action items, open questions, risks,
   topics. A paragraph summary is pleasant and useless; a list of action items
   with owners is what people act on.

3. **Owners must be real.** An action item's owner has to be a participant who
   was actually in the meeting. We tell the model the roster and then *check the
   answer against it*, dropping owners it invented. Trust but verify: the check
   is cheap, and it kills a whole class of hallucination on its own.

4. **Map-reduce over long meetings.** An hour of transcript will not fit
   comfortably in one call, and stuffing it in degrades recall of items in the
   middle. Chunk with overlap, extract per chunk, then deduplicate.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date

from app.core.config import settings
from app.db.models.enums import MinutesItemType
from app.db.models.segment import Segment
from app.services.minutes.providers import get_provider
from app.services.minutes.schemas import ExtractionResult, SummaryResult

logger = logging.getLogger(__name__)

#: Segments per extraction chunk, with overlap. Sized to stay well inside the
#: model's comfortable range rather than at its limit — recall of items in the
#: middle of a long context degrades before the context is full.
CHUNK_SIZE = 120
CHUNK_OVERLAP = 20

#: The overlap matters more than it looks: an action item is often stated in one
#: sentence and accepted in the next, and a hard boundary between them loses the
#: owner — which is the half that makes the item worth having.

MAX_TOKENS = 16_000

SYSTEM_PROMPT = """\
You extract meeting minutes from a transcript.

The transcript is given as numbered lines, each tagged with who said it:

    [12] Priya: I'll take the ChromaDB migration.

Extract only what the meeting actually decided, committed to, asked, or flagged.

Rules:
- Every item must cite the line numbers it came from. No citation, no item.
- An action item's owner must be one of the listed participants, named exactly as
  listed. If nobody explicitly took the work on, leave the owner null. Never
  assign work to someone who did not accept it.
- People state deadlines relative to the meeting ("by Friday", "end of next
  week"). Resolve those against the meeting date given below, to the next such
  day *after* the meeting, and return an ISO date.
- But only when a deadline was actually stated. "Soon", "shortly", and "when I
  get a chance" are not deadlines. Do not invent one, and do not resolve a
  relative date if no meeting date was given.
- If somebody floated an idea and it was not agreed, that is not a decision. If
  discussion trailed off without resolution, that is an open question, not a
  decision.
- Prefer fewer, well-supported items over broad coverage. A missed item costs the
  reader one manual note. A fabricated one gets acted on."""


@dataclass(slots=True)
class ExtractedItem:
    """One item the model pulled out, with its citations resolved to segments."""

    type: MinutesItemType
    text: str
    owner_name: str | None
    due_date: str | None
    segment_ids: list[uuid.UUID] = field(default_factory=list)


def render_transcript(segments: list[Segment]) -> str:
    """Render segments for the model, one per line, tagged with a citable index.

    The model cites the leading ``[n]``. We map it back to a real segment UUID
    afterwards rather than showing it UUIDs, which it would spend tokens on and
    get wrong.
    """
    lines = []
    for i, seg in enumerate(segments):
        speaker = seg.speaker.display_name if seg.speaker else "Unknown"
        text = seg.text_en or seg.text  # minutes are written in English
        lines.append(f"[{i}] {speaker}: {text}")
    return "\n".join(lines)


def chunk_segments(segments: list[Segment]) -> list[list[Segment]]:
    """Split a long transcript into overlapping chunks."""
    if len(segments) <= CHUNK_SIZE:
        return [segments]

    chunks = []
    step = CHUNK_SIZE - CHUNK_OVERLAP
    for start in range(0, len(segments), step):
        chunk = segments[start : start + CHUNK_SIZE]
        if chunk:
            chunks.append(chunk)
    return chunks


def participant_names(segments: list[Segment]) -> list[str]:
    """Who actually spoke. The only people who may own an action item."""
    names = {seg.speaker.display_name for seg in segments if seg.speaker}
    return sorted(names)


def format_meeting_date(meeting_date: date | None) -> str:
    """Describe the meeting date for the model, weekday included.

    The weekday is the whole point. "By Friday" is unresolvable from
    ``2026-07-14`` alone — the model would have to work out what day that was —
    and asking it to do calendar arithmetic in its head is how you get a deadline
    that is off by a week. Naming the day directly removes the guesswork.
    """
    if meeting_date is None:
        # Said explicitly rather than omitted, so the model knows the absence is
        # a fact about the input and not something it should paper over.
        return "Meeting date: unknown — do not resolve relative deadlines."
    return f"Meeting date: {meeting_date:%A, %d %B %Y} ({meeting_date.isoformat()})"


def extract(
    segments: list[Segment],
    *,
    meeting_date: date | None = None,
    model: str | None = None,
) -> list[ExtractedItem]:
    """Extract minutes items from an attributed transcript.

    Args:
        segments: The attributed transcript, in time order.
        meeting_date: The day the meeting happened. Without it every relative
            deadline in the meeting — and most deadlines are relative — is lost,
            because the model is (correctly) forbidden from guessing.
        model: Provider model id. Defaults to the configured one.
    """
    if not segments:
        return []

    model = model or settings.minutes_model
    roster = participant_names(segments)
    chunks = chunk_segments(segments)

    logger.info("Extracting minutes from %d segments in %d chunk(s)", len(segments), len(chunks))

    items: list[ExtractedItem] = []
    for chunk in chunks:
        items.extend(_extract_chunk(chunk, roster, meeting_date, model=model))

    return _deduplicate(items)


def _extract_chunk(
    chunk: list[Segment],
    roster: list[str],
    meeting_date: date | None,
    *,
    model: str,
) -> list[ExtractedItem]:
    """Run one extraction call over one chunk."""
    prompt = (
        f"{format_meeting_date(meeting_date)}\n"
        f"Participants: {', '.join(roster) if roster else 'unknown'}\n\n"
        f"Transcript:\n{render_transcript(chunk)}"
    )

    # High effort: telling a decision apart from a passing remark, and a
    # commitment apart from a suggestion, is the actual work here.
    result = get_provider().complete(
        system=SYSTEM_PROMPT,
        prompt=prompt,
        schema=ExtractionResult,
        model=model,
        effort="high",
        max_tokens=MAX_TOKENS,
    )

    if result is None:
        logger.warning("Extraction returned no parsed output for a chunk; skipping it.")
        return []

    items: list[ExtractedItem] = []
    for raw in result.items:
        # Resolve the model's line numbers back to real segments. Out-of-range
        # indices are dropped rather than trusted — a citation that points
        # nowhere is worse than no citation, because it looks checkable.
        segment_ids = [chunk[i].id for i in raw.cites if 0 <= i < len(chunk)]
        if not segment_ids:
            logger.warning("Dropping item with no resolvable citation: %s", raw.text[:80])
            continue

        # The model was told the roster; this checks that it listened. An owner
        # who was not in the meeting is the signature of an invented commitment.
        owner = raw.owner if raw.owner in roster else None
        if raw.owner and owner is None:
            logger.warning("Dropping invented owner %r (not a participant)", raw.owner)

        items.append(
            ExtractedItem(
                type=MinutesItemType(raw.type),
                text=raw.text.strip(),
                owner_name=owner,
                due_date=raw.due_date,
                segment_ids=segment_ids,
            )
        )

    return items


def _deduplicate(items: list[ExtractedItem]) -> list[ExtractedItem]:
    """Merge items the overlapping chunks found twice.

    Citations are *unioned* rather than kept from the first occurrence: a claim
    supported in two places is twice as verifiable, and throwing the second
    citation away would discard evidence we already paid for.
    """
    merged: dict[tuple[MinutesItemType, str], ExtractedItem] = {}

    for item in items:
        key = (item.type, " ".join(item.text.lower().split()))
        existing = merged.get(key)

        if existing is None:
            merged[key] = item
            continue

        for segment_id in item.segment_ids:
            if segment_id not in existing.segment_ids:
                existing.segment_ids.append(segment_id)

        # A later chunk may have caught the owner or date the earlier one missed
        # — that is precisely what the overlap is for.
        existing.owner_name = existing.owner_name or item.owner_name
        existing.due_date = existing.due_date or item.due_date

    return list(merged.values())


def summarize(segments: list[Segment], *, model: str | None = None) -> str | None:
    """Write the prose overview that sits above the structured items."""
    if not segments:
        return None

    model = model or settings.minutes_model

    # The summary reads the whole meeting, but only its text — it is the one part
    # of the output that is allowed to generalise, because nobody acts on it.
    result = get_provider().complete(
        system=(
            "Summarise this meeting in one short paragraph: what it was about and "
            "where it landed. Do not list action items — they are captured "
            "separately. Do not invent outcomes that were not reached."
        ),
        prompt=render_transcript(segments),
        schema=SummaryResult,
        model=model,
        effort="medium",
        max_tokens=MAX_TOKENS,
    )

    return result.summary if result else None
