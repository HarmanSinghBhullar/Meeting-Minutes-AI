"""Structured-LLM providers.

Which one is used is a config choice (``LLM_PROVIDER``), not a code change. The
minutes and grounding services never name a provider — they ask for one and get
whatever is configured.
"""

from functools import lru_cache

from app.core.config import settings
from app.services.minutes.providers.base import Effort, StructuredLLM

__all__ = ["Effort", "StructuredLLM", "get_provider"]


@lru_cache
def get_provider() -> StructuredLLM:
    """Return the configured structured-LLM provider.

    Cached: the clients are cheap to hold and expensive to rebuild per call.
    """
    provider = settings.llm_provider.lower()

    if provider == "groq":
        if not settings.groq_api_key:
            raise RuntimeError(
                "LLM_PROVIDER is 'groq' but GROQ_API_KEY is not set. "
                "Minutes cannot be generated."
            )
        # Imported lazily so that an install using only one provider does not
        # need the others' SDKs present.
        from app.services.minutes.providers.groq_provider import GroqProvider

        return GroqProvider(api_key=settings.groq_api_key)

    if provider == "google":
        if not settings.google_api_key:
            raise RuntimeError(
                "LLM_PROVIDER is 'google' but GOOGLE_API_KEY is not set. "
                "Minutes cannot be generated."
            )
        # Imported lazily so that an install using only one provider does not
        # need the others' SDKs present.
        from app.services.minutes.providers.google_provider import GoogleProvider

        return GoogleProvider(api_key=settings.google_api_key)

    if provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError(
                "LLM_PROVIDER is 'openai' but OPENAI_API_KEY is not set. "
                "Minutes cannot be generated."
            )
        # Imported lazily so that an install using only one provider does not
        # need the other's SDK present.
        from app.services.minutes.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(api_key=settings.openai_api_key)

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise RuntimeError(
                "LLM_PROVIDER is 'anthropic' but ANTHROPIC_API_KEY is not set. "
                "Minutes cannot be generated."
            )
        from app.services.minutes.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key=settings.anthropic_api_key)

    raise RuntimeError(
        f"Unknown LLM_PROVIDER {settings.llm_provider!r}. "
        "Expected 'groq', 'google', 'openai', or 'anthropic'."
    )
