"""Structured-LLM provider interface.

The minutes pipeline needs exactly one thing from a language model: given a
system instruction and a prompt, return an object matching a Pydantic schema —
or nothing at all. That is the whole contract, and it is deliberately narrow.

Everything that makes the minutes trustworthy — the required ``cites`` field, the
owner check against the roster, the isolation of the grounding pass, the chunking
and dedup — lives *above* this interface and is provider-agnostic. Swapping
Anthropic for OpenAI changes which HTTP call gets made and nothing else.

That is also the point of writing it this way rather than reaching for the SDK
directly: it means the evaluation set can score providers against each other
later on, instead of us picking one on vibes and never revisiting it.
"""

from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)

#: How hard the model should think. Both providers expose a reasoning-effort
#: knob; the names happen to line up, so this maps straight through.
Effort = Literal["low", "medium", "high"]


class StructuredLLM(Protocol):
    """Returns a validated object of a given shape, or None."""

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
        """Run one structured completion.

        Args:
            system: The instruction that frames the task.
            prompt: The content to work on.
            schema: A Pydantic model. The provider must enforce it server-side —
                not parse it hopefully out of prose — so that a malformed
                response is impossible rather than merely unlikely.
            model: The provider's model id.
            effort: Reasoning depth.
            max_tokens: Output ceiling.

        Returns:
            A validated instance of ``schema``, or None if the model declined or
            returned nothing usable. Callers must treat None as "no answer",
            never as "approved" — the grounding pass depends on failing closed.
        """
        ...
