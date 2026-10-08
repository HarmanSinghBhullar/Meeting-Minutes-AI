# Meeting Intelligence

A browser extension that produces **accurate, verifiable meeting minutes**.

Everything in the architecture follows from that sentence. The minutes are the
product; the transcript, the speaker labels, and the RAG index exist to serve
them. By the time text reaches a summarizer the accuracy ceiling is already set —
a model handed a transcript that says *"yeah so Deepak will own the thing by
Friday"*, with the speaker mislabelled, cannot produce correct minutes no matter
how good it is. So the work goes upstream, and the LLM is the last place we
invest.

## The four decisions that matter

**Two audio tracks, not one.** `chrome.tabCapture` gives us the remote
participants; the microphone gives us the local user. Rather than mixing them, we
record them separately. The mic track is the local speaker *by definition* and
the tab track is everyone else *by definition* — a perfectly clean speaker
boundary, obtained with no model and no inference error.

**Speakers are diarized from audio, then named by a human — once.** This one was
built the other way first, and the reversal is the most instructive decision in
the project. Meet, Zoom, and Teams all highlight whoever is talking, so a content
script scraped that and got a speaker timeline with real names in it, free, with
no model. It worked. It was also the wrong design, and the reason is *how it
failed*: the highlight is a per-frame CSS class with an obfuscated name, and when
Google reskinned Meet the scrape did not throw — it simply stopped matching, and
every remote speaker came out "Unknown". Silently. The failure surfaced days later
as a set of minutes with no owners, and the fix was a reverse-engineering session
against a live call.

So attribution is now `pyannote.audio` on the tab track. It costs GPU minutes, and
it produces anonymous clusters that a person has to name — a real, visible price,
paid on every meeting. It buys a signal that reads the audio, which no CSS change
can break, and whose failure mode is *asking a question* rather than quietly
inventing an answer. **A predictable manual step beats an unpredictable silent
failure**, especially on the axis users check first.

The roster survives, because it never had that problem: the participant list is
read once, from the meeting UI's own list, not sampled per frame. It is no longer
the timeline — it is the *name source*, the candidate list you map clusters onto,
and the speaker-count bound handed to pyannote. And the two-track split does real
work here: the mic track is the local user by definition, so it is never diarized,
never a cluster, and never something you are asked to identify.

**Every minutes item cites its transcript segments, and is verified against
them.** A second pass re-reads each extracted item against *only* the lines it
claims to come from, and items that are not supported get flagged rather than
shipped. The worst thing this product can do is fabricate a commitment with a
real person's name on it: nobody catches it, someone acts on it, and the tool is
finished. Citations also mean a reader can check any claim in one click, and
minutes you cannot check are minutes you eventually stop trusting.

**Accuracy is measured, not asserted.** See [`eval/`](eval/README.md). Without a
gold set, every change is a coin flip you cannot evaluate.

## Documentation

This page is the argument. [`docs/`](docs/README.md) is the reference:
[architecture](docs/architecture.md), [data model](docs/data-model.md),
[API](docs/api-reference.md), [pipeline](docs/pipeline.md),
[extension](docs/extension.md), [configuration](docs/configuration.md),
[operations](docs/operations.md), and [development](docs/development.md).

## Stack

| Layer | Choice | Why |
|---|---|---|
| Extension | React, TypeScript (strict), Vite, MV3 | Capture must live in an offscreen document — MV3 service workers have no DOM and cannot hold a `MediaRecorder` |
| API | FastAPI, SQLAlchemy 2.0, PostgreSQL | Segments are the spine; every feature reads them |
| Queue | Postgres `FOR UPDATE SKIP LOCKED` | Transcription takes minutes, so it cannot run in a request. A broker would buy throughput we don't need — the GPU is the bottleneck |
| Transcription | `faster-whisper` `large-v3`, int8_float16 | The big model earns its keep on exactly the words minutes are made of: names, products, acronyms |
| Diarization | `pyannote.audio` `speaker-diarization-3.1` | The only attribution source. Anonymous clusters, named once by a human — a scrape of the meeting UI was free and faster, but broke silently on every reskin |
| Minutes | Groq (`llama-3.3-70b-versatile`), structured output + grounding pass | Provider is a one-line switch (`LLM_PROVIDER`); Google, OpenAI, and Anthropic are installed and A/B-testable on the eval set |
| RAG | ChromaDB + grounded LLM | Searches cited transcript windows after grounding |

## Running it

Requires **PostgreSQL**, **ffmpeg** on `PATH` (a system dependency — this is the
usual cause of a fresh install failing immediately), and an NVIDIA GPU.

```bash
# Backend
cd backend
python -m venv venv
venv/Scripts/python -m pip install -e ".[dev,diarization]"   # Windows; use venv/bin on POSIX

createdb meeting_minutes                            # or: psql -U postgres -c "CREATE DATABASE meeting_minutes;"
cp ../.env.example .env                             # then set DATABASE_URL
venv/Scripts/python -m alembic upgrade head

venv/Scripts/python -m uvicorn app.main:app --reload

# Worker — a separate process, so a job pinning the GPU for several minutes
# cannot starve the API the extension is still uploading chunks to.
venv/Scripts/python -m app.workers.run_worker

# Extension
cd extension
npm install
npm run build                 # then load extension/dist unpacked at chrome://extensions
```

Check the pipeline without the extension — it pushes two audio files through
ffmpeg, Whisper, alignment, and Postgres, and prints the transcript:

```bash
venv/Scripts/python scripts/smoke_transcribe.py <mic.wav> <tab.wav>
```

### pyannote setup (required)

The `diarization` extra is not optional for the **worker** — it is the only
attribution source, and without it every transcribe job fails at the attribution
stage. It is separate from the base install because it pulls torch, which the API
process has no use for.

**On Windows, install torch from PyTorch's own index, not PyPI.** The default
PyPI wheel is CPU-only, and it does not fail — pyannote quietly falls back to CPU
and diarizes several times slower, with only a log line to say so:

```bash
venv/Scripts/python -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu126
venv/Scripts/python -c "import torch; print(torch.cuda.is_available())"   # must print True
```

Pick the `cuXXX` index matching your driver; `cu126` is verified here on an
RTX 3050 (driver 592). The version bounds in `pyproject.toml` (`torch<2.9`,
`torchaudio<2.9`, `pyannote.audio<4`) are not cosmetic — each one marks a
combination that breaks at import. Read the comments there before loosening them.

The models are also **gated**, and this is the step everyone loses an hour to.
Accept the licence on **both** pages — `pyannote/segmentation-3.0` *and*
`pyannote/speaker-diarization-3.1` — then put a read token in
`HUGGINGFACE_TOKEN`. The diarization pipeline loads segmentation internally, so
accepting only the pipeline's own page fails with a 401 that reads like a network
error.

### GPU note

On a 4GB card, Whisper and pyannote will not fit at the same time. The worker
therefore holds only one model at a time and unloads between stages — Whisper is
gone before diarization loads, so `PYANNOTE_DEVICE` can safely default to `cuda`.
Leave it there unless you have no GPU: pyannote defaults to CPU on its own, where
an hour of audio takes tens of minutes instead of a couple.

**Windows CUDA.** CTranslate2 links against cuBLAS and cuDNN but ships neither,
and Windows has no system CUDA toolkit to fall back on. The `nvidia-cublas-cu12`
and `nvidia-cudnn-cu12` wheels are therefore hard dependencies on `win32`, and
`app/core/cuda.py` puts them on the DLL search path at import. If you ever see
`RuntimeError: Library cublas64_12.dll is not found`, that module is the place to
look — note that `os.add_dll_directory` alone does *not* fix it, because
CTranslate2 resolves cuBLAS with a plain `LoadLibrary` that only searches `PATH`.

## Status

**Working end to end:** audio → ffmpeg → `large-v3` on the GPU → pyannote
diarization → word-level alignment → attributed segments in Postgres →
translation for non-English meetings → **speaker mapping** → point-wise cited
minutes → grounding pass.

- Minutes — `backend/app/services/minutes/` (extraction + grounding, with OpenAI
  and Anthropic providers behind a config switch).
- Translation — `backend/app/services/translation.py`. A non-English transcript
  is transcribed in the language spoken, then translated line-by-line through the
  same structured-LLM provider as the minutes, which handles names, jargon, and
  code-switching better than Whisper's own translate task. The English rendering
  is written to `Segment.text_en` alongside the original `text` (never over it),
  and the minutes are written from the English text.

**Speaker mapping, and the gate in front of the minutes.** Diarization separates
voices but cannot name them, so a meeting stops after transcription and asks. The
gate is deliberate: minutes crediting `SPEAKER_01` are not a rough draft of the
right answer, they are a confident wrong one, and this product's entire pitch is
that you can trust what it hands you. The meeting page shows each unnamed voice
with its **longest** lines — a call opens with "hi" and "can you hear me", which
identify nobody, whereas "I'll take the ChromaDB migration" identifies a colleague
instantly — and you map it to a participant, type a name for a phone joiner the
roster missed, or mark it *not a person* (a shared video, hold music: its words
stay in the transcript, but are withheld from the minutes). Naming the last voice
is what queues the minutes; there is no separate confirm button to forget.
Mechanically the gate is just `Speaker.source == 'diarization'` — an unnamed
cluster row *is* the unmapped state, so there is no status column that can drift
out of step with it.

The Meet adapter still **identifies the local user** (via `data-self-name` or
Meet's "(You)" marker), so their microphone track carries their real name (tagged
`Name (You)`) rather than an invented "You" — and, since the mic track is never
diarized, they are never one of the voices you are asked to name. The adapter's
active-speaker and presenter events are still recorded, but they no longer drive
attribution; they are kept as evaluation data to score the diarizer against. A
rotted `SELECTORS.speaking` is therefore now a stale-eval problem, not a
wrong-minutes problem — which was the entire point of the change.

**Reviewing and managing meetings.** The extension's meeting page doubles as a
dashboard: opened with no `?id=` it lists every recording with its status, and
each row can be opened, **renamed**, **deleted** (row + audio), have its
**speakers mapped**, or its **minutes regenerated**. A single meeting shows its
point-wise minutes under a `Minutes` heading, attendees under `Attendance`, and
every claim one click from the transcript line behind it.

The extension's upload path has been exercised against the API on a live
recording, and the transcript-only path is still verifiable in isolation with
`scripts/smoke_transcribe.py`.

**RAG:** after grounding completes, the worker indexes overlapping transcript
windows in Chroma. `POST /api/v1/qa` retrieves those windows and returns only an
answer carrying citations to the meeting timestamp(s) that support it. Install
the optional dependencies with `pip install -e ".[rag]"`.

## Recording consent

Recording a meeting without telling the participants is illegal in two-party-
consent jurisdictions. The visible `REC` badge and the in-popup notice are
product requirements, not disclaimers, and should not be quietly removed.
