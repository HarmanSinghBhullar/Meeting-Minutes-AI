"""Fallback attribution: pyannote.audio diarization.

Used only when there is no DOM timeline — a conference-room microphone, a
desktop meeting client, or a platform we have not written an adapter for.

Two operational facts worth knowing before this path is exercised:

1. The models are **gated on HuggingFace**. You must accept the licence for
   ``pyannote/speaker-diarization-3.1`` and its segmentation dependency on the
   model pages, then supply ``HUGGINGFACE_TOKEN``. Without that, loading fails
   with a 401 that reads like a network error.

2. It wants the GPU, and it will not fit alongside Whisper on a 4GB card. The
   worker unloads Whisper before calling this, which is why ``unload()`` exists
   on both providers.

Output is anonymous clusters — SPEAKER_00, SPEAKER_01 — with no names. A human
has to label them afterwards, which is exactly the manual step the DOM timeline
saves us in the common case.
"""

from pathlib import Path

from app.core.config import settings
from app.db.models.enums import SpeakerSource
from app.services.attribution.base import SpeakerTurn


class PyannoteProvider:
    """Speaker diarization via pyannote.audio."""

    def __init__(self) -> None:
        self._pipeline: object | None = None

    def _ensure_loaded(self) -> object:
        """Load the diarization pipeline on first use.

        Imported lazily: pyannote pulls in torch, and it is an optional extra so
        that installs which never need the fallback path stay light.
        """
        if self._pipeline is not None:
            return self._pipeline

        from pyannote.audio import Pipeline  # noqa: PLC0415  (deliberately lazy)

        if not settings.huggingface_token:
            raise RuntimeError(
                "HUGGINGFACE_TOKEN is not set. pyannote's models are gated: accept "
                "the licence on the model page and set a token, or rely on the DOM "
                "speaker timeline instead."
            )

        self._pipeline = Pipeline.from_pretrained(
            settings.pyannote_model,
            use_auth_token=settings.huggingface_token,
        )
        return self._pipeline

    def unload(self) -> None:
        """Free the pipeline and its VRAM."""
        if self._pipeline is None:
            return
        self._pipeline = None
        try:
            import torch

            torch.cuda.empty_cache()
        except ImportError:
            pass

    def attribute(
        self, *, meeting_id: str, audio_path: Path | None = None
    ) -> list[SpeakerTurn]:
        """Diarize the audio into anonymous speaker turns."""
        if audio_path is None:
            raise ValueError("pyannote needs audio; it cannot attribute without a file.")

        pipeline = self._ensure_loaded()
        diarization = pipeline(str(audio_path))  # type: ignore[operator]

        return [
            SpeakerTurn(
                speaker_label=label,
                start_ms=int(turn.start * 1000),
                end_ms=int(turn.end * 1000),
                source=SpeakerSource.DIARIZATION,
            )
            for turn, _, label in diarization.itertracks(yield_label=True)
        ]
