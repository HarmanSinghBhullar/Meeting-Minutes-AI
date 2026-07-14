"""Audio normalization via ffmpeg.

``MediaRecorder`` in the browser produces WebM/Opus. Whisper wants 16kHz mono
PCM. ffmpeg is the bridge, and it is a **system dependency** — a binary that has
to be on PATH, not something pip installs. If transcription fails immediately on
a fresh machine, this is usually why.
"""

import subprocess
from pathlib import Path

from app.core.config import settings

#: Whisper is trained on 16kHz mono audio. Feeding it anything else means it
#: resamples internally anyway, so we do it once, up front, and cache the result.
TARGET_SAMPLE_RATE = 16_000
TARGET_CHANNELS = 1


class FfmpegError(RuntimeError):
    """Raised when ffmpeg fails or is missing."""


def normalize(source: Path, dest: Path) -> Path:
    """Convert an audio file to the 16kHz mono WAV that Whisper expects.

    Args:
        source: The uploaded recording (WebM/Opus, as the browser produced it).
        dest: Where to write the normalized WAV.

    Returns:
        ``dest``.

    Raises:
        FfmpegError: If ffmpeg is missing or the conversion fails.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        settings.ffmpeg_bin,
        "-y",  # overwrite; a re-run should be idempotent
        "-i", str(source),
        "-ar", str(TARGET_SAMPLE_RATE),
        "-ac", str(TARGET_CHANNELS),
        "-c:a", "pcm_s16le",
        str(dest),
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise FfmpegError(
            f"ffmpeg not found at {settings.ffmpeg_bin!r}. It is a system dependency: "
            "install it and put it on PATH, or set FFMPEG_BIN."
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace") if exc.stderr else "<no output>"
        raise FfmpegError(f"ffmpeg failed converting {source.name}:\n{stderr}") from exc

    return dest


def probe_duration(path: Path) -> float | None:
    """Return an audio file's duration in seconds, or None if it can't be read."""
    cmd = [
        settings.ffmpeg_bin.replace("ffmpeg", "ffprobe"),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True)
        return float(result.stdout.decode().strip())
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
        return None
