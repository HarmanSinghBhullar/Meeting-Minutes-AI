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

from app.db.models.segment import Segment


def needs_translation(language: str | None) -> bool:
    """Whether a transcript in this language needs an English rendering."""
    return bool(language) and not language.startswith("en")  # type: ignore[union-attr]


def translate_segments(segments: list[Segment], *, source_language: str, model: str) -> None:
    """Populate ``text_en`` on each segment, in place.

    TODO(phase-2): implement via ``services.minutes.providers.get_provider()`` —
    translation is another structured completion, so it should go through the same
    provider interface rather than reaching for an SDK directly.
      - Batch segments (translating one line at a time loses the context that
        makes translation good, and costs a round trip per line).
      - Pass the speaker labels through: knowing who is talking disambiguates
        pronouns and formality in most languages.
      - Keep the segment boundaries exactly. The minutes cite segment ids, so a
        translation that merges or splits lines breaks every citation.
    """
    raise NotImplementedError("Translation: implement after the transcript slice lands.")
