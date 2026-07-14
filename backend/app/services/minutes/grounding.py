"""Grounding: verify each minutes item against the segments it cites.

This is the pass that separates minutes people trust from minutes people quietly
stop reading.

The worst thing this product can do is fabricate a commitment and attach a real
person's name to it. Nobody catches it, someone acts on it, and the tool is
finished. A fluent summariser will do exactly this when the transcript is
ambiguous — it resolves the ambiguity in favour of a tidy-sounding action item,
because a tidy-sounding action item is what it was asked for.

So every extracted item is re-read **in isolation**, against only the lines it
claims to come from, by a call that has not seen the rest of the meeting and has
no investment in the item being true.

Isolating the verifier is the whole point, and it is worth being explicit about
why: a verifier that can see the full transcript will cheerfully justify a claim
from context the *citation* does not contain. It would confirm "Priya owns the
migration" because Priya volunteered forty lines later — which makes the claim
true but the citation wrong, and a wrong citation is a broken promise to the
reader who clicks it. We are checking the citation, not the claim.
"""

import logging
from datetime import date

from app.core.config import settings
from app.db.models.segment import Segment
from app.services.minutes.extractor import ExtractedItem, format_meeting_date
from app.services.minutes.providers import get_provider
from app.services.minutes.schemas import GroundingVerdict

logger = logging.getLogger(__name__)

MAX_TOKENS = 2_000

SYSTEM_PROMPT = """\
You are checking whether a claim is supported by the exact transcript lines it
cites. You will not be shown the rest of the meeting, and you must not assume
anything about it.

Answer one question: do these specific lines state this?

Reject the claim if:
- The lines are merely *consistent* with it, but do not say it.
- It is presented as a decision, but the lines only show it being discussed.
- It names an owner who did not accept the work in these lines.
- It is a reasonable inference from the lines rather than something they say.

Due dates need care. People say "by Friday", not "2026-07-17", so a claimed due
date is given as a calendar date and the lines will usually hold a relative one.
Accept the date when it is the correct resolution of what the lines actually say,
counted forward from the meeting date given below. Reject it when the lines state
no deadline at all, or when the date does not match the one they state.

Default to rejecting when you are unsure. A rejected item costs the reader one
manual note. An accepted false one gets acted on by a real person."""


def verify(
    item: ExtractedItem,
    cited: list[Segment],
    *,
    meeting_date: date | None = None,
    model: str | None = None,
) -> GroundingVerdict:
    """Check whether an item's cited segments actually support it.

    Args:
        item: The extracted minutes item.
        cited: **Only** the segments the item cites. Passing the full transcript
            here would defeat the entire purpose — see the module docstring.
        meeting_date: The same anchor the extractor used. The verifier needs it
            for one specific reason: the extractor resolves "by Friday" into a
            calendar date, so without the anchor the verifier would compare
            ``2026-07-17`` against a line that says "Friday", find no match, and
            reject every correctly-dated action item in the meeting. The one thing
            it must *not* do is see anything else about the meeting.
        model: The grounding model. A cheaper one than the extractor is right:
            this is a narrow judgement, and it runs once per item.

    Returns:
        The verdict, which is persisted onto the item and shown in the UI.
    """
    if not cited:
        # An item with no citations cannot be checked, so it cannot be trusted.
        return GroundingVerdict(
            is_grounded=False,
            note="No transcript lines were cited for this item.",
        )

    model = model or settings.grounding_model

    lines = "\n".join(
        f"{seg.speaker.display_name if seg.speaker else 'Unknown'}: {seg.text_en or seg.text}"
        for seg in cited
    )

    owner = f"\nClaimed owner: {item.owner_name}" if item.owner_name else ""
    due = f"\nClaimed due date: {item.due_date}" if item.due_date else ""

    prompt = (
        f"{format_meeting_date(meeting_date)}\n\n"
        f"Cited transcript lines:\n{lines}\n\n"
        f"Claim ({item.type.value}): {item.text}{owner}{due}"
    )

    # Deliberately not "high". This is a narrow, well-posed question, and the
    # verifier should stay cheap enough that nobody is ever tempted to switch it
    # off to save money — a grounding pass that gets disabled is no pass at all.
    verdict = get_provider().complete(
        system=SYSTEM_PROMPT,
        prompt=prompt,
        schema=GroundingVerdict,
        model=model,
        effort="medium",
        max_tokens=MAX_TOKENS,
    )

    if verdict is None:
        # A verifier that fails to answer has not approved anything. Failing
        # closed is the only safe direction here.
        return GroundingVerdict(
            is_grounded=False,
            note="The grounding check did not return a verdict.",
        )

    if not verdict.is_grounded:
        logger.info("Grounding rejected: %s — %s", item.text[:60], verdict.note)

    return verdict
