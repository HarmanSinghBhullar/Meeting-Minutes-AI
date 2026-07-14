"""OpenAI structured-output provider.

Uses the Responses API's ``parse`` helper, which takes a Pydantic model directly
and enforces it server-side in strict mode. That is the property we actually need
— the same one the Anthropic provider gives us — so the schemas in
``minutes/schemas.py`` are shared between the two, unchanged.

One consequence of strict mode worth knowing: OpenAI requires **every** property
to appear in the schema's ``required`` list. Optional fields therefore have to be
nullable (``str | None``) rather than absent, which is exactly how the minutes
schemas already declare ``owner`` and ``due_date`` — a field the model must
answer, even if the answer is "nobody". That happens to be the behaviour we want:
it forces the model to say it found no owner rather than to quietly omit the key.
"""

import logging
from typing import TypeVar

from openai import OpenAI
from pydantic import BaseModel

from app.services.minutes.providers.base import Effort

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


class OpenAIProvider:
    """Structured completions via the OpenAI Responses API."""

    def __init__(self, api_key: str) -> None:
        self._client = OpenAI(api_key=api_key)

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
        response = self._client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            text_format=schema,
            reasoning={"effort": effort},
            max_output_tokens=max_tokens,
        )

        parsed = response.output_parsed
        if parsed is None:
            # Either a refusal or a truncated response. Both mean "no answer",
            # and the callers are built to fail closed on that: an item with no
            # extraction is simply not extracted, and an item with no grounding
            # verdict is not grounded.
            logger.warning("OpenAI returned no parsed output (refusal or truncation).")
            return None

        return parsed
