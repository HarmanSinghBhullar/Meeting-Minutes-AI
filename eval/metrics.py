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

Everything here is pure: stdlib only, no ``app`` import, no I/O. That is a
constraint worth defending — ``app.workers.pipeline`` pulls in torch and a Whisper
backend at module scope, and a scorer that needs a GPU to compare two strings is a
scorer nobody runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Tokens are lowercased and stripped of punctuation, but keep internal
#: apostrophes and hyphens: "I'll" is one word, and splitting it invents an error
#: the transcript did not make. Curly quotes fold first — Whisper emits them and a
#: hand-typed gold file will not, and that difference is not a transcription error.
_NOT_TOKEN = re.compile(r"[^\w'\-]+")

#: An unmapped diarization cluster, exactly as ``run_transcribe`` writes it.
#: Matching this is how ``named_speaker_rate`` tells "a name" from "a voice we
#: could not name".
_CLUSTER_LABEL = re.compile(r"SPEAKER_\d+", re.IGNORECASE)

# Backtrace codes for the alignment DP. One byte per cell, which is the whole
# reason the table fits in memory (see `align_tokens`).
_DIAG = 1  # match or substitution: consume one reference token and one hypothesis
_UP = 2  # deletion: a reference token the hypothesis never produced
_LEFT = 3  # insertion: a token the hypothesis invented


def tokenize(text: str) -> list[str]:
    """Split text into comparable word tokens.

    Normalisation is a judgement about what counts as an error, not a detail.
    Case and punctuation are dropped because "Priya." and "priya" are the same word
    correctly heard, and counting that as an error would bury the errors that
    matter under noise nobody can act on.
    """
    folded = text.lower().replace("’", "'").replace("‘", "'")
    return [token for token in _NOT_TOKEN.sub(" ", folded).split() if token]


def strip_inflection(token: str) -> str:
    """Reduce a token to the form we care about matching.

    Only for ``keyword_recall``, never for WER. The question there is whether the
    name survived transcription at all — "Priya's" proves it did — so a possessive
    or a plural must not read as a miss.

    Deliberately crude: strip a possessive, then a trailing plural ``s`` on tokens
    long enough that it is unlikely to be part of the word. A real stemmer would
    trade these false negatives for false positives, and a false positive here
    inflates the headline number, which is the one failure this metric must not
    have.
    """
    if token.endswith("'s") or token.endswith("s'"):
        token = token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        token = token[:-1]
    return token


def align_tokens(
    reference: list[str], hypothesis: list[str]
) -> list[tuple[int | None, int | None]]:
    """Levenshtein-align two token sequences, returning the edit path.

    Each pair is ``(reference_index, hypothesis_index)``: both set for a match or a
    substitution, ``(i, None)`` for a deletion, ``(None, j)`` for an insertion.

    This is the shared spine of the package. WER counts the path; attribution
    accuracy walks it, comparing speaker labels wherever a reference word and a
    hypothesis word landed on each other. Deriving both from one alignment is what
    keeps the two metrics talking about the same words.

    Cost is O(len(reference) x len(hypothesis)) in time, one byte per cell in
    memory: an hour of talk is ~9k words each way, so ~81MB and ~a minute. Fine for
    a harness that runs over five meetings when a model changes; not fine if this
    ever moves somewhere hot. The escape hatch, if that day comes, is a banded DP —
    gold and hypothesis agree far more than they differ, so the path hugs the
    diagonal and the corners are never read.
    """
    n, m = len(reference), len(hypothesis)
    width = m + 1
    back = bytearray(width * (n + 1))

    # Row 0: everything the hypothesis says before the reference starts is inserted.
    for j in range(1, width):
        back[j] = _LEFT

    previous = list(range(width))
    for i in range(1, n + 1):
        current = [i] + [0] * m
        row = i * width
        back[row] = _UP  # column 0: the reference is all deletions
        ref_token = reference[i - 1]
        for j in range(1, width):
            cost = 0 if ref_token == hypothesis[j - 1] else 1
            best = previous[j - 1] + cost
            code = _DIAG
            deletion = previous[j] + 1
            if deletion < best:
                best, code = deletion, _UP
            insertion = current[j - 1] + 1
            if insertion < best:
                best, code = insertion, _LEFT
            current[j] = best
            back[row + j] = code
        previous = current

    pairs: list[tuple[int | None, int | None]] = []
    i, j = n, m
    while i > 0 or j > 0:
        code = back[i * width + j]
        if code == _DIAG:
            i, j = i - 1, j - 1
            pairs.append((i, j))
        elif code == _UP:
            i -= 1
            pairs.append((i, None))
        else:
            j -= 1
            pairs.append((None, j))
    pairs.reverse()
    return pairs


@dataclass(slots=True)
class TranscriptionMetrics:
    """Accuracy of the transcript itself."""

    wer: float
    #: Recall over a hand-listed set of names, products, and acronyms for the
    #: meeting. The headline number: these are the words the minutes are made of.
    keyword_recall: float
    #: Reference length, so a WER can be read next to the size of the thing it
    #: averages over. A twelve-word meeting scoring 0.0 is not evidence of anything.
    reference_words: int = 0
    #: Terms in ``keywords.txt``. Zero means keyword recall is vacuously 1.0 and
    #: must be read as "not measured" rather than "perfect".
    keywords: int = 0


@dataclass(slots=True)
class AttributionMetrics:
    """Accuracy of who-said-what."""

    #: Fraction of transcript words assigned to the correct speaker.
    word_level_accuracy: float
    #: How often a real name was available at all, rather than SPEAKER_01.
    named_speaker_rate: float
    #: Words the alignment matched, and so the denominator of
    #: ``word_level_accuracy``. Attribution is scored only on words we actually
    #: heard: a word the transcript never produced has no speaker to get wrong, and
    #: folding that in would make a WER regression read as an attribution
    #: regression — two different bugs, two different fixes.
    scored_words: int = 0
    #: Reference words in total. ``scored_words`` far below this means the accuracy
    #: above is computed over a thin slice and is less trustworthy than it looks.
    reference_words: int = 0
    #: Distinct voices the pipeline separated, against the distinct people in gold.
    #: ``word_level_accuracy`` maps clusters onto gold speakers many-to-one (see
    #: ``scoring.map_labels``), so splitting one person across two clusters costs it
    #: nothing — the human merges them and the transcript comes out right. It is not
    #: free, though: it is a question someone has to answer. These two numbers are
    #: where that cost is visible.
    hypothesis_speakers: int = 0
    reference_speakers: int = 0


@dataclass(slots=True)
class MinutesMetrics:
    """Accuracy of the minutes against hand-written gold minutes.

    Not yet computed — see ``judge.py``. The dataclass stays because it is the
    specification, and because the comment on ``grounding_rejection_rate`` is the
    most useful line in this file.
    """

    action_item_precision: float
    action_item_recall: float
    decision_precision: float
    decision_recall: float
    #: Items the grounding pass rejected. If this is zero, the grounding pass is
    #: not doing anything and should be distrusted rather than celebrated.
    grounding_rejection_rate: float


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Standard WER: edit distance over words, divided by reference length.

    Not a percentage, and it does not cap at 1.0 — a hypothesis that hallucinates a
    paragraph into silence scores above 1.0, which is the correct and useful answer.

    An empty reference scores 0.0 against an empty hypothesis and 1.0 against
    anything else. The alternative is dividing by zero, and there is no third option
    that means anything.
    """
    ref = tokenize(reference)
    hyp = tokenize(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0

    errors = sum(
        1 for r, h in align_tokens(ref, hyp) if r is None or h is None or ref[r] != hyp[h]
    )
    return errors / len(ref)


def keyword_recall(reference_keywords: list[str], hypothesis: str) -> float:
    """Fraction of the meeting's key terms that survived transcription.

    Case-insensitive, and tolerant of inflection — we care whether the name made it
    through, not whether it was possessive.

    Multi-word terms must appear contiguously: "Kuberya dashboard" is not found by
    seeing "Kuberya" in one sentence and "dashboard" in another, because that is not
    the term, and a metric that accepted it would drift upward as the meeting got
    longer.

    With no keywords, returns 1.0. Read that as "not measured": the caller knows the
    list was empty, and ``TranscriptionMetrics.keywords`` carries the count so a
    report can say so out loud.
    """
    if not reference_keywords:
        return 1.0

    haystack = [strip_inflection(token) for token in tokenize(hypothesis)]
    found = 0
    for term in reference_keywords:
        needle = [strip_inflection(token) for token in tokenize(term)]
        if not needle:
            continue
        span = len(needle)
        if any(haystack[i : i + span] == needle for i in range(len(haystack) - span + 1)):
            found += 1

    return found / len(reference_keywords)


def is_cluster_label(name: str | None) -> bool:
    """Is this the pipeline saying "a voice", rather than naming a person?

    ``None`` (UNKNOWN) and ``SPEAKER_07`` (an unmapped cluster) are both the system
    declining to name someone. They are very different *products* — one is an honest
    gap, the other is a question waiting for a human — but for ``named_speaker_rate``
    they are the same: no name reached the minutes.
    """
    return name is None or bool(_CLUSTER_LABEL.fullmatch(name.strip()))
