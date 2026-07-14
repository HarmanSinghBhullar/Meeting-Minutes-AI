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

**Speaker names are read from the meeting UI, not inferred.** Meet, Zoom, and
Teams all display a participant list with real names and highlight whoever is
talking. A content script watches that and records a speaker timeline with actual
human names in it. Diarization can only produce anonymous clusters that someone
then has to label, so pyannote is our *fallback* — for conference-room audio and
unsupported platforms — rather than the main path. "Priya owns the migration" is
worth far more than "someone owns the migration", and attribution errors are the
ones users notice.

**Every minutes item cites its transcript segments, and is verified against
them.** A second pass re-reads each extracted item against *only* the lines it
claims to come from, and items that are not supported get flagged rather than
shipped. The worst thing this product can do is fabricate a commitment with a
real person's name on it: nobody catches it, someone acts on it, and the tool is
finished. Citations also mean a reader can check any claim in one click, and
minutes you cannot check are minutes you eventually stop trusting.

**Accuracy is measured, not asserted.** See [`eval/`](eval/README.md). Without a
gold set, every change is a coin flip you cannot evaluate.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Extension | React, TypeScript (strict), Vite, MV3 | Capture must live in an offscreen document — MV3 service workers have no DOM and cannot hold a `MediaRecorder` |
| API | FastAPI, SQLAlchemy 2.0, PostgreSQL | Segments are the spine; every feature reads them |
| Queue | Postgres `FOR UPDATE SKIP LOCKED` | Transcription takes minutes, so it cannot run in a request. A broker would buy throughput we don't need — the GPU is the bottleneck |
| Transcription | `faster-whisper` `large-v3`, int8_float16 | The big model earns its keep on exactly the words minutes are made of: names, products, acronyms |
| Diarization | `pyannote.audio` (fallback only) | Anonymous clusters; used when there is no DOM timeline |
| Minutes | Claude, structured output + grounding pass | |
| RAG | ChromaDB (phase 2) | Inherits every upstream error, so it goes last |

## Running it

Requires **PostgreSQL**, **ffmpeg** on `PATH` (a system dependency — this is the
usual cause of a fresh install failing immediately), and an NVIDIA GPU.

```bash
# Backend
cd backend
python -m venv venv
venv/Scripts/python -m pip install -e ".[dev]"     # Windows; use venv/bin on POSIX

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

### GPU note

On a 4GB card, Whisper and pyannote will not fit at the same time. The worker
therefore holds only one model at a time and unloads between stages. In the
common case — a browser meeting on a supported platform — pyannote never loads at
all, because the DOM already gave us the speaker timeline, and Whisper gets the
whole card.

**Windows CUDA.** CTranslate2 links against cuBLAS and cuDNN but ships neither,
and Windows has no system CUDA toolkit to fall back on. The `nvidia-cublas-cu12`
and `nvidia-cudnn-cu12` wheels are therefore hard dependencies on `win32`, and
`app/core/cuda.py` puts them on the DLL search path at import. If you ever see
`RuntimeError: Library cublas64_12.dll is not found`, that module is the place to
look — note that `os.add_dll_directory` alone does *not* fix it, because
CTranslate2 resolves cuBLAS with a plain `LoadLibrary` that only searches `PATH`.

## Status

**Working end to end:** audio → ffmpeg → `large-v3` on the GPU → word-level
alignment → attributed segments in Postgres. Verified with
`scripts/smoke_transcribe.py`.

**Not yet built:**

- `extension/src/content/adapters/*.ts` — the DOM speaker timeline. Until it
  exists, remote speakers come out as `unknown` (or as anonymous pyannote
  clusters, if you enable the fallback). This is the accuracy advantage and the
  next piece to build.
- The extension's upload path has never been exercised against the API.
- `backend/app/services/minutes/` — extraction and grounding.
- RAG (`services/rag/`, `api/v1/routes/qa.py`) is deliberately stubbed until the
  transcripts underneath it are accurate.

## Recording consent

Recording a meeting without telling the participants is illegal in two-party-
consent jurisdictions. The visible `REC` badge and the in-popup notice are
product requirements, not disclaimers, and should not be quietly removed.
