"""Tests for the gold/hypothesis format.

Loading is strict, and these tests are mostly about the strictness. A gold file
misread in silence does not fail — it reports a worse score, and you go hunting for
a regression in the model that was never there. Every error below is one someone
will actually make at 1am with a text editor open.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from eval.transcript import Segment, Transcript, load, load_keywords, save


def _write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "transcript.gold.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "Sync",
        "local_user": "Harman",
        "segments": [{"start_ms": 0, "end_ms": 1000, "speaker": "Harman", "text": "hello"}],
    }
    payload.update(overrides)
    return payload


class TestLoad:
    def test_round_trip(self, tmp_path: Path) -> None:
        original = Transcript(
            segments=[Segment(start_ms=0, end_ms=1000, speaker="Priya", text="hello")],
            title="Sync",
            date="2026-05-14",
            language="en",
            local_user="Priya",
        )
        path = tmp_path / "t.json"
        save(original, path)

        assert load(path) == original

    def test_segments_are_sorted_by_time(self, tmp_path: Path) -> None:
        """Alignment does not care about order, but the rendered transcript does. A
        gold file typed out of order would read as nonsense and still score fine."""
        path = _write(
            tmp_path,
            _payload(
                local_user=None,
                segments=[
                    {"start_ms": 5000, "end_ms": 6000, "speaker": "B", "text": "second"},
                    {"start_ms": 0, "end_ms": 1000, "speaker": "A", "text": "first"},
                ],
            ),
        )

        assert [s.text for s in load(path).segments] == ["first", "second"]

    def test_speakers_are_in_order_of_first_appearance(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            _payload(
                local_user=None,
                segments=[
                    {"start_ms": 0, "end_ms": 1, "speaker": "Zoe", "text": "a"},
                    {"start_ms": 2, "end_ms": 3, "speaker": "Adam", "text": "b"},
                    {"start_ms": 4, "end_ms": 5, "speaker": "Zoe", "text": "c"},
                ],
            ),
        )

        assert load(path).speakers == ["Zoe", "Adam"]

    def test_null_speaker_is_allowed(self, tmp_path: Path) -> None:
        """A voice the corrector could not place. Rare in gold, and a real answer."""
        path = _write(
            tmp_path,
            _payload(
                local_user=None,
                segments=[{"start_ms": 0, "end_ms": 1, "speaker": None, "text": "mumbling"}],
            ),
        )

        assert load(path).segments[0].speaker is None


class TestLoadRejects:
    def test_seconds_where_milliseconds_belong(self, tmp_path: Path) -> None:
        """The most likely mistake by a mile, and the one that would otherwise pass
        silently — the pipeline's own word timings really are float seconds."""
        path = _write(
            tmp_path,
            _payload(segments=[{"start_ms": 1.5, "end_ms": 4.8, "speaker": "A", "text": "x"}]),
        )

        with pytest.raises(ValueError, match="Seconds are the most likely mistake"):
            load(path)

    def test_backwards_times(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            _payload(segments=[{"start_ms": 5000, "end_ms": 1000, "speaker": "A", "text": "x"}]),
        )

        with pytest.raises(ValueError, match="precedes"):
            load(path)

    def test_missing_text(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _payload(segments=[{"start_ms": 0, "end_ms": 1, "speaker": "A"}]))

        with pytest.raises(ValueError, match="missing required key 'text'"):
            load(path)

    def test_empty_speaker_name(self, tmp_path: Path) -> None:
        """An empty name is indistinguishable from a typo. `null` says it on purpose."""
        path = _write(
            tmp_path,
            _payload(segments=[{"start_ms": 0, "end_ms": 1, "speaker": "  ", "text": "x"}]),
        )

        with pytest.raises(ValueError, match="empty string"):
            load(path)

    def test_local_user_who_never_speaks(self, tmp_path: Path) -> None:
        """A typo here silently renames the local user for the whole run."""
        path = _write(tmp_path, _payload(local_user="Harmna"))

        with pytest.raises(ValueError, match="never speaks"):
            load(path)

    def test_missing_segments_key(self, tmp_path: Path) -> None:
        path = _write(tmp_path, {"title": "Sync"})

        with pytest.raises(ValueError, match="missing required key 'segments'"):
            load(path)

    def test_not_json(self, tmp_path: Path) -> None:
        path = tmp_path / "t.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(ValueError, match="not valid JSON"):
            load(path)

    def test_error_names_the_segment(self, tmp_path: Path) -> None:
        """You are looking for one bad line in nine hundred."""
        path = _write(
            tmp_path,
            _payload(
                local_user=None,
                segments=[
                    {"start_ms": 0, "end_ms": 1, "speaker": "A", "text": "fine"},
                    {"start_ms": 2, "end_ms": 3, "speaker": "A"},
                ],
            ),
        )

        with pytest.raises(ValueError, match="segment 1"):
            load(path)


class TestText:
    def test_joins_segments_in_order(self) -> None:
        transcript = Transcript(
            segments=[
                Segment(start_ms=0, end_ms=1, speaker="A", text="the cat"),
                Segment(start_ms=2, end_ms=3, speaker="B", text="sat down"),
            ]
        )

        assert transcript.text == "the cat sat down"

    def test_skips_empty_segments(self) -> None:
        transcript = Transcript(
            segments=[
                Segment(start_ms=0, end_ms=1, speaker="A", text="hello"),
                Segment(start_ms=2, end_ms=3, speaker="B", text="   "),
            ]
        )

        assert transcript.text == "hello"


class TestKeywords:
    def test_comments_and_blanks_are_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "keywords.txt"
        path.write_text("# a comment\n\nChromaDB\nKuberya  # trailing\n", encoding="utf-8")

        assert load_keywords(path) == ["ChromaDB", "Kuberya"]

    def test_duplicates_are_dropped(self, tmp_path: Path) -> None:
        """Listing a name twice would silently weight it double."""
        path = tmp_path / "keywords.txt"
        path.write_text("ChromaDB\nChromaDB\n", encoding="utf-8")

        assert load_keywords(path) == ["ChromaDB"]

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert load_keywords(tmp_path / "nope.txt") == []
