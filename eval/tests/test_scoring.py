"""Tests for scoring a hypothesis against gold.

The cases that matter here are the ones about the cluster mapping, because that is
the part of this harness most able to lie. It exists to stop the mapping gate from
scoring as a permanent zero; it must not become a machine for laundering real
attribution errors into perfect scores. Most of what follows is that boundary.
"""

from eval.scoring import labelled_words, map_labels, render, score
from eval.transcript import Segment, Transcript


def _transcript(*pairs: tuple[str | None, str]) -> Transcript:
    """Build a transcript from (speaker, text) pairs, one per second."""
    return Transcript(
        segments=[
            Segment(start_ms=i * 1000, end_ms=(i + 1) * 1000, speaker=speaker, text=text)
            for i, (speaker, text) in enumerate(pairs)
        ]
    )


class TestLabelledWords:
    def test_words_inherit_their_segment_speaker(self) -> None:
        tokens, labels = labelled_words(_transcript(("Priya", "yes ok"), ("Deepak", "no")))
        assert tokens == ["yes", "ok", "no"]
        assert labels == ["Priya", "Priya", "Deepak"]


class TestClusterMapping:
    def test_clusters_map_to_the_speaker_they_co_occur_with(self) -> None:
        gold = _transcript(("Priya", "i will take the migration"))
        hyp = _transcript(("SPEAKER_00", "i will take the migration"))

        report = score(gold, hyp)

        assert report.attribution.word_level_accuracy == 1.0

    def test_perfect_diarization_scores_perfectly_despite_no_names(self) -> None:
        """The gate is not an accuracy failure. A cluster is a question, not a
        wrong answer, and scoring it as one would measure the gate rather than the
        diarizer."""
        gold = _transcript(("Priya", "hello there"), ("Deepak", "hi priya"))
        hyp = _transcript(("SPEAKER_00", "hello there"), ("SPEAKER_01", "hi priya"))

        report = score(gold, hyp)

        assert report.attribution.word_level_accuracy == 1.0
        assert report.attribution.named_speaker_rate == 0.0

    def test_mapping_is_many_to_one(self) -> None:
        """Two clusters for one person is what `target_speaker_id` merges. The
        human answers twice and the transcript comes out right, so accuracy is
        right too."""
        gold = _transcript(("Priya", "hello there"), ("Priya", "and also this"))
        hyp = _transcript(("SPEAKER_00", "hello there"), ("SPEAKER_01", "and also this"))

        report = score(gold, hyp)

        assert report.attribution.word_level_accuracy == 1.0
        # ...but the cost is visible rather than hidden.
        assert report.attribution.hypothesis_speakers == 2
        assert report.attribution.reference_speakers == 1

    def test_a_real_attribution_error_survives_the_mapping(self) -> None:
        """The guard rail. `SPEAKER_00` maps to Priya on the strength of four words
        and still gets the fifth wrong, and the score must say so — otherwise this
        whole file is a machine for producing 1.0."""
        gold = _transcript(("Priya", "one two three four"), ("Deepak", "five"))
        hyp = _transcript(("SPEAKER_00", "one two three four five"))

        report = score(gold, hyp)

        assert report.attribution.scored_words == 5
        assert report.attribution.word_level_accuracy == 0.8

    def test_ties_break_stably(self) -> None:
        """Arbitrary is fine; unstable is not. A metric that wobbles between runs on
        identical input cannot detect a regression."""
        gold = _transcript(("Anna", "alpha"), ("Bella", "bravo"))
        hyp = _transcript(("SPEAKER_00", "alpha"), ("SPEAKER_00", "bravo"))

        first = map_labels(*_align_args(gold, hyp))
        second = map_labels(*_align_args(gold, hyp))

        assert first == second
        assert first["SPEAKER_00"] == "Anna"  # alphabetical on an even split

    def test_misheard_words_do_not_vote(self) -> None:
        """A substitution is a word we got wrong. Letting it vote on who said it
        would let a transcription error pose as an attribution signal."""
        gold = _transcript(("Priya", "alpha bravo charlie"))
        hyp = _transcript(("SPEAKER_00", "alpha bravo delta"))

        report = score(gold, hyp)

        assert report.attribution.scored_words == 2  # "charlie"/"delta" excluded
        assert report.attribution.word_level_accuracy == 1.0


class TestNamedSpeakerRate:
    def test_measured_on_raw_output_not_after_mapping(self) -> None:
        """The one number the cluster mapping must never be allowed to flatter."""
        gold = _transcript(("Harman", "one two"), ("Priya", "three four"))
        hyp = _transcript(("Harman", "one two"), ("SPEAKER_00", "three four"))

        report = score(gold, hyp)

        assert report.attribution.word_level_accuracy == 1.0
        assert report.attribution.named_speaker_rate == 0.5

    def test_unknown_counts_as_unnamed(self) -> None:
        gold = _transcript(("Priya", "one two"))
        hyp = _transcript((None, "one two"))

        assert score(gold, hyp).attribution.named_speaker_rate == 0.0


class TestTranscriptionScoring:
    def test_wer_ignores_who_spoke(self) -> None:
        gold = _transcript(("Priya", "the cat sat"))
        hyp = _transcript(("Deepak", "the cat sat"))

        assert score(gold, hyp).transcription.wer == 0.0

    def test_keywords_are_scored_against_the_hypothesis(self) -> None:
        gold = _transcript(("Priya", "the ChromaDB migration"))
        hyp = _transcript(("Priya", "the chroma DB migration"))

        report = score(gold, hyp, keywords=["ChromaDB"])

        assert report.transcription.keyword_recall == 0.0
        assert report.transcription.keywords == 1

    def test_counts_are_reported(self) -> None:
        report = score(_transcript(("Priya", "one two three")), _transcript(("Priya", "one two")))

        assert report.transcription.reference_words == 3
        assert report.attribution.scored_words == 2


class TestEmptyHypothesis:
    def test_scores_without_dividing_by_zero(self) -> None:
        """A pipeline that produced nothing is a real outcome, and the harness has
        to survive reporting it — this is what a crashed worker looks like."""
        report = score(_transcript(("Priya", "hello there")), Transcript())

        assert report.transcription.wer == 1.0
        assert report.attribution.word_level_accuracy == 0.0
        assert report.attribution.named_speaker_rate == 0.0
        assert report.attribution.scored_words == 0


class TestRender:
    def test_flags_missing_keywords_as_unmeasured(self) -> None:
        report = score(_transcript(("Priya", "hello")), _transcript(("Priya", "hello")))
        out = render([report])

        assert "not measured" in out

    def test_flags_over_clustering(self) -> None:
        gold = _transcript(("Priya", "hello there"), ("Priya", "and also this"))
        hyp = _transcript(("SPEAKER_00", "hello there"), ("SPEAKER_01", "and also this"))
        out = render([score(gold, hyp, keywords=["hello"])])

        assert "over-clustering" in out

    def test_empty(self) -> None:
        assert render([]) == "No datasets scored."


def _align_args(gold: Transcript, hyp: Transcript):  # type: ignore[no-untyped-def]
    """Unpack what `map_labels` wants, so a tie test can call it directly."""
    from eval.metrics import align_tokens

    gold_tokens, gold_labels = labelled_words(gold)
    hyp_tokens, hyp_labels = labelled_words(hyp)
    return gold_tokens, gold_labels, hyp_tokens, hyp_labels, align_tokens(gold_tokens, hyp_tokens)
