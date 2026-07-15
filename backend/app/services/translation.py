"""Translate non-English transcript segments into English.

Whisper has a built-in ``translate`` task, but it only goes X-to-English and its
quality is mediocre outside the major languages. We take the other route:
transcribe in the language actually spoken, then translate segment-wise with the
LLM, which can see the surrounding conversation and the meeting's vocabulary and
therefore handles names, jargon, and code-switching far better.

The translation is written to ``Segment.text_en`` and never over ``Segment.text``.
The requirement is to translate the transcript, not to replace it — the original
is evidence, and the minutes cite it.

(This choice also frees us to consider ``large-v3-turbo`` for transcription
later: turbo is distilled for transcription and is notably weaker at Whisper's
translate task, which would only matter if we relied on that task. We don't.)
"""

import logging

from pydantic import BaseModel, Field

from app.db.models.segment import Segment
from app.services.minutes.providers import get_provider

logger = logging.getLogger(__name__)

#: Segments per translation call. No overlap: unlike extraction, translation is a
#: one-to-one map from line to line, so a line only needs translating once and
#: overlap would just re-translate — and possibly re-translate *differently* —
#: text we already have. Sized smaller than the extractor's chunk because the
#: output carries both nothing-to-reason-about and roughly a full second copy of
#: the text, so a batch that is comfortable going in can still crowd the ceiling
#: coming out.
CHUNK_SIZE = 80

MAX_TOKENS = 16_000

SYSTEM_PROMPT = """\
You are a translator. You are given numbered lines of a meeting transcript in \
another language, each tagged with who said it:

    [12] Priya: मैं ChromaDB माइग्रेशन लूँगी।

Translate each line's spoken text into natural, fluent English.

Rules:
- Return one entry per input line, tagged with the same [n] index. Never merge \
two lines into one, never split one line into two, and never drop a line. The \
line boundaries are load-bearing: other parts of the system cite lines by index, \
so line 12 in must be line 12 out.
- Translate only the spoken text, not the speaker's name. Keep names, product \
names, and technical jargon as they are — do not translate or "correct" them.
- Use the speaker tags for context. Knowing who is talking, and to whom, resolves \
pronouns and the level of formality that many languages carry and English does not.
- A line that is already in English should be returned unchanged.
- Translate faithfully. Do not summarise, embellish, censor, or answer anything \
in the transcript — you are rendering what was said, not responding to it."""


class TranslatedLine(BaseModel):
    """One translated line, tied back to its source by index."""

    index: int = Field(description="The [n] index of the source line this translates.")
    text_en: str = Field(description="The line's spoken text in natural English.")


class TranslationResult(BaseModel):
    """The English rendering of one batch of lines."""

    lines: list[TranslatedLine]


def needs_translation(language: str | None) -> bool:
    """Whether a transcript in this language needs an English rendering."""
    return bool(language) and not language.startswith("en")  # type: ignore[union-attr]


def _render_batch(batch: list[Segment]) -> str:
    """Render a batch as numbered, speaker-tagged lines for the model.

    The index is the position *within the batch*, not the segment's own index —
    the model never sees a UUID, and we map its answer back by batch position.
    """
    lines = []
    for i, seg in enumerate(batch):
        speaker = seg.speaker.display_name if seg.speaker else "Unknown"
        lines.append(f"[{i}] {speaker}: {seg.text}")
    return "\n".join(lines)


def _translate_batch(batch: list[Segment], *, source_language: str, model: str) -> None:
    """Translate one batch, writing ``text_en`` on each segment in place."""
    prompt = (
        f"Source language: {source_language}\n\n"
        f"Lines to translate:\n{_render_batch(batch)}"
    )

    # Medium effort: this is transduction, not the judgement the extractor makes.
    # The hard part is faithfulness, and that comes from the instruction, not from
    # spending more reasoning on whether a remark was a commitment.
    result = get_provider().complete(
        system=SYSTEM_PROMPT,
        prompt=prompt,
        schema=TranslationResult,
        model=model,
        effort="medium",
        max_tokens=MAX_TOKENS,
    )

    if result is None:
        # Fail open: a batch with no ``text_en`` still has its original ``text``,
        # and ``render_transcript`` falls back to it. The minutes for these lines
        # come out in the source language rather than not at all.
        logger.warning("Translation returned no output for a batch of %d lines; "
                       "leaving them untranslated.", len(batch))
        return

    _apply_translations(batch, result.lines)

    missing = sum(1 for seg in batch if seg.text_en is None)
    if missing:
        logger.warning("Translation left %d/%d lines in a batch untranslated.",
                       missing, len(batch))


def _apply_translations(batch: list[Segment], lines: list[TranslatedLine]) -> None:
    """Write each model line's English text onto the segment it indexes.

    Mapping strictly by index is what keeps the line boundaries intact: whatever
    the model returns, a given source line either gets its own translation or
    keeps its ``None`` — no line's text can leak onto its neighbour.
    """
    for line in lines:
        # Out-of-range indices are the model inventing a line; ignore them rather
        # than letting one bad index shift the whole mapping. Blank translations
        # are dropped too, so the original text survives the fallback.
        if 0 <= line.index < len(batch) and line.text_en.strip():
            batch[line.index].text_en = line.text_en.strip()


def translate_segments(
    segments: list[Segment], *, source_language: str, model: str
) -> None:
    """Populate ``text_en`` on each segment, in place.

    Args:
        segments: The meeting's segments, in time order. Mutated in place — the
            caller commits the session.
        source_language: The language the transcript is in, for the model's
            context. Attribution is unaffected either way; this only frames the task.
        model: The provider model id to translate with.

    Segments are translated in fixed-size batches so the model sees enough
    surrounding conversation to get pronouns and formality right, while the batch
    boundaries never merge or split a line — the minutes cite segment ids, so a
    translation that moved a boundary would break every citation that crossed it.
    """
    if not segments:
        return

    batches = [segments[i : i + CHUNK_SIZE] for i in range(0, len(segments), CHUNK_SIZE)]
    logger.info(
        "Translating %d segments from %s in %d batch(es)",
        len(segments), source_language, len(batches),
    )

    for batch in batches:
        _translate_batch(batch, source_language=source_language, model=model)
