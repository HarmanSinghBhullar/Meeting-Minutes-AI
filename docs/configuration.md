# Configuration

Everything is environment variables, read once at import by
[`app/core/config.py`](../backend/app/core/config.py) via pydantic-settings. Copy
[`.env.example`](../.env.example) to `backend/.env` and edit.

`get_settings()` is `lru_cache`d and `settings = get_settings()` runs at **module
import**, so a missing `DATABASE_URL` fails at startup rather than on the first
request. That is intentional: a misconfigured process should not accept traffic.

Only `DATABASE_URL` is required. `HUGGINGFACE_TOKEN` is required *in practice* for
the worker — without it every transcribe job fails at attribution.

## Database

| Variable | Type | Default | Notes |
|---|---|---|---|
| `DATABASE_URL` | str | **required** | e.g. `postgresql+psycopg://user:pass@localhost/meeting_minutes` |

Also used by Alembic — `alembic.ini` leaves `sqlalchemy.url` deliberately blank and
`env.py` fills it from here, so one config works everywhere and no credentials land
in git. The test suite reads it too, and connects to the `postgres` admin database
on the same server to create and drop `meeting_minutes_test`.

## API

| Variable | Type | Default | Notes |
|---|---|---|---|
| `API_HOST` | str | `127.0.0.1` | Localhost by design — there is no auth |
| `API_PORT` | int | `8000` | The extension hardcodes `127.0.0.1:8000` in `src/lib/api.ts` |
| `CORS_ORIGINS` | str | `""` | Comma-separated. Parsed by the `cors_origin_list` property |
| `CORS_ORIGIN_REGEX` | str | `chrome-extension://.*` | What actually lets the extension in |

The regex is doing the work: an unpacked extension's origin changes with its id, so
listing origins by hand does not survive a reload. **Widening either of these is a
security decision, not a convenience one** — the API has no authentication.

## Audio storage

| Variable | Type | Default | Notes |
|---|---|---|---|
| `STORAGE_DIR` | Path | `./storage` | Created on startup |
| `FFMPEG_BIN` | str | `ffmpeg` | Full path if it is not on `PATH` |

Layout, per meeting:

```
<STORAGE_DIR>/<meeting_id>/mic.webm       # appended chunks, as uploaded
<STORAGE_DIR>/<meeting_id>/tab.webm
<STORAGE_DIR>/<meeting_id>/mic.16k.wav    # normalized for the models
<STORAGE_DIR>/<meeting_id>/tab.16k.wav
```

`storage/` is gitignored. Deleting a meeting removes its directory.

> `probe_duration` derives the `ffprobe` path by string-replacing `ffmpeg` in
> `FFMPEG_BIN`. An `FFMPEG_BIN` whose *directory* contains "ffmpeg" (e.g.
> `C:\ffmpeg\bin\ffmpeg.exe`) gets mangled — durations come back `None`, which is
> cosmetic and does not fail the transcript.

## Transcription

| Variable | Type | Default | Notes |
|---|---|---|---|
| `WHISPER_MODEL` | str | `large-v3` | |
| `WHISPER_DEVICE` | str | `cuda` | |
| `WHISPER_COMPUTE_TYPE` | str | `int8_float16` | Keeps large-v3 near ~2GB VRAM |
| `WHISPER_VAD_FILTER` | bool | `True` | |

`large-v3` earns its keep on exactly the words minutes are made of: names,
products, acronyms. `int8_float16` is what makes it fit on a 4GB card alongside a
browser — dropping to `float16` needs headroom this project does not assume.

`WHISPER_VAD_FILTER=false` is a bad idea in a meeting: Whisper's signature failure
is inventing fluent sentences during silence, and meetings are mostly silence.

## Diarization

| Variable | Type | Default | Notes |
|---|---|---|---|
| `HUGGINGFACE_TOKEN` | str \| None | `None` | **Required in practice.** A read token |
| `PYANNOTE_MODEL` | str | `pyannote/speaker-diarization-3.1` | |
| `PYANNOTE_DEVICE` | str | `cuda` | pyannote defaults to CPU on its own |
| `DIARIZATION_ENABLED` | bool | `True` | |

**The token is not enough — the licences must be accepted on *both* model pages**:
`pyannote/segmentation-3.0` **and** `pyannote/speaker-diarization-3.1`. The pipeline
loads segmentation internally, so accepting only the pipeline's own page fails with
a 401 that reads like a network error. This is the step that costs everyone an
hour.

`PYANNOTE_DEVICE=cuda` is safe despite the 4GB budget because the worker holds one
model at a time and Whisper is unloaded before diarization loads. Leave it unless
you have no GPU: on CPU, an hour of audio takes tens of minutes instead of a couple.

**`DIARIZATION_ENABLED=false` means no attribution at all** — every remote speaker
comes out "Unknown". It is not a lightweight mode; it is a diagnostic switch, only
sensible with no GPU and no HF token.

## LLM

| Variable | Type | Default | Notes |
|---|---|---|---|
| `LLM_PROVIDER` | str | `openai` | `openai` or `anthropic` |
| `OPENAI_API_KEY` | str \| None | `None` | Required if provider is `openai` |
| `ANTHROPIC_API_KEY` | str \| None | `None` | Required if provider is `anthropic` |
| `MINUTES_MODEL` | str | `gpt-5.6` | |
| `GROUNDING_MODEL` | str | `gpt-5.6` | |
| `TRANSLATION_MODEL` | str | `gpt-5.6` | |

Both SDKs are installed deliberately, and `get_provider()` imports lazily and
raises a clear error if the selected provider's key is missing. So **A/B-ing a
provider on the eval set is a config change, not a reinstall**.

Known model ids: OpenAI `gpt-5.6`; Anthropic `claude-opus-4-8`, `claude-sonnet-5`.
The three model settings are separate because they are separate jobs — grounding is
a narrow, well-posed question and could reasonably run on a cheaper model than
extraction.

> `TRANSLATION_MODEL` is read by `run_translate` but is **not listed in
> `.env.example`**, which documents 21 vars and omits this one. It defaults to
> `gpt-5.6`, so nothing breaks; the example file is just incomplete.

> `ANTHROPIC_API_KEY` is empty in the current `backend/.env`, so the one-line
> `LLM_PROVIDER=anthropic` switch needs a key added before it works on this
> machine.

## RAG (phase 2)

| Variable | Type | Default |
|---|---|---|
| `CHROMA_DIR` | Path | `./chroma` |
| `EMBEDDING_MODEL` | str | `sentence-transformers/all-MiniLM-L6-v2` |

Reserved. Nothing reads them yet — `services/rag/` is stubbed and
`POST /api/v1/qa` returns 501. See
[Architecture](architecture.md#deliberately-not-built).

## Which process needs what

| | API | Worker | Tests |
|---|---|---|---|
| `DATABASE_URL` | ✅ | ✅ | ✅ |
| `STORAGE_DIR`, `FFMPEG_BIN` | writes chunks | ✅ | |
| `CORS_*`, `API_*` | ✅ | | |
| `WHISPER_*` | | ✅ | |
| `HUGGINGFACE_TOKEN`, `PYANNOTE_*` | | ✅ | |
| LLM keys and models | | ✅ | |

The API needs neither a GPU nor an LLM key. It writes rows and enqueues jobs; the
worker does everything expensive. That split is
[why the API must never import torch](architecture.md#three-processes-and-why).
