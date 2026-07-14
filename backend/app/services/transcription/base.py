"""Transcription provider interface.

Defined as a Protocol so the engine can be swapped without touching the routes
or the worker. Local ``faster-whisper`` is the implementation we ship; a hosted
API could be dropped in behind the same interface if the GPU ever becomes the
bottleneck.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(slots=True)
class Word:
    """One word with its timing.

    Word-level timings are not a nicety — they are what makes speaker attribution
    possible. Whisper's segment boundaries and the speaker turns do not line up,
    so a segment can straddle a speaker change and only per-word times let us cut
    it in the right place.
    """

    word: str
    start: float
    end: float
    probability: float | None = None


@dataclass(slots=True)
class TranscribedSegment:
    """One segment of transcript, as returned by the engine."""

    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    avg_logprob: float | None = None
    #: High values on fluent-looking text are the signature of a hallucination.
    no_speech_prob: float | None = None


@dataclass(slots=True)
class TranscriptionResult:
    """The full output of a transcription run."""

    language: str | None
    segments: list[TranscribedSegment]
    duration: float | None = None


class TranscriptionProvider(Protocol):
    """Turns an audio file into timed, word-level transcript segments."""

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        vocabulary_prompt: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe an audio file.

        Args:
            audio_path: A 16kHz mono WAV, as produced by the ffmpeg normalize step.
            language: ISO-639-1 code, or None to let the model detect it.
            vocabulary_prompt: Participant names, the meeting title, and agenda
                terms. Priming the decoder with the meeting's own vocabulary is
                the cheapest accuracy win we have, because the words it rescues
                (names, products, acronyms) are precisely the words the minutes
                are built from.

        Returns:
            The transcript with word-level timings.
        """
        ...
