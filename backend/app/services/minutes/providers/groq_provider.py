"""Groq (OpenAI-compatible) structured-output provider.

Groq serves open models (Llama and friends) behind an OpenAI-compatible API — fast,
and with a generous free tier — which is exactly what makes it a good fallback when
a metered provider's quota runs out mid-meeting.

**Structured output here is JSON mode plus a Pydantic gate, not a server-side schema
constraint.** Groq has no single schema-enforcement mode that every model supports,
so instead: the request asks for a JSON object (``response_format`` json mode
guarantees the wrapper is *well-formed* JSON), the target schema is handed to the
model in the system message, and the reply is validated with
``schema.model_validate_json``. The interface's promise still holds — a malformed or
off-schema response never reaches a caller, because validation failure returns
``None``, which is the "no answer" the callers already fail closed on (an
unextracted item is simply not extracted; an ungrounded item is not grounded). The
trade against OpenAI/Gemini's server-side enforcement is that a wrong shape costs a
retry-less ``None`` rather than being impossible; the gain is that this works on
*every* Groq chat model rather than the shifting subset that supports json_schema.
"""

import json
import logging
from typing import TypeVar

from groq import Groq
from pydantic import BaseModel, ValidationError

from app.services.minutes.providers.base import Effort

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

#: Cap on the completion reservation, because Groq bills the *requested* ``max_tokens``
#: against its per-minute token budget, not just the tokens actually generated. The
#: free tier is 12k TPM on the 70B model, so the interface's 16k default 413s ("request
#: too large") before the prompt is even read — a tiny one-line request reserved 16584
#: tokens and was refused. 8k is far more than any of these structured outputs needs
#: (a batch of translated lines or extracted items is well under that) while leaving
#: ~4k of the minute's budget for the prompt. A caller asking for less still gets less.
#:
#: This bounds a *single* request; a long meeting is still TPM-bound across requests on
#: the free tier. Enabling Groq's (free) Dev tier at console.groq.com/settings/billing
#: raises the limit ~25x and is the real fix for sustained throughput.
_MAX_COMPLETION_TOKENS = 8_000


class GroqProvider:
    """Structured completions via Groq's OpenAI-compatible chat API."""

    def __init__(self, api_key: str) -> None:
        self._client = Groq(api_key=api_key)

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        schema: type[ModelT],
        model: str,
        effort: Effort = "high",  # noqa: ARG002 - no reasoning-effort knob on Groq
        max_tokens: int = 16_000,
    ) -> ModelT | None:
        """Run one structured completion. See ``StructuredLLM``."""
        # The schema goes in the system message (Groq has no cross-model schema
        # mode) and json mode guarantees a valid-JSON wrapper. The literal word
        # "JSON" must appear somewhere for json_object mode to be accepted.
        system_with_schema = (
            f"{system}\n\n"
            "Respond with a single JSON object and nothing else — no prose, no "
            "markdown code fences. It must conform exactly to this JSON schema:\n"
            f"{json.dumps(schema.model_json_schema())}"
        )

        response = self._client.chat.completions.create(
            model=model,
            max_tokens=min(max_tokens, _MAX_COMPLETION_TOKENS),
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_with_schema},
                {"role": "user", "content": prompt},
            ],
        )

        content = response.choices[0].message.content
        if not content:
            logger.warning("Groq returned an empty response.")
            return None

        try:
            return schema.model_validate_json(_strip_fences(content))
        except ValidationError as exc:
            logger.warning("Groq output did not match %s: %s", schema.__name__, exc)
            return None


def _strip_fences(text: str) -> str:
    """Drop a ```json … ``` fence if a model added one despite JSON mode.

    JSON mode should return raw JSON, but models occasionally wrap it anyway, and a
    stray fence would fail validation for no real reason. Cheap to guard against.
    """
    t = text.strip()
    if not t.startswith("```"):
        return t
    # Drop the opening fence line (``` or ```json), then a trailing fence if present.
    t = t[t.find("\n") + 1 :] if "\n" in t else t[3:]
    t = t.rstrip()
    if t.endswith("```"):
        t = t[:-3]
    return t.strip()
