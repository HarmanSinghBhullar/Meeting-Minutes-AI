# Evaluation set

Build this early. It is an afternoon of work and it is the highest-leverage thing
in the repository, because it is the only reason to believe any claim about
accuracy — including the claims made in the code comments.

## What goes in it

Three to five real meetings. Not twenty: twenty is a research programme, five is
an afternoon, and five is enough to tell a real improvement from a lucky one.
Pick meetings that are hard in the ways your actual meetings are hard — accents,
crosstalk, a bad laptop microphone, product names nobody outside the team has
heard of.

For each one, in `datasets/<meeting-name>/`:

| File | What it is |
|---|---|
| `mic.webm`, `tab.webm` | The raw recordings, exactly as the extension uploaded them |
| `transcript.gold.json` | Hand-corrected transcript, with speaker names and timings |
| `keywords.txt` | The names, products, and acronyms this meeting turns on |
| `minutes.gold.md` | The minutes you *wish* the tool had produced |

`datasets/` is gitignored — it contains real meeting audio, and that is not
something to push to a remote.

## Why `keywords.txt` matters more than it looks

Word error rate averages over every word, and most words are "the" and "and".
A model can score well on WER while mangling every proper noun in the meeting,
and a transcript that mangles proper nouns is worthless as minutes: the whole
content of a decision is *who* committed to *what thing*.

So list the words this meeting actually turns on, and track recall over that list
as the headline number. It is the metric that correlates with whether the minutes
are any good.

## What to measure

Run the pipeline over the set and compare against gold:

1. **WER** and **keyword recall** — did we hear it.
2. **Speaker attribution accuracy** — did we get the name right. Report the DOM
   path and the pyannote fallback separately; they should look very different,
   and if they do not, one of them is broken.
3. **Action-item precision and recall** — did the minutes say what the meeting
   said. Precision is the one to defend: a missed item costs a manual note, a
   fabricated item gets acted on.
4. **Latency** — wall-clock from finalize to minutes, on the actual GPU. This is
   what decides whether the product feels alive.
