"""Anthropic structured-output provider.

Two parameters here are recent changes and are easy to get wrong from memory:

* ``thinking={"type": "adaptive"}`` must be set **explicitly**. On the current
  Opus models, omitting the field does not give you adaptive thinking — it gives
  you none. The older ``budget_tokens`` form is now rejected outright.

* There is no ``temperature``. Sampling parameters were removed and now return a
  400 rather than being quietly ignored.
"""

import logging
from typing import TypeVar

from anthropic import Anthropic
from pydantic import BaseModel

from app.services.minutes.providers.base import Effort

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


class AnthropicProvider:
    """Structured completions via the Anthropic Messages API."""

    def __init__(self, api_key: str) -> None:
        self._client = Anthropic(api_key=api_key)

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
        response = self._client.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )

        parsed = response.parsed_output
        if parsed is None:
            logger.warning("Anthropic returned no parsed output (refusal or truncation).")
            return None

        return parsed
