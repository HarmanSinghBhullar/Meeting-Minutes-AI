"""Minutes accuracy — deliberately not built yet.

The other metrics in this package are arithmetic: given gold and a hypothesis, WER
has one right answer and you can unit-test it. "Did the minutes say what the
meeting said" has no such answer. Gold says *"Priya owns the ChromaDB migration,
due Friday"* and the tool says *"Priya to complete the Chroma migration by 22
May"*. Same item. No string comparison agrees.

So matching items is a judgement, and the only thing available to make it at scale
is an LLM — which means a nondeterministic scorer grading a nondeterministic
system. That is not a reason to refuse. It is a reason to build it deliberately,
with an argument, rather than reaching for the obvious thing and trusting the
number that falls out. Notes toward that argument:

**The judge must not be the extractor.** Same provider, same prompt family, same
blind spots: it will forgive exactly the errors it would itself make, and the
metric reads highest precisely where the system is most consistently wrong. Pin the
judge to its own model and its own setting (``JUDGE_MODEL``), and hold it there
across a comparison — the point is comparing pipeline versions, and a metric whose
ruler changes underneath it compares nothing. Note ``get_provider()`` is
``lru_cache``d; sweeping providers means ``get_provider.cache_clear()``.

**The judge decides matching, never truth.** Give it one gold item and one
extracted item and ask a single question: are these the same commitment? Do not
ask it to read the transcript and decide whether an item is real — that is the
grounding pass's job (``services/minutes/grounding.py``), it already exists, it
already fails closed, and a second opinion here would just be an unaccountable
third model with an opinion. Matching is a narrow question with a defensible
answer. Truth is not.

**Precision is the number to defend.** A missed action item costs someone one
manual note. A fabricated one gets acted on. When the two trade off — and a
matching threshold makes them trade off — the harness should be the thing that
notices, which means never averaging them into an F1 and reporting that instead.

**Calibrate before trusting.** Hand-label ~30 (gold, extracted) pairs, score the
judge against them, and report *its* agreement rate. A judge that is 80% accurate
grading a system that is 80% accurate produces a number with no useful precision,
and you will not discover that from the number itself. If the judge cannot be shown
to be much better than the thing it grades, this file should stay unimplemented and
the honest metric is a human reading five sets of minutes.

**Grounding rejection rate is free, and worth taking first.** It needs no judge at
all: ``run_ground`` already persists ``MinutesItem.is_grounded`` and
``grounding_note``, so it is one query over the latest ``Minutes.version``. Note
``is_grounded IS NULL`` means "not yet run" and must not be counted as accepted.
Zero rejections means the grounding pass should be distrusted, not celebrated —
and ``scripts/check_grounding.py`` is the adversarial check that tells you which.

**The gate is in the way, and that is correct.** ``run_minutes`` raises while any
cluster is unmapped, so an unattended run cannot reach minutes at all. The two ways
through are a human mapping every cluster (which is the design, and is not
automatable by definition) or auto-mapping from gold (which measures a pipeline no
user will ever run). Probably: a small ``mapping.json`` per dataset, recording what
a human *did* map, once, by hand — replayable, faithful, and honest about being a
recording rather than a simulation.
"""

from __future__ import annotations

from eval.metrics import MinutesMetrics


def score_minutes(gold_minutes: str, extracted: object) -> MinutesMetrics:
    """Score extracted minutes against hand-written gold minutes.

    Unimplemented. Read the module docstring before starting: the design decisions
    are the hard part here, not the code.
    """
    raise NotImplementedError(
        "Minutes scoring needs an LLM judge — see eval/judge.py for the design "
        "and eval/README.md for why it is second."
    )
