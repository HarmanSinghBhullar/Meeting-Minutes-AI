"""The transcript format — one shape for both gold and hypothesis.

A hypothesis is a transcript the machine wrote. Gold is a transcript a human
corrected. They are the same thing, so they get the same schema, and that single
decision buys three things worth having:

* **Scoring is a pure function of two files.** ``scoring.score(gold, hyp)`` needs
  no GPU, no Postgres, no audio, and no API key. It is testable today, on a
  laptop, in milliseconds — which is why the metrics in this package have tests
  and the pipeline-driving half does not.
* **A run is inspectable.** The runner dumps ``transcript.hyp.json`` next to the
  gold. When a number moves, the diff between two hypothesis files says why, and
  it is readable.
* **Bootstrapping gold is a rename.** Correcting a hypothesis by hand is an
  afternoon; typing a transcript from silence is a week. (It biases gold toward
  the machine's own mistakes — you must actually listen, not skim. But the
  alternative is no gold set at all, which is the state this repository has been
  in since the beginning.)

Deliberately **not** a mirror of ``app.db.models.Segment``. This file has no
``app`` import and never should: the moment eval depends on the ORM it depends on
torch and a Whisper backend (see ``runner.py``), and a scorer that needs a GPU to
compare two strings will not get run.

## The format

```json
{
  "title": "Weekly sync",
  "date": "2026-05-14",
  "language": "en",
  "local_user": "Harman",
  "segments": [
    {"start_ms": 1200, "end_ms": 4800, "speaker": "Priya", "text": "I'll take the migration."}
  ]
}
```

Times are **milliseconds, integers, everywhere** — including in gold, which a
human types. The pipeline itself is not so lucky: ``Segment.start_ms`` is int ms
while ``Segment.words[].start`` is float *seconds*, a seam that exists for good
reasons upstream and is worth exactly nothing here. The runner converts once, at
the boundary, and this side of it has one unit.

Word-level timings are **not** part of the format. They are not needed: every
metric here is computed by aligning word *sequences*, so a word inherits its
segment's speaker and the timings never enter the arithmetic. Requiring them
would have made hand-correction impossible and bought nothing. (Time-based DER
would need them. If that day comes, add them then, and only then.)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Segment:
    """One continuous stretch of one person talking.

    ``speaker`` is ``None`` when nobody was identified. In gold that means a voice
    the corrector could not place, and it should be rare; in a hypothesis it is the
    pipeline saying ``UNKNOWN``, which is a real answer and is scored as one.
    """

    start_ms: int
    end_ms: int
    speaker: str | None
    text: str


@dataclass(slots=True)
class Transcript:
    """A whole meeting's transcript, gold or hypothesis."""

    segments: list[Segment] = field(default_factory=list)
    title: str | None = None
    date: str | None = None
    language: str | None = None
    #: Which speaker is the person running the extension. Not decoration: the
    #: pipeline never diarizes the mic track (it is the local user by definition),
    #: so a run needs to know whose voice that is or it invents a name. Gold is
    #: where that fact lives, because it is a fact about the meeting.
    local_user: str | None = None

    @property
    def text(self) -> str:
        """Every segment's text in time order, joined.

        The input to WER, which does not care who spoke.
        """
        return " ".join(s.text for s in self.segments if s.text.strip())

    @property
    def speakers(self) -> list[str]:
        """Distinct speaker names, in order of first appearance.

        Order is first-appearance rather than sorted so that reading it tells you
        something about the meeting rather than about the alphabet.
        """
        seen: list[str] = []
        for segment in self.segments:
            if segment.speaker is not None and segment.speaker not in seen:
                seen.append(segment.speaker)
        return seen


def _segment_from_json(raw: dict[str, Any], *, where: str) -> Segment:
    """Build one segment, complaining precisely about what is wrong.

    Gold is hand-written, so the error messages here are a feature: they are read
    by someone with a text editor open at 1am, and "missing 'text'" is worth ten
    minutes more than a ``KeyError``.
    """
    for key in ("start_ms", "end_ms", "text"):
        if key not in raw:
            raise ValueError(f"{where}: missing required key {key!r}")

    start_ms, end_ms = raw["start_ms"], raw["end_ms"]
    if not isinstance(start_ms, int) or not isinstance(end_ms, int):
        raise ValueError(
            f"{where}: start_ms/end_ms must be integer milliseconds, got "
            f"{start_ms!r}/{end_ms!r}. Seconds are the most likely mistake."
        )
    if end_ms < start_ms:
        raise ValueError(f"{where}: end_ms ({end_ms}) precedes start_ms ({start_ms})")

    speaker = raw.get("speaker")
    if speaker is not None and not isinstance(speaker, str):
        raise ValueError(f"{where}: speaker must be a string or null, got {speaker!r}")
    if isinstance(speaker, str) and not speaker.strip():
        raise ValueError(
            f"{where}: speaker is an empty string. Use null to mean 'not identified' — "
            "an empty name is indistinguishable from a typo."
        )

    text = raw["text"]
    if not isinstance(text, str):
        raise ValueError(f"{where}: text must be a string, got {text!r}")

    return Segment(start_ms=start_ms, end_ms=end_ms, speaker=speaker, text=text)


def load(path: Path) -> Transcript:
    """Read a transcript, gold or hypothesis, and validate it.

    Validation is strict on purpose. A silently-misread gold file does not fail;
    it reports a worse score, and you go looking for the regression in the model.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: not valid JSON — {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(
            f"{path}: expected a JSON object at the top level, got {type(raw).__name__}"
        )
    if "segments" not in raw:
        raise ValueError(f"{path}: missing required key 'segments'")
    if not isinstance(raw["segments"], list):
        raise ValueError(f"{path}: 'segments' must be a list")

    segments = [
        _segment_from_json(item, where=f"{path.name} segment {i}")
        for i, item in enumerate(raw["segments"])
    ]

    # Sorted defensively. Alignment does not depend on order, but the rendered
    # transcript does, and a gold file typed out of order would otherwise read as
    # nonsense while scoring perfectly.
    segments.sort(key=lambda s: s.start_ms)

    transcript = Transcript(
        segments=segments,
        title=raw.get("title"),
        date=raw.get("date"),
        language=raw.get("language"),
        local_user=raw.get("local_user"),
    )

    if transcript.local_user is not None and transcript.local_user not in transcript.speakers:
        raise ValueError(
            f"{path}: local_user {transcript.local_user!r} never speaks in this transcript. "
            f"Known speakers: {transcript.speakers or ['(none)']}. A typo here silently "
            "renames the local user for the whole run."
        )

    return transcript


def save(transcript: Transcript, path: Path) -> None:
    """Write a transcript as JSON, formatted for a human to correct by hand."""
    payload: dict[str, Any] = {
        "title": transcript.title,
        "date": transcript.date,
        "language": transcript.language,
        "local_user": transcript.local_user,
        "segments": [
            {
                "start_ms": s.start_ms,
                "end_ms": s.end_ms,
                "speaker": s.speaker,
                "text": s.text,
            }
            for s in transcript.segments
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_keywords(path: Path) -> list[str]:
    """Read ``keywords.txt``: one term per line, ``#`` comments, blanks ignored.

    Terms may be multi-word ("Kuberya dashboard"). Duplicates are dropped — the
    metric is recall over distinct terms, and listing a name twice would silently
    weight it double.
    """
    if not path.exists():
        return []

    keywords: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        term = line.split("#", 1)[0].strip()
        if term and term not in keywords:
            keywords.append(term)
    return keywords
