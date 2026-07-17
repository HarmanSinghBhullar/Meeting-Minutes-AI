"""The committed `example` dataset, scored end to end through the runner.

Every other test here builds its input inline. This one goes through the real
entry point over real files on disk — discovery, loading, validation, scoring,
rendering — which is the only test that would notice the parts nobody writes a
unit test for: a renamed constant, a broken path, a dataset directory that stops
being found.

The expected values are hand-derived below rather than recorded from a run. A
number copied out of the output of the code it is testing asserts only that the
code still does what it did, which is not the same as doing the right thing, and
these numbers are the whole product of this package.
"""

from pathlib import Path

import pytest

from eval.runner import DATASETS_DIR, HYP_FILE, discover, evaluate, find_audio

EXAMPLE = DATASETS_DIR / "example"

# Gold is 44 words:
#   seg1  8   Right, let's start with the ChromaDB migration. Priya?
#   seg2 11   I'll take the migration. I can have it done by Friday.
#   seg3 13   What about the Kuberya dashboard? Is anyone actually looking at that one yet?
#   seg4  6   That's blocked on the API keys.
#   seg5  6   Okay, I'll chase the keys today.
GOLD_WORDS = 44

# Three planted transcription errors:
#   "ChromaDB" -> "chroma DB"  = one substitution + one insertion  (2)
#   "Kuberya"  -> "Cooperia"   = one substitution                  (1)
WER = 3 / 44

# Two gold words were substituted, so they never matched; 42 remain scoreable.
# Priya's six-word seg4 was given to SPEAKER_01, which maps to Deepak.
SCORED_WORDS = 42
ATTRIBUTION = 36 / 42

# The hypothesis has 45 tokens (44 + the inserted "DB"). Harman's two segments
# carry 15 of them and are the only named ones — his is the mic track, which is
# never diarized and so never needs mapping.
NAMED = 15 / 45

# ChromaDB and "Kuberya dashboard" were both mangled; "Priya" survived.
KEYWORD_RECALL = 1 / 3


class TestExampleDataset:
    def test_it_is_discovered(self) -> None:
        assert EXAMPLE in discover()

    def test_it_holds_no_audio(self) -> None:
        """The reason this one dataset is committed. If it ever grows a real
        recording, .gitignore's exception becomes the hole that publishes it."""
        assert find_audio(EXAMPLE, "mic") is None
        assert find_audio(EXAMPLE, "tab") is None

    def test_scores_are_what_the_fixture_was_built_to_produce(self) -> None:
        report = evaluate(EXAMPLE, score_only=True)

        assert report.name == "example"
        assert report.transcription.reference_words == GOLD_WORDS
        assert report.transcription.wer == pytest.approx(WER)
        assert report.transcription.keyword_recall == pytest.approx(KEYWORD_RECALL)
        assert report.attribution.scored_words == SCORED_WORDS
        assert report.attribution.word_level_accuracy == pytest.approx(ATTRIBUTION)
        assert report.attribution.named_speaker_rate == pytest.approx(NAMED)

    def test_the_planted_attribution_error_is_caught(self) -> None:
        """Stated as its own test because it is the claim the harness exists to
        make. If cluster mapping ever starts laundering misattributed words, this
        goes to 1.0 and everything else still passes."""
        report = evaluate(EXAMPLE, score_only=True)

        assert report.attribution.word_level_accuracy < 1.0

    def test_the_gate_is_visible_but_is_not_an_accuracy_failure(self) -> None:
        report = evaluate(EXAMPLE, score_only=True)

        assert report.attribution.named_speaker_rate < 0.5  # two of three unmapped
        assert report.attribution.word_level_accuracy > 0.8  # the diarizer did fine

    def test_scoring_needs_no_audio_gpu_or_database(self) -> None:
        """`--score-only` must never reach for `app`. If someone hoists the lazy
        imports in `runner.transcribe` to module scope, this still passes — but
        `sys.modules` tells the truth."""
        import sys

        evaluate(EXAMPLE, score_only=True)

        assert "torch" not in sys.modules
        assert "faster_whisper" not in sys.modules

    def test_score_only_says_so_when_there_is_no_hypothesis(self, tmp_path: Path) -> None:
        (tmp_path / "transcript.gold.json").write_text(
            '{"segments": [{"start_ms": 0, "end_ms": 1, "speaker": "A", "text": "x"}]}',
            encoding="utf-8",
        )

        with pytest.raises(FileNotFoundError, match=HYP_FILE):
            evaluate(tmp_path, score_only=True)
