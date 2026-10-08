"""Run the pipeline over the evaluation set and score what comes out.

    python -m eval.runner                    # every dataset, transcribing each
    python -m eval.runner --score-only       # score existing hypotheses, no GPU
    python -m eval.runner --dataset kickoff  # just one

Two halves, and the seam between them is the point:

* **Transcribe** — stage a dataset's audio exactly as the upload endpoint would,
  call ``run_transcribe``, and dump what Postgres holds as ``transcript.hyp.json``.
  Needs a GPU, Postgres, ffmpeg, and the ``[diarization]`` extra.
* **Score** — compare that file to gold. Needs nothing.

``--score-only`` runs the second half alone, which is what makes a change to the
metrics testable in a second instead of an hour, and what lets the committed
``example`` dataset prove the scorer works without shipping real audio.

## What this does not do

It stops after transcription. It does not run minutes, and that is not an
oversight: ``run_minutes`` raises while any diarization cluster is unmapped, and
the honest ways to get past that gate are to have a human map every cluster (not
automatable, which is the whole design) or to auto-map them from gold (which
measures a pipeline no user will ever run). Minutes metrics need an LLM judge and
a different argument — see ``judge.py``.

## Two traps, both load-bearing

**A fresh ``meeting_id`` every run.** ``_persist_segments`` deletes the meeting's
segments and its ``DIARIZATION`` speaker rows before writing, and cluster numbering
is not stable across runs. Reusing an id would have each run quietly demolish the
last one's output, which is fine right up until you are diffing two hypotheses to
explain a regression.

**``agenda.txt`` is not ``keywords.txt``.** The agenda primes Whisper's decoder,
and a real meeting has one, so a run without it under-measures the real product.
But keyword recall is scored over ``keywords.txt``, so feeding *that* file to the
decoder would prime the model with the exact rare words we then congratulate it for
hearing. The number would go up and mean nothing. They are separate files, they
must stay separate, and ``agenda.txt`` should hold what was really in the invite —
nothing more.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import uuid
from pathlib import Path

from eval import transcript as tx
from eval.scoring import Report, render, score

DATASETS_DIR = Path(__file__).parent / "datasets"

GOLD_FILE = "transcript.gold.json"
HYP_FILE = "transcript.hyp.json"
KEYWORDS_FILE = "keywords.txt"
AGENDA_FILE = "agenda.txt"

#: Suffixes the extension might have uploaded. ``.wav`` first only because that is
#: what the smoke scripts produce; ffmpeg normalises all of them anyway.
_AUDIO_SUFFIXES = (".wav", ".webm", ".m4a", ".ogg", ".mp3")


def discover(root: Path = DATASETS_DIR) -> list[Path]:
    """Every directory holding a gold transcript, sorted for a stable report."""
    if not root.exists():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and (d / GOLD_FILE).exists())


def find_audio(dataset: Path, track: str) -> Path | None:
    """Locate ``mic.*`` / ``tab.*``, whatever container it arrived in."""
    for suffix in _AUDIO_SUFFIXES:
        candidate = dataset / f"{track}{suffix}"
        if candidate.exists():
            return candidate
    return None


def transcribe(dataset: Path, gold: tx.Transcript) -> tx.Transcript:
    """Push one dataset's audio through the real pipeline; return the hypothesis.

    Imports ``app`` lazily and on purpose. ``app.workers.pipeline`` pulls in a
    Whisper backend at module scope and drags torch behind it, and everything else
    in this package is stdlib-pure so that ``--score-only`` stays instant on a
    machine with no GPU. Hoisting these to the top of the file would quietly undo
    that for every caller.
    """
    from app.db.models.enums import Platform, Track
    from app.db.models.meeting import Meeting
    from app.db.models.recording import Recording
    from app.db.models.segment import Segment
    from app.db.models.speaker import Speaker
    from app.db.session import SessionLocal
    from app.services.audio import storage
    from app.workers.pipeline import run_transcribe
    from sqlalchemy import select

    mic, tab = find_audio(dataset, "mic"), find_audio(dataset, "tab")
    if mic is None or tab is None:
        missing = ", ".join(t for t, p in (("mic", mic), ("tab", tab)) if p is None)
        raise FileNotFoundError(
            f"{dataset.name}: no {missing} audio. Expected e.g. {dataset.name}/mic.webm. "
            f"Use --score-only to score an existing {HYP_FILE} without audio."
        )

    agenda_path = dataset / AGENDA_FILE
    agenda = agenda_path.read_text(encoding="utf-8").strip() if agenda_path.exists() else None

    db = SessionLocal()
    meeting_id = uuid.uuid4()
    try:
        db.add(
            Meeting(
                id=meeting_id,
                title=gold.title or dataset.name,
                platform=Platform.MEET,
                agenda=agenda,
            )
        )

        # The roster, as `getParticipants()` would have captured it: every person in
        # gold, with the local user flagged. This is not a courtesy — the roster is
        # the name source for mapping, it primes Whisper's vocabulary prompt, and it
        # bounds pyannote's `max_speakers`. A run without it is a run of a different
        # system than the one users have.
        for name in gold.speakers:
            db.add(
                Speaker(
                    meeting_id=meeting_id,
                    display_name=name,
                    is_local_user=(name == gold.local_user),
                )
            )

        for track, source in ((Track.MIC, mic), (Track.TAB, tab)):
            dest = storage.track_path(meeting_id, track, suffix=source.suffix)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, dest)
            db.add(
                Recording(
                    meeting_id=meeting_id,
                    track=track,
                    source_path=str(dest),
                    chunk_count=1,
                    is_finalized=True,
                )
            )
        db.commit()

        run_transcribe(db, meeting_id)

        rows = (
            db.execute(
                select(Segment)
                .where(Segment.meeting_id == meeting_id)
                .order_by(Segment.start_ms)
            )
            .scalars()
            .all()
        )
        meeting = db.get(Meeting, meeting_id)

        return tx.Transcript(
            segments=[
                tx.Segment(
                    start_ms=row.start_ms,
                    end_ms=row.end_ms,
                    speaker=row.speaker.display_name if row.speaker else None,
                    # `text`, never `text_en`. WER is measured against what was
                    # said; scoring a translation against a gold transcript would
                    # charge the translator's word choices to Whisper.
                    text=row.text,
                )
                for row in rows
            ],
            title=meeting.title if meeting else gold.title,
            date=gold.date,
            language=meeting.source_language if meeting else None,
            local_user=gold.local_user,
        )
    finally:
        db.close()


def evaluate(dataset: Path, *, score_only: bool) -> Report:
    """Produce one dataset's report, transcribing first unless told not to."""
    gold = tx.load(dataset / GOLD_FILE)
    keywords = tx.load_keywords(dataset / KEYWORDS_FILE)
    hyp_path = dataset / HYP_FILE

    if score_only:
        if not hyp_path.exists():
            raise FileNotFoundError(
                f"{dataset.name}: --score-only needs {HYP_FILE}, which does not exist. "
                "Drop the flag to generate it."
            )
        hypothesis = tx.load(hyp_path)
    else:
        hypothesis = transcribe(dataset, gold)
        tx.save(hypothesis, hyp_path)

    return score(gold, hypothesis, keywords=keywords, name=dataset.name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dataset", help="Evaluate only this dataset directory by name.")
    parser.add_argument(
        "--score-only",
        action="store_true",
        help=f"Score an existing {HYP_FILE} instead of running the pipeline. No GPU needed.",
    )
    parser.add_argument(
        "--datasets-dir",
        type=Path,
        default=DATASETS_DIR,
        help="Where the datasets live (default: eval/datasets).",
    )
    args = parser.parse_args(argv)

    datasets = discover(args.datasets_dir)
    if args.dataset:
        datasets = [d for d in datasets if d.name == args.dataset]
        if not datasets:
            print(f"No dataset named {args.dataset!r} in {args.datasets_dir}.", file=sys.stderr)
            return 1

    if not datasets:
        print(
            f"No datasets found in {args.datasets_dir}.\n"
            f"A dataset is a directory holding {GOLD_FILE}. See eval/README.md.",
            file=sys.stderr,
        )
        return 1

    reports: list[Report] = []
    failed = False
    for dataset in datasets:
        try:
            reports.append(evaluate(dataset, score_only=args.score_only))
        except Exception as exc:  # noqa: BLE001 — one bad dataset must not sink the run
            # Report and carry on. Five meetings is the whole set; losing the other
            # four because one has a typo in its gold file is a bad trade, and the
            # nonzero exit still tells CI something is wrong.
            print(f"{dataset.name}: FAILED — {exc}", file=sys.stderr)
            failed = True

    if reports:
        print(render(reports))

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
