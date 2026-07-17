"""Scoring: a gold transcript, a hypothesis transcript, and the numbers between.

Pure — two ``Transcript`` objects in, metrics out. No I/O, no ``app`` import, no
GPU. That is what makes this half of the harness testable today, before a single
real meeting has been recorded.

## The argument about cluster names

The pipeline does not emit "Priya". It emits ``SPEAKER_00``, and waits for a human
to say who that is (the mapping gate — see ``CLAUDE.md``). So a hypothesis scored
naively against gold gets zero, forever, and the number measures nothing except
that the gate exists.

So scoring maps hypothesis labels onto gold names first: each cluster becomes
whichever gold speaker it most co-occurs with. That is the standard move in
diarization evaluation, and the reason it is honest here rather than charitable is
that **it is what the product actually does**. A cluster is not a guess the system
commits to; it is a question, and ``POST /speakers/{id}/resolve`` is the human
answering it. Scoring after the mapping measures the thing the system is actually
responsible for — *did it separate the voices* — instead of re-measuring the gate.

The mapping is **many-to-one**: two clusters may both map to Priya. That is
deliberate and it mirrors the product exactly, because ``target_speaker_id``
*merges* a cluster into a roster speaker, so a diarizer that split Priya in two
still yields a correct transcript once both are merged. It is not free — it is one
more question for a human — but the cost is human time, not wrong minutes, and
those should not be scored as the same failure. ``AttributionMetrics`` carries
``hypothesis_speakers`` against ``reference_speakers`` so the over-clustering is
visible in the report rather than hidden inside the accuracy.

The trap to know about: this mapping cannot be used to argue the pipeline names
people correctly. It assumes perfect naming, by construction. ``named_speaker_rate``
is the metric that keeps that claim honest, and it is computed on the **raw**
hypothesis, before any of this.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from eval.metrics import (
    AttributionMetrics,
    TranscriptionMetrics,
    align_tokens,
    is_cluster_label,
    keyword_recall,
    tokenize,
    word_error_rate,
)
from eval.transcript import Transcript


@dataclass(slots=True)
class Report:
    """Everything one meeting's evaluation produced."""

    name: str
    transcription: TranscriptionMetrics
    attribution: AttributionMetrics


def labelled_words(transcript: Transcript) -> tuple[list[str], list[str | None]]:
    """Flatten a transcript to a word sequence and a parallel speaker sequence.

    This is where the format's decision to skip word timings pays off. Every
    ``Segment`` has exactly one speaker — upstream, ``align`` splits a Whisper
    segment at every label change specifically to guarantee that — so a word simply
    inherits its segment's speaker. No timing, no interpolation, no join.
    """
    tokens: list[str] = []
    labels: list[str | None] = []
    for segment in transcript.segments:
        for token in tokenize(segment.text):
            tokens.append(token)
            labels.append(segment.speaker)
    return tokens, labels


def map_labels(
    gold_tokens: list[str],
    gold_labels: list[str | None],
    hyp_tokens: list[str],
    hyp_labels: list[str | None],
    pairs: list[tuple[int | None, int | None]],
) -> dict[str | None, str | None]:
    """Map each hypothesis speaker onto the gold speaker it most co-occurs with.

    Many-to-one, counted only over words the two transcripts agree on — a
    substitution is a word we misheard, and letting a misheard word vote on who
    said it would let a transcription error masquerade as an attribution signal.

    Ties break on the gold name, alphabetically. It is arbitrary, but it is
    *stably* arbitrary: an unstable tie-break would make the metric wobble between
    runs on identical input, and a metric you cannot trust to be boring is a metric
    that cannot detect a regression.
    """
    votes: dict[str | None, dict[str | None, int]] = defaultdict(lambda: defaultdict(int))
    for r, h in pairs:
        if r is None or h is None:
            continue
        if gold_tokens[r] != hyp_tokens[h]:
            continue
        votes[hyp_labels[h]][gold_labels[r]] += 1

    return {
        hyp_label: min(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))[0]
        for hyp_label, counts in votes.items()
    }


def score(
    gold: Transcript,
    hypothesis: Transcript,
    *,
    keywords: list[str] | None = None,
    name: str = "meeting",
) -> Report:
    """Score one hypothesis against one gold transcript.

    Both metrics come off a single alignment of the two word sequences, so they are
    always talking about the same words.
    """
    keywords = keywords or []

    gold_tokens, gold_labels = labelled_words(gold)
    hyp_tokens, hyp_labels = labelled_words(hypothesis)

    transcription = TranscriptionMetrics(
        wer=word_error_rate(gold.text, hypothesis.text),
        keyword_recall=keyword_recall(keywords, hypothesis.text),
        reference_words=len(gold_tokens),
        keywords=len(keywords),
    )

    pairs = align_tokens(gold_tokens, hyp_tokens)
    mapping = map_labels(gold_tokens, gold_labels, hyp_tokens, hyp_labels, pairs)

    scored = 0
    correct = 0
    for r, h in pairs:
        if r is None or h is None or gold_tokens[r] != hyp_tokens[h]:
            continue
        scored += 1
        if mapping.get(hyp_labels[h]) == gold_labels[r]:
            correct += 1

    # Computed on the raw hypothesis, deliberately: this is the one number the
    # cluster mapping above must not be allowed to flatter. It answers "did a name
    # reach the minutes", which for a diarized meeting is a question about the
    # human, not the model.
    named = sum(1 for label in hyp_labels if not is_cluster_label(label))

    attribution = AttributionMetrics(
        word_level_accuracy=correct / scored if scored else 0.0,
        named_speaker_rate=named / len(hyp_labels) if hyp_labels else 0.0,
        scored_words=scored,
        reference_words=len(gold_tokens),
        hypothesis_speakers=len(hypothesis.speakers),
        reference_speakers=len(gold.speakers),
    )

    return Report(name=name, transcription=transcription, attribution=attribution)


def render(reports: list[Report]) -> str:
    """Format reports as a table, with the caveats attached rather than implied.

    The warnings below the table are the point. A keyword recall of 1.00 over zero
    keywords and a keyword recall of 1.00 over forty are the same number and
    opposite facts, and a report that cannot tell you which is worse than no report
    — it is a wrong report that looks authoritative.
    """
    if not reports:
        return "No datasets scored."

    header = f"{'meeting':<24} {'WER':>7} {'keyword':>9} {'attrib':>8} {'named':>7} {'voices':>8}"
    lines = [header, "-" * len(header)]

    for report in reports:
        t, a = report.transcription, report.attribution
        voices = f"{a.hypothesis_speakers}/{a.reference_speakers}"
        lines.append(
            f"{report.name[:24]:<24} {t.wer:>7.3f} {t.keyword_recall:>9.3f} "
            f"{a.word_level_accuracy:>8.3f} {a.named_speaker_rate:>7.3f} {voices:>8}"
        )

    if len(reports) > 1:
        count = len(reports)
        lines.append("-" * len(header))
        mean_voices = (
            f"{sum(r.attribution.hypothesis_speakers for r in reports)}/"
            f"{sum(r.attribution.reference_speakers for r in reports)}"
        )
        lines.append(
            f"{'mean':<24} "
            f"{sum(r.transcription.wer for r in reports) / count:>7.3f} "
            f"{sum(r.transcription.keyword_recall for r in reports) / count:>9.3f} "
            f"{sum(r.attribution.word_level_accuracy for r in reports) / count:>8.3f} "
            f"{sum(r.attribution.named_speaker_rate for r in reports) / count:>7.3f} "
            f"{mean_voices:>8}"
        )

    notes: list[str] = []
    for report in reports:
        t, a = report.transcription, report.attribution
        if t.keywords == 0:
            notes.append(
                f"{report.name}: no keywords.txt — keyword recall is not measured, not 1.00."
            )
        if a.scored_words < t.reference_words // 2:
            notes.append(
                f"{report.name}: only {a.scored_words} of {t.reference_words} words matched gold, "
                "so attribution accuracy is computed over a thin slice. Fix WER first."
            )
        if a.hypothesis_speakers > a.reference_speakers:
            notes.append(
                f"{report.name}: {a.hypothesis_speakers} voices found against "
                f"{a.reference_speakers} real people — over-clustering costs accuracy nothing "
                "here, but every extra one is a question a human has to answer."
            )

    if notes:
        lines.append("")
        lines.extend(f"  ! {note}" for note in notes)

    lines.append("")
    lines.append("  WER: lower is better, uncapped. Everything else: higher is better.")
    lines.append("  attrib assumes perfect cluster naming; named is what shipped. Read both.")

    return "\n".join(lines)
