# Operations

Install, run, and the specific failures that cost people an hour. The
[README](../README.md#running-it) has the short version; this page is the long one.

## Prerequisites

| | Why |
|---|---|
| **PostgreSQL** | The models are Postgres-specific (UUID PKs, JSONB, array columns, `SKIP LOCKED`) |
| **ffmpeg on `PATH`** | A *system* dependency, not a pip package. The most common cause of a fresh install failing immediately |
| **An NVIDIA GPU** | Whisper `large-v3` + pyannote. Verified on an RTX 3050 (4GB, driver 592) |
| **Python 3.11+** | |
| **Node 18+** | For the extension |
| **A Hugging Face account** | For the pyannote model licences |

## Install

```bash
cd backend
python -m venv venv
venv/Scripts/python -m pip install -e ".[dev,diarization]"   # Windows; venv/bin on POSIX

createdb meeting_minutes                # or: psql -U postgres -c "CREATE DATABASE meeting_minutes;"
cp ../.env.example .env                 # then set DATABASE_URL and your keys
venv/Scripts/python -m alembic upgrade head
```

```bash
cd extension
npm install
npm run build            # then load extension/dist unpacked at chrome://extensions
```

### The torch install is a separate step, and it is the one that goes wrong

**On Windows, install torch from PyTorch's own index, not PyPI.** The default PyPI
wheel is CPU-only and it does **not** fail — pyannote quietly falls back to CPU and
diarizes several times slower, with only a log line to say so.

```bash
venv/Scripts/python -m pip install "torch>=2.4,<2.6" "torchaudio>=2.4,<2.6" \
    --index-url https://download.pytorch.org/whl/cu126
venv/Scripts/python -c "import torch; print(torch.cuda.is_available())"   # must print True
```

Pick the `cuXXX` index matching your driver; `cu126` is verified here.

**Keep the version bounds.** They are declared in `pyproject.toml` under the
`diarization` extra, and every one was paid for by an install that broke:

| Bound | What breaks without it |
|---|---|
| `pyannote.audio>=3.3,<4` | 4.x renamed `use_auth_token`→`token` and routes audio I/O through torchcodec, needing FFmpeg **shared libraries** on the DLL path |
| `torch>=2.4,<2.6` | 2.6 flipped `torch.load(weights_only=True)`; the safe unpickler rejects pyannote's checkpoints, and allowlisting does not converge |
| `torchaudio>=2.4,<2.6` | Must match the torch it was built against. 2.9 also dropped `torchaudio.AudioMetaData`, which pyannote imports at module scope |
| `huggingface_hub>=0.23,<1` | 1.0 removed the `use_auth_token` argument pyannote still passes → `TypeError` fetching gated weights |
| `speechbrain>=1.0,<1.1` | 1.1's lazy `integrations.k2_fsa` import explodes under pyannote 3.x |

Also, `nvidia-cudnn-cu12` / `nvidia-cublas-cu12` must match what torch bundles.

> The README's pyannote section currently cites `torch<2.9` / `torchaudio<2.9`. The
> real bounds in `pyproject.toml` are `<2.6`. Trust `pyproject.toml`, and note that
> the README's unpinned `pip install torch torchaudio` would install a torch
> **outside** the declared bound.

### Accept the pyannote licences

The models are **gated**, and this is the step everyone loses an hour to. Accept the
licence on **both** pages:

- `pyannote/segmentation-3.0`
- `pyannote/speaker-diarization-3.1`

then put a read token in `HUGGINGFACE_TOKEN`. The diarization pipeline loads
segmentation internally, so accepting only the pipeline's own page fails with a 401
that reads like a network error.

If the token is valid but a licence is not accepted, `Pipeline.from_pretrained`
returns **`None`** rather than raising — which is why `diarize_cli` checks for it
explicitly and says so.

## Running

Three processes:

```bash
# API — auto-reloads
backend/venv/Scripts/python -m uvicorn app.main:app --reload

# Worker — separate process, owns the GPU
backend/venv/Scripts/python -m app.workers.run_worker

# Extension — rebuild, then reload at chrome://extensions
cd extension && npm run build
```

The worker does **not** auto-reload. After a backend change, restart it; the API
picks the change up on its own.

Then: open a meeting, click the extension, *Start recording*. Stop when done and
watch the meeting page.

## Verifying without the extension

Two smoke scripts, testing different things:

```bash
# The pipeline directly — ffmpeg, Whisper, alignment, Postgres. No API, no worker.
venv/Scripts/python scripts/smoke_transcribe.py <mic.wav> <tab.wav>

# The production HTTP path — needs both uvicorn and the worker running.
venv/Scripts/python scripts/smoke_queue.py <mic.wav> <tab.wav>
```

`smoke_transcribe` isolates the models. `smoke_queue` isolates the plumbing: it
posts a meeting, uploads both files as chunks, finalizes, polls until the transcribe
job succeeds, and prints the transcript. If `smoke_transcribe` passes and
`smoke_queue` hangs, the worker is not running.

## Failure modes

### `RuntimeError: Library cublas64_12.dll is not found`

CTranslate2 links cuBLAS and cuDNN but ships neither, and Windows has no system
CUDA toolkit to fall back on. The DLLs *are* on disk — under
`site-packages/nvidia/*/bin`, from the `nvidia-cublas-cu12` and `nvidia-cudnn-cu12`
wheels — just not somewhere CTranslate2 looks.

[`app/core/cuda.py`](../backend/app/core/cuda.py) fixes this at import, and it is
the place to look. It does **two** things, and both are needed:

- `os.add_dll_directory(...)` for each `nvidia/*/bin`
- **prepends those directories to `PATH`**

The second is the one that actually works. `add_dll_directory` only affects loads
that opt into `LOAD_LIBRARY_SEARCH_USER_DIRS`, and CTranslate2 resolves cuBLAS
lazily at the first GPU call with a plain `LoadLibrary` that only searches `PATH`.

If `nvidia/` is missing entirely it warns with the remedy:
`pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`.

### The interpreter dies with exit 127, no traceback

`Could not load symbol cudnnGetLibConfig. Error code 127`.

Something imported torch into the worker process after CTranslate2 touched the GPU.
The two CUDA runtimes cannot coexist in one process, and there is nothing to catch
— the interpreter is gone.

**`workers/pipeline.py` must never import torch.** Diarization runs in a child
process for exactly this reason
([details](pipeline.md#why-a-subprocess)). Do not simplify it away.

### Every transcribe job fails at attribution

The `[diarization]` extra is **not optional for the worker** — it is the only
attribution source. Check, in order:

1. `pip install -e ".[diarization]"` actually ran in the worker's venv
2. `HUGGINGFACE_TOKEN` is set
3. Both licences are accepted
4. `torch.cuda.is_available()` is `True`

The error message on the failed job names which one. `_tail` surfaces the last 15
lines of the child's stderr, which is where the real message lives.

### Diarization is mysteriously slow

You have the CPU-only torch wheel. `torch.cuda.is_available()` returns `False`,
`_to_device` warns and carries on on CPU — a slow transcript beats no transcript,
but it is a warning line in a log nobody is reading. Reinstall torch from the
PyTorch index.

### The meeting sits at "Needs speakers"

Working as designed. Diarization found voices it cannot name, and
[the gate](architecture.md#the-mapping-gate) is holding the minutes. Open the
meeting and map them. `POST /minutes/regenerate` returns **409** until you do.

### A meeting is stuck "running" forever

There is no lease timeout or reaper: `claim_next` only selects `PENDING`, so a
worker killed mid-job leaves the row `RUNNING` and nothing reclaims it. Reset it by
hand:

```sql
UPDATE jobs SET status = 'PENDING' WHERE id = '...';
```

(Note the **uppercase** value — the enums are `native_enum=False` and persist the
member *name*.)

### The tab goes silent when recording starts

Capturing a tab mutes it. The offscreen recorder pipes the tab stream back through
an `AudioContext` to restore playback. If that breaks, the user is deafened in
their own meeting and will notice immediately.

### `chrome-extension://` requests are blocked by CORS

An unpacked extension's origin changes with its id, so `CORS_ORIGIN_REGEX`
(`chrome-extension://.*`) is what lets it in, not `CORS_ORIGINS`. Check the former.

### The content script silently never runs

Check that `dist/src/content/content.js` exists and matches the path in
`manifest.json`, and that it contains no top-level `import` statement — that is a
syntax error in a content script, and the failure is silent. See the
[build note](extension.md#build).

## Data and privacy

Everything is local: Postgres on your machine, audio under `STORAGE_DIR`, no
service in between. The exceptions are the LLM calls — **transcript text is sent to
OpenAI or Anthropic** for translation, minutes, and grounding. Audio never leaves
the machine.

`.gitignore` covers `storage/`, `chroma/`, `models/`, `.env`, and
`eval/datasets/` — the last one because real meeting audio must never be committed.

## Recording consent

Recording a meeting without telling the participants is illegal in two-party-consent
jurisdictions. The visible `REC` badge and the popup notice — *"Recording. Everyone
in the meeting should know."* — are **product requirements, not disclaimers**. They
should not be quietly removed.
