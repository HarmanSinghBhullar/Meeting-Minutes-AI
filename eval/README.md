# Evaluation set

The only reason to believe any claim about accuracy — including the claims made in
the code comments, and including the one on the front page of this repository that
says accuracy is measured rather than asserted.

The harness is built. **The gold set is not**, and until it is, nothing here has
measured anything. That is the honest status: this directory currently contains a
working ruler and nothing to measure.

```bash
# From the repository root.
backend/venv/Scripts/python -m eval.runner                    # transcribe and score every dataset
backend/venv/Scripts/python -m eval.runner --score-only       # score existing hypotheses, no GPU
backend/venv/Scripts/python -m eval.runner --dataset kickoff  # just one

backend/venv/Scripts/python -m pytest eval                    # the harness's own tests
```

## What goes in it

Three to five real meetings. Not twenty: twenty is a research programme, five is an
afternoon, and five is enough to tell a real improvement from a lucky one. Pick
meetings that are hard in the ways your actual meetings are hard — accents,
crosstalk, a bad laptop microphone, product names nobody outside the team has heard
of.

For each one, in `datasets/<meeting-name>/`:

| File | Required | What it is |
|---|---|---|
| `mic.webm`, `tab.webm` | to transcribe | The raw recordings, exactly as the extension uploaded them |
| `transcript.gold.json` | yes | Hand-corrected transcript: who said what, in order |
| `keywords.txt` | for the headline metric | The names, products, and acronyms this meeting turns on |
| `agenda.txt` | no | What was really in the invite. **Not** `keywords.txt` — see below |
| `minutes.gold.md` | not yet used | The minutes you *wish* the tool had produced |
| `transcript.hyp.json` | generated | What the pipeline produced. Written by the runner; overwritten each run |

`datasets/` is gitignored — it contains real meeting audio, and that is not
something to push to a remote. The one exception is [`datasets/example/`](datasets/example/README.md),
which is synthetic, has no audio, and exists so the scorer can prove itself on a
laptop with no GPU.

## The format

Gold and hypothesis are **the same shape**, because a hypothesis is a transcript the
machine wrote and gold is one a human corrected. Full schema and reasoning in
[`transcript.py`](transcript.py).

```json
{
  "title": "Weekly sync",
  "date": "2026-05-14",
  "language": "en",
  "local_user": "Harman",
  "segments": [
    {"start_ms": 1200, "end_ms": 4800, "speaker": "Priya", "text": "I'll take the migration."}
  ]
}
```

Times are integer **milliseconds** everywhere, including in gold — the pipeline's own
seconds-vs-milliseconds seam buys nothing here, and the loader rejects floats
because that mistake would otherwise pass silently. There are **no word-level
timings**: every metric aligns word *sequences*, so a word inherits its segment's
speaker and the timings never enter the arithmetic. Requiring them would have made
hand-correction impossible and bought nothing.

**Correct a hypothesis rather than typing from silence.** Run the pipeline, rename
`transcript.hyp.json` to `transcript.gold.json`, and fix it against the audio.
An afternoon instead of a week. It biases gold toward the machine's own mistakes,
so you must actually listen rather than skim — but the alternative is no gold set,
which is where this project has been since the beginning.

## Why `keywords.txt` matters more than it looks

Word error rate averages over every word, and most words are "the" and "and". A
model can score well on WER while mangling every proper noun in the meeting, and a
transcript that mangles proper nouns is worthless as minutes: the whole content of
a decision is *who* committed to *what thing*.

So list the words this meeting actually turns on, and track recall over that list as
the headline number. It is the metric that correlates with whether the minutes are
any good.

**`keywords.txt` is never fed to the model.** `agenda.txt` is — it primes Whisper's
decoder, and a real meeting has an agenda, so a run without one under-measures the
real product. But priming the decoder with the exact rare words we then score recall
on would raise the number and prove nothing. Two files, two jobs, and they must stay
apart. `agenda.txt` holds what was really in the invite; nothing more.

## What is measured

| Column | Metric | Read it as |
|---|---|---|
| `WER` | Word error rate | Lower is better. Not a percentage, and uncapped — above 1.0 means hallucination |
| `keyword` | Keyword recall | The headline number |
| `attrib` | Word-level attribution accuracy | Did the diarizer separate the voices |
| `named` | Named speaker rate | Did a real name actually reach the minutes |
| `voices` | Clusters found / real people | Over-clustering, which costs `attrib` nothing and costs a human time |

`attrib` and `named` are two questions, and reading either alone will mislead you.

The pipeline does not emit "Priya". It emits `SPEAKER_00` and waits for a human (the
mapping gate). So scoring maps each cluster onto the gold speaker it most co-occurs
with, and `attrib` is computed after that. This is not charity: a cluster is a
*question*, not a wrong answer, and `POST /speakers/{id}/resolve` is the human
answering it. Scoring after the mapping measures what the system is responsible for
— did it separate the voices — rather than re-measuring the gate.

But that mapping assumes perfect naming by construction, so `attrib` can never be
evidence that the product names people correctly. `named` is the number that keeps
it honest, and it is computed on the raw output before any mapping. Full argument in
[`scoring.py`](scoring.py).

## What is not measured yet

**Minutes accuracy.** Action-item and decision precision/recall need an LLM judge —
a nondeterministic scorer grading a nondeterministic system — and that needs an
argument before it needs code. The argument is in [`judge.py`](judge.py). Precision
is the one to defend when it lands: a missed item costs a manual note, a fabricated
item gets acted on.

**Grounding rejection rate** is the cheap half and needs no judge: `run_ground`
already persists `MinutesItem.is_grounded`, so it is one query. `is_grounded IS
NULL` means "not yet run" and must not count as accepted. Zero rejections means the
grounding pass should be distrusted, not celebrated —
[`scripts/check_grounding.py`](../backend/scripts/check_grounding.py) is the
adversarial check that tells you which.

**Latency** — wall-clock from finalize to minutes, on the actual GPU. This is what
decides whether the product feels alive, and the runner does not time anything yet.

## How it is put together

| File | Needs | What |
|---|---|---|
| `transcript.py` | stdlib | The format, loading, strict validation |
| `metrics.py` | stdlib | WER, keyword recall, the Levenshtein alignment both lean on |
| `scoring.py` | stdlib | Gold × hypothesis → numbers. The cluster-mapping argument |
| `runner.py` | GPU, Postgres, ffmpeg | Drives the real pipeline. The only file that imports `app` |
| `judge.py` | — | Minutes scoring: design notes, no implementation |

Everything except `runner.py` is stdlib-pure and imports no `app`. That is load-bearing,
not tidiness: `app.workers.pipeline` pulls in torch and a Whisper backend at module
scope, and a scorer that needs a GPU to compare two strings is a scorer nobody runs.
`runner.py` keeps its `app` imports inside the function that transcribes, so
`--score-only` never touches them — and a test asserts `torch` stays out of
`sys.modules` to keep it that way.

## A note on the numbers you will get first

They will be bad, and the first instinct will be to tune something. Resist it long
enough to look at *where* the errors are. They are usually concentrated somewhere
unglamorous — one participant's microphone, crosstalk in the first two minutes, a
product name nobody outside the team has heard — and the fix is usually not the
model.
