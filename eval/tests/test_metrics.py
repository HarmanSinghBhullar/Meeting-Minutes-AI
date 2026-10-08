"""Tests for the metric arithmetic.

These run in milliseconds with no GPU, no Postgres, and no API key, which is the
entire reason the harness was split the way it was. A metric you cannot test is a
metric you cannot trust, and an untrustworthy metric is worse than none — it does
not leave you uncertain, it leaves you confidently wrong about whether a change
helped.
"""

from eval.metrics import (
    align_tokens,
    is_cluster_label,
    keyword_recall,
    strip_inflection,
    tokenize,
    word_error_rate,
)


class TestTokenize:
    def test_case_and_punctuation_are_not_errors(self) -> None:
        assert tokenize("Priya.") == tokenize("priya") == ["priya"]

    def test_contractions_stay_one_word(self) -> None:
        """Splitting "I'll" would invent an error the transcript did not make."""
        assert tokenize("I'll take it") == ["i'll", "take", "it"]

    def test_curly_quotes_fold_to_straight(self) -> None:
        """Whisper emits curly, a human types straight. Not a transcription error."""
        assert tokenize("I’ll") == tokenize("I'll")

    def test_hyphenated_words_stay_one_word(self) -> None:
        assert tokenize("word-level accuracy") == ["word-level", "accuracy"]

    def test_empty_and_whitespace(self) -> None:
        assert tokenize("") == []
        assert tokenize("   \n  ") == []


class TestWordErrorRate:
    def test_identical_is_zero(self) -> None:
        assert word_error_rate("the cat sat", "the cat sat") == 0.0

    def test_substitution(self) -> None:
        assert word_error_rate("the cat sat", "the dog sat") == 1 / 3

    def test_deletion(self) -> None:
        assert word_error_rate("the cat sat", "the sat") == 1 / 3

    def test_insertion(self) -> None:
        assert word_error_rate("the cat sat", "the big cat sat") == 1 / 3

    def test_can_exceed_one(self) -> None:
        """A hallucinated paragraph in silence is worse than saying nothing.

        Capping at 1.0 would hide exactly the failure mode this project fears most.
        """
        assert word_error_rate("hello", "hello and then she said a great deal more") > 1.0

    def test_empty_reference(self) -> None:
        assert word_error_rate("", "") == 0.0
        assert word_error_rate("", "invented words") == 1.0

    def test_empty_hypothesis_is_total_loss(self) -> None:
        assert word_error_rate("the cat sat", "") == 1.0


class TestStripInflection:
    def test_possessive(self) -> None:
        assert strip_inflection("priya's") == "priya"

    def test_plural(self) -> None:
        assert strip_inflection("keys") == "key"

    def test_short_words_keep_their_s(self) -> None:
        """"as" is not a plural "a". Below four characters the guess is worse than
        the miss it prevents."""
        assert strip_inflection("as") == "as"

    def test_double_s_is_not_a_plural(self) -> None:
        assert strip_inflection("access") == "access"


class TestKeywordRecall:
    def test_all_present(self) -> None:
        assert keyword_recall(["ChromaDB", "Priya"], "the ChromaDB work is Priya's") == 1.0

    def test_none_present(self) -> None:
        assert keyword_recall(["ChromaDB"], "the chroma DB work") == 0.0

    def test_case_insensitive(self) -> None:
        assert keyword_recall(["ChromaDB"], "chromadb migration") == 1.0

    def test_possessive_still_counts(self) -> None:
        """The name made it through. That is the question being asked."""
        assert keyword_recall(["Priya"], "that is Priya's call") == 1.0

    def test_multiword_term_must_be_contiguous(self) -> None:
        """Otherwise the metric drifts upward as the meeting gets longer, which is
        the opposite of what a metric should do."""
        found = "we shipped the Kuberya dashboard"
        scattered = "Kuberya is fine and the dashboard is not"
        assert keyword_recall(["Kuberya dashboard"], found) == 1.0
        assert keyword_recall(["Kuberya dashboard"], scattered) == 0.0

    def test_partial(self) -> None:
        assert keyword_recall(["ChromaDB", "Kuberya"], "chromadb only") == 0.5

    def test_no_keywords_is_vacuously_one(self) -> None:
        """Read as "not measured". TranscriptionMetrics.keywords carries the count
        so a report can say which."""
        assert keyword_recall([], "anything at all") == 1.0


class TestAlignTokens:
    def test_identical_sequences_all_match(self) -> None:
        pairs = align_tokens(["a", "b"], ["a", "b"])
        assert pairs == [(0, 0), (1, 1)]

    def test_deletion_is_reference_only(self) -> None:
        assert align_tokens(["a", "b"], ["a"]) == [(0, 0), (1, None)]

    def test_insertion_is_hypothesis_only(self) -> None:
        assert align_tokens(["a"], ["a", "b"]) == [(0, 0), (None, 1)]

    def test_empty_sides(self) -> None:
        assert align_tokens([], []) == []
        assert align_tokens([], ["a"]) == [(None, 0)]
        assert align_tokens(["a"], []) == [(0, None)]

    def test_every_reference_token_appears_exactly_once(self) -> None:
        """The invariant attribution scoring leans on: walk the path and you visit
        every reference word, in order, once."""
        ref = ["the", "cat", "sat", "on", "the", "mat"]
        hyp = ["the", "dog", "sat", "the", "mat", "today"]
        seen = [r for r, _ in align_tokens(ref, hyp) if r is not None]
        assert seen == list(range(len(ref)))


class TestIsClusterLabel:
    def test_unmapped_cluster(self) -> None:
        assert is_cluster_label("SPEAKER_00") is True
        assert is_cluster_label("SPEAKER_07") is True

    def test_unknown_is_also_no_name(self) -> None:
        assert is_cluster_label(None) is True

    def test_a_real_name(self) -> None:
        assert is_cluster_label("Priya") is False

    def test_a_person_whose_name_merely_contains_it(self) -> None:
        """fullmatch, not search — "SPEAKER_00 (phone)" renamed in place by a human
        is a named person, and must not read as an unanswered gate."""
        assert is_cluster_label("SPEAKER_00 (phone)") is False
