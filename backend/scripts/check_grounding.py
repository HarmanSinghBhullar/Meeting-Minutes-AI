"""Adversarial check: does the grounding pass actually reject anything?

A verifier that approves everything is indistinguishable from no verifier at all,
and it is worse than none — it gives the minutes a credibility they have not
earned. So we feed it claims we *know* the cited line does not support and
confirm it says so.

The fabrications below are not random. Each is a real failure mode of a fluent
summariser working from an ambiguous transcript:

  * wrong owner         — someone was *asked*, someone else accepted
  * fabricated due date — a deadline nobody said, but that sounds plausible
  * inflated to decision — discussion reported as a resolution
  * unsupported extra   — a commitment bolted onto a real one

The "resolved due date" case guards the opposite failure. The extractor turns
"by Friday" into a calendar date, so the verifier is comparing an ISO date against
a line that never contains one. It must accept the *correct* resolution — a
verifier that rejects every properly-dated action item is just as broken as one
that accepts fabrications, and much more annoying.

Usage:
    python scripts/check_grounding.py <meeting_id>
"""

import sys
import uuid
from datetime import timedelta

from sqlalchemy import select

from app.db.models.enums import MinutesItemType
from app.db.models.meeting import Meeting
from app.db.models.segment import Segment
from app.db.session import SessionLocal
from app.services.minutes.extractor import ExtractedItem
from app.services.minutes.grounding import verify
from app.workers.pipeline import _meeting_date


def main(meeting_id: uuid.UUID) -> int:
    db = SessionLocal()
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        sys.exit(f"Meeting {meeting_id} not found.")

    segments = list(
        db.execute(
            select(Segment).where(Segment.meeting_id == meeting_id).order_by(Segment.start_ms)
        ).scalars()
    )

    cited = [s for s in segments if s.speaker and s.speaker.display_name == "Harman"]
    if not cited:
        sys.exit("No attributed segment to cite; run the transcript pipeline first.")

    meeting_date = _meeting_date(meeting)
    if meeting_date is None:
        sys.exit("Meeting has no date; relative deadlines cannot be checked.")

    # The Friday after the meeting — what "by Friday" actually resolves to.
    days_ahead = (4 - meeting_date.weekday()) % 7 or 7
    friday = meeting_date + timedelta(days=days_ahead)

    print(f"Meeting date -> {meeting_date:%A, %d %B %Y}")
    print(f"Cited line   -> {cited[0].speaker.display_name}: {cited[0].text}\n")

    cases: list[tuple[str, str, str | None, str | None, bool]] = [
        # label, claim, owner, due, expected_grounded
        ("true claim", "Harman will take the ChromaDB migration.", "Harman", None, True),
        ("resolved due date", "Harman will take the migration.", "Harman", friday.isoformat(), True),
        ("wrong owner", "Priya will take the ChromaDB migration.", "Priya", None, False),
        ("wrong resolved date", "Harman will take the migration.", "Harman", "2026-07-20", False),
        ("inflated to decision", "The team decided to cancel the migration.", None, None, False),
        ("unsupported extra", "Harman will also rewrite the API layer.", "Harman", None, False),
    ]

    failures = 0

    for label, text, owner, due, expected in cases:
        item = ExtractedItem(
            type=MinutesItemType.ACTION_ITEM,
            text=text,
            owner_name=owner,
            due_date=due,
            segment_ids=[s.id for s in cited],
        )
        verdict = verify(item, cited, meeting_date=meeting_date)

        ok = verdict.is_grounded == expected
        if not ok:
            failures += 1

        status = "GROUNDED" if verdict.is_grounded else "REJECTED"
        mark = "ok  " if ok else "FAIL"
        print(f"[{mark}] {label:22} -> {status}")
        if verdict.note:
            print(f"                              {verdict.note}")

    print()
    if failures:
        print(f"{failures} case(s) behaved wrongly — the grounding pass is not doing its job.")
    else:
        print("Grounding behaved correctly on every case.")

    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/check_grounding.py <meeting_id>")
    sys.exit(main(uuid.UUID(sys.argv[1])))
