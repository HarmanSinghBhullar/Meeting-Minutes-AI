# Architecture

## Three processes, and why

```
┌─────────────────────────────┐
│  Chrome (MV3 extension)     │
│  ┌───────────────────────┐  │
│  │ popup  (React)        │  │  start/stop, recent meetings
│  ├───────────────────────┤  │
│  │ service worker        │  │  orchestrates; holds no DOM
│  ├───────────────────────┤  │
│  │ offscreen document    │  │  tabCapture + mic, 2x MediaRecorder
│  ├───────────────────────┤  │
│  │ content script        │  │  roster, self-name, speaker events
│  ├───────────────────────┤  │
│  │ meeting page (React)  │  │  dashboard, mapping, minutes, transcript
│  └───────────────────────┘  │
└──────────────┬──────────────┘
               │  HTTP  (127.0.0.1:8000/api/v1)
┌──────────────▼──────────────┐        ┌────────────────────────────┐
│  FastAPI  (uvicorn)         │        │  Worker (separate process) │
│  writes rows, enqueues jobs │        │  claims jobs, owns the GPU │
│  never touches the GPU      │        │  ffmpeg / Whisper / pyannote│
└──────────────┬──────────────┘        └─────────────┬──────────────┘
               │                                      │
               └──────────────┬───────────────────────┘
                              ▼
                   ┌─────────────────────┐   ┌──────────────────┐
                   │  PostgreSQL         │   │  storage/<id>/   │
                   │  rows + job queue   │   │  audio blobs     │
                   └─────────────────────┘   └──────────────────┘
```

**The API and the worker are separate processes.** Transcription pins the GPU for
minutes at a time. If it ran in a request handler it would starve the API that
the extension is *still uploading chunks to* — the recording is live while the
previous meeting is being transcribed. The split is not about scale; it is about
one job not being able to block the other.

**The API never imports torch.** This is enforced by where code lives, not by
convention alone: `unmapped_cluster_count` sits in
[`services/attribution/mapping.py`](../backend/app/services/attribution/mapping.py)
rather than in `workers/pipeline.py`, precisely so the speakers and minutes
routes can import it without dragging faster-whisper — and through it, torch —
into the API process at startup.

**Postgres is the job queue.** `SELECT … FOR UPDATE SKIP LOCKED` (see
[Pipeline](pipeline.md#the-queue)). A broker would buy throughput we do not need:
the GPU is the bottleneck and does one thing at a time. It would also add a
second thing to install and a second place for state to be wrong.

**Audio lives on disk, paths live in Postgres.** Hundred-megabyte blobs in the
database would bloat every backup and slow every meetings query. The storage
interface ([`services/audio/storage.py`](../backend/app/services/audio/storage.py))
is deliberately narrow — `track_path`, `normalized_path`, `append_chunk`,
`delete_meeting`, `size_bytes` — so S3 could replace it without touching a
caller.

## The life of a meeting

1. **Open.** The user clicks *Start* in the popup. The service worker asks the
   content script for the meeting context (title, platform, participant roster,
   which tile is the local user), then `POST /recordings/meetings` — creating the
   `Meeting`, one `Speaker` per roster entry, and two `Recording` rows (mic, tab).
   The roster is posted *before* any audio, because the names prime Whisper.

2. **Record.** The offscreen document captures two streams — `chrome.tabCapture`
   for the remote participants, `getUserMedia` for the microphone — and runs a
   `MediaRecorder` on each, at a 5-second timeslice. Every chunk is `POST`ed to
   `/chunks` and appended to a `.webm` on disk. They are never mixed.

3. **Finalize.** On stop, the offscreen recorder drains its in-flight uploads, the
   content script hands over its buffered speaker events, and
   `POST /finalize` sets `ended_at` and enqueues a `TRANSCRIBE` job.

4. **Transcribe.** The worker claims the job and runs, in one process:
   ffmpeg normalize → Whisper `large-v3` on both tracks → pyannote diarization on
   the tab track → word-level alignment → segments written to Postgres.

5. **Translate** (only if the detected language is not English). Line-by-line
   through the same structured-LLM provider as the minutes, writing
   `Segment.text_en` and never overwriting `Segment.text`.

6. **Wait for a human.** If diarization produced unnamed clusters, the meeting
   stops here. See [the mapping gate](#the-mapping-gate) below.

7. **Minutes.** Extraction (chunked, map-reduce, every item citing its segments)
   then a separate grounding pass that re-reads each item against *only* its
   cited lines.

Stages 4–7 are ordered by data dependency, not by preference. `normalize`,
`attribute`, and `align` are function calls *inside* the transcribe job rather
than jobs of their own, because they share the audio and the loaded models —
splitting them would mean reloading a 2GB model to save nothing.

## The four boundaries that carry weight

### Two tracks, not one

The mic track is the local user **by definition**. The tab track is everyone else
**by definition**. That is a perfectly clean speaker boundary obtained with no
model and no inference error, and it is why `_attribute` can hand the mic track
to `align()` with `turns=[]` and a `default_speaker` — there is nothing to infer.

It also means the local user is never diarized, never a cluster, and never
someone you are asked to identify.

### Diarization, then a human — once

Attribution is `pyannote/speaker-diarization-3.1` on the tab track, and nothing
else. The DOM active-speaker scrape that used to do this job is **deleted**
(`services/attribution/dom_timeline.py`). It was free and fast and it worked —
until Google reskinned Meet, at which point it did not throw. It silently stopped
matching, and every remote speaker came out "Unknown". That surfaced days later
as a set of minutes with no owners.

The trade is explicit: GPU minutes plus a manual naming step, in exchange for a
signal that reads the audio and whose failure mode is *asking a question* rather
than quietly inventing an answer.

The roster survives because it never had that problem — it is read once from the
meeting UI's own participant list, not sampled per frame. It is no longer the
timeline. It is the *name source*: the candidate list you map clusters onto, and
the `max_speakers` bound handed to pyannote.

### The mapping gate

A diarized meeting stops after transcription and asks who was speaking. Minutes
crediting `SPEAKER_01` are not a rough draft of the right answer — they are a
confident wrong one.

Mechanically the gate is just `Speaker.source == DIARIZATION`. **An unnamed
cluster row *is* the unmapped state.** There is no `needs_mapping` column,
deliberately: it would be a second copy of a fact the first copy already tells
you, free to drift, and needing a backfill to exist at all.

It is enforced at three depths:

| Where | Behaviour |
|---|---|
| `_enqueue_minutes_unless_unmapped` (worker) | Holds. Logs and returns — a *success*, not a failure. Raising would light the meeting red and invite a retry, when what is needed is a person. |
| `POST /minutes/regenerate` (API) | **409**, with the count in the message. |
| `run_minutes` (worker) | **Raises.** Reaching here means something bypassed the API — and a guarantee enforced only at the edge is not enforced. |

Resolving the last cluster is what queues the minutes. There is no separate
confirm button to forget.

### Every item cites, and is checked against, its own lines

`cites` is a **required** field on the extraction schema, so an uncited item is
inexpressible rather than merely discouraged. Then a second pass re-reads each
item against *only* the lines it claims to come from — in isolation, by a call
that has not seen the rest of the meeting.

The isolation is the point. A verifier holding the whole transcript would confirm
"Priya owns the migration" because she volunteered forty lines later — making the
claim true and the citation wrong. **We are checking the citation, not the
claim.**

Items that fail are flagged, not deleted, and the UI shows them struck with a
warning. The worst thing this product can do is fabricate a commitment with a
real person's name on it: nobody catches it, someone acts on it, and the tool is
finished.

## GPU discipline

On a 4GB card, Whisper and pyannote do not fit at the same time. The worker holds
**one model at a time** and unloads between stages — Whisper is gone before
diarization loads. The 10–30s of model load on a job measured in minutes is not
worth optimizing away.

Diarization runs in a **child process**
([`attribution/diarize_cli.py`](../backend/app/services/attribution/diarize_cli.py)),
and this is not a style choice. CTranslate2 and torch both link cuDNN and cannot
both initialise it in one process: once CTranslate2 has touched the GPU, torch's
cuDNN load dies with `Could not load symbol cudnnGetLibConfig. Error code 127` and
takes the interpreter with it — exit 127, no traceback, nothing to catch.
Reproducible with byte-identical `cudnn64_9.dll` copies, so it is not a version
conflict. Only an OS process boundary separates two CUDA runtimes. It also means
the GPU is released by the OS rather than by someone remembering to call
`unload()`.

**Do not "simplify" the subprocess away, and do not import torch in
`workers/pipeline.py`.**

## Where things live

```
backend/
  app/
    api/v1/routes/     recordings, meetings, speakers, minutes, qa
    core/              config (env), cuda (Windows DLL search path)
    db/models/         meeting, recording, segment, speaker, minutes, job, enums
    schemas/           pydantic wire types
    services/
      audio/           ffmpeg normalize, on-disk storage
      transcription/   faster-whisper provider behind a Protocol
      attribution/     pyannote provider, the diarize subprocess, the gate helper
      alignment.py     words + turns -> attributed segments
      translation.py   non-English -> Segment.text_en
      minutes/         extractor, grounding, provider-agnostic structured LLM
      rag/             Chroma indexing and grounded Q&A
    workers/           the queue, the pipeline stages, the worker loop
  alembic/             migrations
  scripts/             smoke tests and an adversarial grounding check
  tests/               pytest, against a real Postgres
extension/
  src/
    background/        service worker: orchestration + badge
    offscreen/         the recorder (MV3 workers have no DOM)
    content/           adapters: roster, self-name, speaker events
    popup/             start/stop
    meeting/           dashboard, meeting page, speaker mapping
    lib/               api client, shared types
eval/                  the gold set and its metrics
docs/                  you are here
```

## RAG

[`services/rag/`](../backend/app/services/rag/) indexes overlapping, attributed
transcript windows after the grounding job succeeds. Chroma is a derived cache:
each meeting index is replaced atomically on re-index and `reindex_all` rebuilds
the entire collection from PostgreSQL. Retrieval gives the configured structured
LLM only the top matching windows and requires it to return evidence-window
numbers. An answer without valid citations is withheld rather than presented as
a confident guess.

The meeting page's **Ask this meeting** panel supplies its meeting id with every
question, so a response can only cite that call's transcript. Citations remain
visible in the conversation and link back to the transcript section.
