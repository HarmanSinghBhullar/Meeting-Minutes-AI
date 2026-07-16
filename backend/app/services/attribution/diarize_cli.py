"""Diarize one audio file, in a process of its own.

    python -m app.services.attribution.diarize_cli --audio x.wav --out turns.json

**Why this is a subprocess and not a function call.** faster-whisper (CTranslate2)
and pyannote (torch) both link cuDNN, and they cannot both initialise it inside
one process on Windows: once CTranslate2 has run on the GPU, torch's cuDNN load
dies with `Could not load symbol cudnnGetLibConfig. Error code 127` and takes the
interpreter with it — exit code 127, no Python traceback, nothing to catch.

It is not a version conflict. It reproduces with the two `cudnn64_9.dll` copies
(torch's bundled one and `nvidia-cudnn-cu12`'s) byte-for-byte identical. The two
runtimes simply will not share the library in one address space.

Unloading does not help, because the problem is not VRAM — the process is already
poisoned by the time the second library loads. The only thing that reliably
separates two CUDA runtimes is an OS process boundary, so the worker pays one
interpreter start (~2s, against a job measured in minutes) and gets a guarantee
instead of a race. Process exit also frees the GPU completely, which is the
"one model resident at a time" discipline the pipeline wanted anyway, enforced by
the operating system rather than by remembering to call `unload()`.

Results go to a **file**, not stdout: pyannote, lightning, and speechbrain all
write chatter to the console, and mixing that with the payload would make parsing
a guessing game.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logger = logging.getLogger(__name__)


def diarize(audio: Path, *, max_speakers: int | None = None) -> list[dict[str, object]]:
    """Run the pyannote pipeline and return plain dicts.

    Imports live inside the function so that `--help` and argument errors do not
    pay for loading torch.
    """
    from app.core.config import settings

    try:
        from pyannote.audio import Pipeline
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError(
            "pyannote.audio is not installed, and it is now the only source of "
            'speaker attribution. Install the extra: pip install -e ".[diarization]"'
        ) from exc

    if not settings.huggingface_token:
        raise RuntimeError(
            "HUGGINGFACE_TOKEN is not set. pyannote's models are gated: accept the "
            "licence for BOTH pyannote/segmentation-3.0 and "
            "pyannote/speaker-diarization-3.1 on their HuggingFace model pages, "
            "then set a read token."
        )

    pipeline = Pipeline.from_pretrained(
        settings.pyannote_model,
        use_auth_token=settings.huggingface_token,
    )
    if pipeline is None:
        # from_pretrained returns None rather than raising when the token is
        # valid but the licence has not been accepted — the single most common
        # setup failure, and utterly opaque if passed along as an AttributeError.
        raise RuntimeError(
            f"Could not load {settings.pyannote_model}. The token is set, so the "
            "likely cause is an unaccepted licence: visit the model page for BOTH "
            "pyannote/segmentation-3.0 and pyannote/speaker-diarization-3.1 and "
            "accept the terms."
        )

    _to_device(pipeline)

    # min_speakers is deliberately not passed: 1 is already the floor, and a
    # meeting where only one person spoke is perfectly ordinary.
    kwargs = {"max_speakers": max_speakers} if max_speakers and max_speakers > 0 else {}
    annotation = pipeline(str(audio), **kwargs)

    return [
        {
            "speaker_label": label,
            "start_ms": int(turn.start * 1000),
            "end_ms": int(turn.end * 1000),
        }
        for turn, _, label in annotation.itertracks(yield_label=True)
    ]


def _to_device(pipeline: object) -> None:
    """Move the pipeline onto the configured device.

    pyannote defaults to CPU if nobody says otherwise, where an hour of audio
    takes tens of minutes rather than a couple. A device that does not exist is a
    misconfiguration, not a reason to fail the job: a slow transcript beats no
    transcript, so this warns and carries on.
    """
    from app.core.config import settings

    try:
        import torch

        pipeline.to(torch.device(settings.pyannote_device))  # type: ignore[attr-defined]
        print(f"diarizing on {settings.pyannote_device}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - any device error means "use CPU"
        print(
            f"could not move pyannote onto {settings.pyannote_device} ({exc}); "
            "falling back to CPU, which is several times slower",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diarize an audio file.")
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path, help="Where to write the JSON turns.")
    parser.add_argument("--max-speakers", type=int, default=None)
    args = parser.parse_args(argv)

    if not args.audio.is_file():
        print(f"No such audio file: {args.audio}", file=sys.stderr)
        return 2

    turns = diarize(args.audio, max_speakers=args.max_speakers)
    args.out.write_text(json.dumps(turns), encoding="utf-8")
    print(f"wrote {len(turns)} turns to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
