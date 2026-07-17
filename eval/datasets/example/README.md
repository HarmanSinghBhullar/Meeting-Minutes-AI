# `example` — a synthetic dataset

**This is not a real meeting and it does not measure anything.** It is five
invented lines, and its scores are meaningless as evidence about the pipeline.

It exists for two reasons, and it is committed — unlike every other dataset here,
which is gitignored because it contains real meeting audio — because neither reason
involves real audio:

1. **The format is executable rather than described.** `eval/README.md` says what
   `transcript.gold.json` looks like; this directory *is* one, and if the loader
   and the docs ever disagree, the test suite says so.
2. **The scorer can prove itself with no GPU.** `python -m eval.runner
   --score-only` scores this in milliseconds on a laptop with no Postgres, no
   CUDA, and no `[diarization]` extra. `test_example_dataset.py` pins the numbers,
   so a change that quietly breaks scoring fails a test instead of silently
   reporting a worse WER on your real meetings next month.

There is no `mic.*` or `tab.*` here, so it can only be scored, never transcribed.
`transcript.hyp.json` is hand-written to stand in for what the pipeline would have
produced.

## What it is built to exercise

Every number in the report is non-trivial here on purpose — a fixture where
everything scores 1.0 tests nothing:

| Planted | Where | Shows up as |
|---|---|---|
| `ChromaDB` heard as "chroma DB" | segment 1 | a keyword miss *and* WER — one word became two |
| `Kuberya` heard as "Cooperia" | segment 3 | a keyword miss; the dashboard is still discussed, unfindably |
| Priya and Deepak left as `SPEAKER_00` / `SPEAKER_01` | segments 2–4 | `named` well below 1.0 — the mapping gate, unanswered |
| Harman named on both his lines | segments 1, 5 | the mic track, which is never diarized and never needs mapping |
| Priya's "blocked on the API keys" given to Deepak's cluster | segment 4 | `attrib` below 1.0 — a real attribution error, surviving the cluster mapping |

That last row is the one worth understanding. Scoring maps each cluster onto the
gold speaker it most co-occurs with, so `SPEAKER_01` becomes Deepak — he out-talks
Priya on that cluster twelve matched words to six. Segment 4 is then wrong, and
stays wrong, which is exactly right: the mapping is meant to forgive *naming*, not
to launder a voice that got put in the wrong mouth.
