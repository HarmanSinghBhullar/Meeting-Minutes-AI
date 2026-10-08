"""Google (Gemini) structured-output provider.

Uses the google-genai SDK's native structured output: a Pydantic model passed as
``response_schema`` (with ``response_mime_type="application/json"``) is enforced by
the API, and ``response.parsed`` comes back as a validated instance — the same
property the OpenAI and Anthropic providers give, so the schemas in
``minutes/schemas.py`` are shared between all three, unchanged.

Two Gemini-specific things worth knowing:

* Reasoning is a **thinking budget** — a token count — not a named effort level, so
  ``effort`` maps to a budget here (see ``_THINKING_BUDGET``). The budgets are kept
  well under ``max_tokens`` because on Gemini 2.5 the thinking tokens are drawn from
  the *same* output allowance as the answer: a budget larger than the ceiling
  starves the response and it comes back truncated (i.e. ``parsed is None``).

* ``parsed`` is None on a refusal, a safety block, or a truncated response. All
  three mean "no answer", which is exactly what the callers fail closed on — an
  unextracted item is simply not extracted, an ungrounded item is not grounded.
"""

import logging
from typing import TypeVar, cast

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.services.minutes.providers.base import Effort

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

#: Reasoning-effort → thinking-token budget. Each value sits inside the band both
#: Gemini 2.5 Flash and Pro accept (Pro will not disable thinking or go below 128;
#: Flash caps at 24576) and comfortably under the default ``max_tokens`` of 16000,
#: since thinking spends the same output budget as the answer. So the same mapping
#: is safe whichever 2.5 model is configured.
_THINKING_BUDGET: dict[Effort, int] = {
    "low": 1024,
    "medium": 4096,
    "high": 8192,
}


class GoogleProvider:
    """Structured completions via the Gemini API."""

    def __init__(self, api_key: str) -> None:
        self._client = genai.Client(api_key=api_key)

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        schema: type[ModelT],
        model: str,
        effort: Effort = "high",
        max_tokens: int = 16_000,
    ) -> ModelT | None:
        """Run one structured completion. See ``StructuredLLM``."""
        response = self._client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=schema,
                max_output_tokens=max_tokens,
                thinking_config=types.ThinkingConfig(
                    thinking_budget=_THINKING_BUDGET[effort],
                ),
            ),
        )

        parsed = response.parsed
        if parsed is None:
            logger.warning("Gemini returned no parsed output (refusal, block, or truncation).")
            return None

        # With a Pydantic ``response_schema`` the SDK returns an instance of it; its
        # static type is the broad parsed union, so narrow it back to the schema.
        return cast(ModelT, parsed)
