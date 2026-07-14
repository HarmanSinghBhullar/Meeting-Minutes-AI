"""faster-whisper transcription provider.

Runs ``large-v3`` on the GPU. The parameter choices below are the difference
between a usable transcript and an unusable one, so they are worth reading:

* ``vad_filter`` — Whisper's characteristic failure is not mumbling, it is
  inventing fluent sentences during silence. Stripping silence before the model
  ever sees it removes most of that at the source. Meetings are full of pauses,
  so this matters more here than in most applications.

* ``condition_on_previous_text=False`` — feeding the previous segment back in as
  context is what lets Whisper fall into a repetition loop, where it emits the
  same phrase over and over for minutes. Turning it off costs a little coherence
  and buys immunity from the worst hallucination mode.

* ``word_timestamps=True`` — required for speaker alignment. Not optional for us.

* ``int8_float16`` compute type — keeps large-v3 near 2GB of VRAM, which is what
  lets it coexist with a browser on a 4GB card. Accuracy loss from quantization
  is small enough to be hard to measure on speech; we keep the large-v3 quality
  we chose the model for and pay in memory instead of in words.
"""

from pathlib import Path

from faster_whisper import WhisperModel

from app.core.config import settings
from app.core.cuda import ensure_cuda_libraries
from app.services.transcription.base import (
    TranscribedSegment,
    TranscriptionResult,
    Word,
)


class FasterWhisperProvider:
    """Local Whisper transcription via CTranslate2."""

    def __init__(self) -> None:
        #: Loaded lazily. The worker holds only one model on the GPU at a time —
        #: Whisper and pyannote together will not fit in 4GB, so whichever is
        #: not in use is unloaded rather than kept warm.
        self._model: WhisperModel | None = None

    def _ensure_loaded(self) -> WhisperModel:
        """Load the model on first use."""
        if self._model is None:
            # CTranslate2 links against cuBLAS/cuDNN but does not ship them, and
            # Windows will not find them on PATH. Must happen before the model
            # touches the GPU.
            ensure_cuda_libraries()
            self._model = WhisperModel(
                settings.whisper_model,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
            )
        return self._model

    def unload(self) -> None:
        """Free the model and its VRAM.

        Called by the worker before handing the GPU to another stage.
        """
        if self._model is None:
            return
        self._model = None
        try:
            import torch

            torch.cuda.empty_cache()
        except ImportError:
            # torch is an optional dependency (it arrives with pyannote). Without
            # it there is nothing to reclaim beyond dropping the reference.
            pass

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        vocabulary_prompt: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe a normalized WAV file. See ``TranscriptionProvider``."""
        model = self._ensure_loaded()

        segments_iter, info = model.transcribe(
            str(audio_path),
            language=language,
            initial_prompt=vocabulary_prompt,
            word_timestamps=True,
            vad_filter=settings.whisper_vad_filter,
            condition_on_previous_text=False,
        )

        segments = [
            TranscribedSegment(
                start=s.start,
                end=s.end,
                text=s.text.strip(),
                words=[
                    Word(word=w.word, start=w.start, end=w.end, probability=w.probability)
                    for w in (s.words or [])
                ],
                avg_logprob=s.avg_logprob,
                no_speech_prob=s.no_speech_prob,
            )
            for s in segments_iter  # generator: transcription happens as we iterate
        ]

        return TranscriptionResult(
            language=info.language,
            segments=segments,
            duration=info.duration,
        )
