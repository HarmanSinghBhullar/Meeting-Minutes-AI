"""Primary attribution: pyannote.audio diarization, run out-of-process.

This reads the audio and answers "how many distinct voices, and when did each
one talk". It cannot answer "who" — that is a human's job, via the speaker
mapping UI — but it answers what it does answer from the recording itself, which
is the point. The DOM active-speaker timeline it replaced was faster and free,
but it was a scrape of obfuscated CSS classes, and when a platform reskinned it
the attribution quietly collapsed to "Unknown" with no failure anywhere. A signal
that degrades silently is worse than one that costs a manual step.

**This class does not import torch.** It shells out to ``diarize_cli``, because
CTranslate2 (Whisper) and torch (pyannote) cannot both initialise cuDNN in one
process — the second one to try kills the interpreter outright. See that module's
docstring for the full diagnosis. Keeping the parent torch-free is what makes the
worker survivable, so resist the urge to "simplify" this back into a direct call.

Two operational facts worth knowing before this path is exercised:

1. It is an **optional extra**: ``pip install -e ".[diarization]"``. The version
   bounds in pyproject.toml are all load-bearing — read the comments there.

2. The models are **gated on HuggingFace**. Accept the licence for both
   ``pyannote/speaker-diarization-3.1`` and ``pyannote/segmentation-3.0``, then
   supply ``HUGGINGFACE_TOKEN``.

Output is anonymous clusters — SPEAKER_00, SPEAKER_01 — with no names. A human
labels them afterwards, and the minutes will not be written until they have.
"""

import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

from app.db.models.enums import SpeakerSource
from app.services.attribution.base import SpeakerTurn

logger = logging.getLogger(__name__)

#: Diarization is roughly 5x faster than real time on a modest GPU and far slower
#: on CPU. This is a deadlock guard, not a performance budget — generous enough
#: that a long meeting on a slow machine finishes, short enough that a wedged
#: child process cannot pin the queue forever.
TIMEOUT_SECONDS = 60 * 60


class PyannoteProvider:
    """Speaker diarization via pyannote.audio, in a child process."""

    def unload(self) -> None:
        """Kept for interface symmetry; there is nothing resident to free.

        The child process holds the model and the CUDA context, and both die with
        it. That is the entire benefit of the arrangement: the GPU is released by
        the operating system rather than by anyone remembering to call this.
        """

    def attribute(
        self,
        *,
        meeting_id: str,
        audio_path: Path | None = None,
        max_speakers: int | None = None,
    ) -> list[SpeakerTurn]:
        """Diarize the audio into anonymous speaker turns.

        Args:
            meeting_id: The meeting being attributed. Logging only — diarization
                reads nothing but the audio.
            audio_path: The normalized track to diarize.
            max_speakers: An upper bound on distinct voices, from the participant
                roster. Worth passing: unbounded clustering is where diarization
                invents an extra speaker out of one person's changing microphone
                conditions, and that is the error a user actually notices — the
                same person appearing twice in the mapping UI. Only ever an upper
                bound, never an exact count, because the roster can be wrong (a
                late joiner nobody saw, two people on one laptop) and forcing an
                exact count on a wrong roster splits or merges real speakers.
        """
        if audio_path is None:
            raise ValueError("pyannote needs audio; it cannot attribute without a file.")

        turns = [
            SpeakerTurn(
                speaker_label=str(raw["speaker_label"]),
                start_ms=int(raw["start_ms"]),
                end_ms=int(raw["end_ms"]),
                source=SpeakerSource.DIARIZATION,
            )
            for raw in self._run(audio_path, max_speakers)
        ]

        clusters = len({t.speaker_label for t in turns})
        logger.info(
            "Diarization for meeting %s: %d turns across %d speaker(s)%s",
            meeting_id,
            len(turns),
            clusters,
            f" (roster suggested at most {max_speakers})" if max_speakers else "",
        )
        return turns

    def _run(self, audio_path: Path, max_speakers: int | None) -> list[dict[str, object]]:
        """Run the child and read back its turns."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "turns.json"
            cmd = [
                sys.executable,
                "-m",
                "app.services.attribution.diarize_cli",
                "--audio",
                str(audio_path),
                "--out",
                str(out),
            ]
            if max_speakers and max_speakers > 0:
                cmd += ["--max-speakers", str(max_speakers)]

            logger.info("Diarizing %s in a child process", audio_path.name)
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                cmd,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
            )

            if proc.returncode != 0:
                # The child's stderr is the only useful thing here — it carries the
                # missing-token message, the unaccepted-licence message, and the
                # CUDA errors. Swallowing it would turn every setup mistake into an
                # indistinguishable "diarization failed".
                raise RuntimeError(
                    f"Diarization failed (exit {proc.returncode}).\n"
                    f"{_tail(proc.stderr)}"
                )

            if not out.is_file():
                raise RuntimeError(
                    "Diarization reported success but wrote no output.\n"
                    f"{_tail(proc.stderr)}"
                )

            return json.loads(out.read_text(encoding="utf-8"))


def _tail(text: str, lines: int = 15) -> str:
    """The last few lines of the child's stderr — where the actual error is."""
    kept = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(kept[-lines:])
