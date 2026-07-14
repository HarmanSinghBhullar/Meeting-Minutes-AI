"""Metrics for the evaluation set.

The point of this directory is simple: without it, "accurate" is a vibe.

Every model swap, every prompt change, every VAD threshold is otherwise a coin
flip you cannot evaluate — and you will spend weeks tuning things that do not
matter while the real error source goes unnoticed. With a gold set, you usually
discover the errors are concentrated somewhere unglamorous (one participant's
bad microphone, crosstalk, a mangled product name) and you can fix the actual
problem.

Three numbers are enough:

* **WER** — did we hear the words. But read it with care: the errors that matter
  are concentrated in rare, informative words (names, products, acronyms), and a
  transcript that is 94% accurate while dropping every proper noun is useless for
  minutes. Hence ``keyword_recall`` below, which is the number to watch.

* **Speaker attribution accuracy** — did we get the right name on the right words.
  This is the error users notice.

* **Action-item precision / recall** — did the minutes say what the meeting said.
  Precision matters more than recall here: a missed action item costs someone one
  manual note, while a fabricated one gets acted on.
"""

from dataclasses import dataclass


@dataclass(slots=True)
class TranscriptionMetrics:
    """Accuracy of the transcript itself."""

    wer: float
    #: Recall over a hand-listed set of names, products, and acronyms for the
    #: meeting. The headline number: these are the words the minutes are made of.
    keyword_recall: float


@dataclass(slots=True)
class AttributionMetrics:
    """Accuracy of who-said-what."""

    #: Fraction of transcript words assigned to the correct speaker.
    word_level_accuracy: float
    #: How often a real name was available at all, rather than SPEAKER_01.
    named_speaker_rate: float


@dataclass(slots=True)
class MinutesMetrics:
    """Accuracy of the minutes against hand-written gold minutes."""

    action_item_precision: float
    action_item_recall: float
    decision_precision: float
    decision_recall: float
    #: Items the grounding pass rejected. If this is zero, the grounding pass is
    #: not doing anything and should be distrusted rather than celebrated.
    grounding_rejection_rate: float


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Standard WER: edit distance over words, divided by reference length.

    TODO: implement (Levenshtein over token lists), or take it from ``jiwer``.
    """
    raise NotImplementedError


def keyword_recall(reference_keywords: list[str], hypothesis: str) -> float:
    """Fraction of the meeting's key terms that survived transcription.

    TODO: implement. Case-insensitive, and tolerant of inflection — we care
    whether the name made it through, not whether it was possessive.
    """
    raise NotImplementedError
