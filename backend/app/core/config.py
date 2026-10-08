"""Application configuration.

All settings are read from environment variables (or a local ``.env`` file) and
validated once at import time. Import ``settings`` rather than reading
``os.environ`` anywhere else in the codebase.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings, loaded from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database ---
    database_url: str = Field(alias="DATABASE_URL")

    # --- API ---
    api_host: str = Field(default="127.0.0.1", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")
    cors_origins: str = Field(default="", alias="CORS_ORIGINS")
    #: The extension calls the API from a chrome-extension:// origin whose id is
    #: assigned by Chrome at install time and differs per machine, so pinning it
    #: in config makes every fresh checkout fail with an opaque CORS error. The
    #: API binds to 127.0.0.1 and has no auth, so any local process can already
    #: reach it — a permissive regex here concedes nothing that isn't already open.
    #: Tighten this to the published extension id before this leaves localhost.
    cors_origin_regex: str = Field(
        default=r"chrome-extension://.*", alias="CORS_ORIGIN_REGEX"
    )

    # --- Audio storage ---
    storage_dir: Path = Field(default=Path("./storage"), alias="STORAGE_DIR")
    ffmpeg_bin: str = Field(default="ffmpeg", alias="FFMPEG_BIN")

    # --- Transcription ---
    whisper_model: str = Field(default="large-v3", alias="WHISPER_MODEL")
    whisper_device: str = Field(default="cuda", alias="WHISPER_DEVICE")
    # int8_float16 keeps large-v3 near ~2GB VRAM, which is what makes it fit
    # alongside a browser on a 4GB card. See docs/accuracy.md.
    whisper_compute_type: str = Field(default="int8_float16", alias="WHISPER_COMPUTE_TYPE")
    whisper_vad_filter: bool = Field(default=True, alias="WHISPER_VAD_FILTER")

    # --- Diarization ---
    #: The only source of speaker attribution for the tab track. Gated models:
    #: HUGGINGFACE_TOKEN is required, and the licences must be accepted first.
    huggingface_token: str | None = Field(default=None, alias="HUGGINGFACE_TOKEN")
    pyannote_model: str = Field(
        default="pyannote/speaker-diarization-3.1", alias="PYANNOTE_MODEL"
    )
    #: pyannote defaults to CPU if nobody says otherwise, which costs tens of
    #: minutes on a long meeting. Whisper has unloaded by the time diarization
    #: runs, so there is no contention for the card.
    pyannote_device: str = Field(default="cuda", alias="PYANNOTE_DEVICE")
    #: Turning this off does not degrade attribution — it removes it. Every remote
    #: speaker becomes "Unknown", and the minutes lose their owners. It exists for
    #: running the pipeline on a machine with no GPU and no HF token at all.
    diarization_enabled: bool = Field(default=True, alias="DIARIZATION_ENABLED")

    # --- LLM ---
    #: "groq", "google", "openai", or "anthropic". The minutes and grounding code
    #: never names a provider — everything that makes the output trustworthy sits
    #: above the provider interface, so this is a genuine one-line switch.
    llm_provider: str = Field(default="groq", alias="LLM_PROVIDER")

    groq_api_key: str | None = Field(default=None, alias="GROQ_API_KEY")
    google_api_key: str | None = Field(default=None, alias="GOOGLE_API_KEY")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    #: Model ids are provider-specific — change these when you change provider.
    #: The extractor decides what counts as a commitment, which is the harder
    #: judgement (so it gets the 70B model); the grounding pass answers a narrow
    #: yes/no question about lines it can see, so it can run on the fast 8B one.
    minutes_model: str = Field(default="openai/gpt-oss-120b", alias="MINUTES_MODEL")
    grounding_model: str = Field(default="openai/gpt-oss-20b", alias="GROUNDING_MODEL")
    #: Translation is line-wise transduction, but quality still matters for names
    #: and code-switching, so it keeps the larger model rather than the 8B.
    translation_model: str = Field(
        default="openai/gpt-oss-120b", alias="TRANSLATION_MODEL"
    )

    # --- RAG ---
    chroma_dir: Path = Field(default=Path("./chroma"), alias="CHROMA_DIR")
    embedding_model: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2", alias="EMBEDDING_MODEL"
    )

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS origins as a list. The extension's origin is a chrome-extension:// URL."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment


settings = get_settings()
