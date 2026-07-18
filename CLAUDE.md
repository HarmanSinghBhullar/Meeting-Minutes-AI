# Project

Meeting Intelligence Browser Extension

## Features

- Record meeting audio
- Generate transcript
- Store transcript in PostgreSQL
- Translate transcript to English
- Speaker identification (pyannote diarization + a human mapping step)
- Meeting summaries
- Meeting dashboard (list, open, rename, delete, map speakers, regenerate minutes)
- RAG Q&A over previous meetings

## Tech Stack

Frontend:
- React
- TypeScript
- Vite

Backend:
- FastAPI
- PostgreSQL
- SQLAlchemy

AI:
- Whisper
- pyannote.audio
- ChromaDB

## Rules

- Use TypeScript strict mode
- Use FastAPI
- Modular architecture
- Add comments and docstrings
- Follow production-grade folder structure

## Documentation

`README.md` carries the argument (why the system is shaped this way). `docs/` is
the reference set — `architecture.md`, `data-model.md`, `api-reference.md`,
`pipeline.md`, `extension.md`, `configuration.md`, `operations.md`,
`development.md`. Both, and the section below, are kept current in the same change
that changes the behaviour. `docs/development.md` also tracks the known gaps.

## Current status

Keep this section current as features land — update it in the same change that
changes the behaviour, so it never drifts from the code.

Working end to end: audio → ffmpeg → Whisper `large-v3` → **pyannote diarization**
of the tab track → word-level alignment → attributed segments in Postgres →
translation for non-English meetings (`services/translation.py`, batched through
the same structured-LLM provider as the minutes; writes `Segment.text_en`, never
over `text`) → **speaker mapping (a human step — see below)** → cited minutes
(`services/minutes/`, OpenAI + Anthropic providers) → grounding pass.

### Attribution is diarization + a manual mapping step

The DOM active-speaker timeline used to be the primary attribution source and
**has been removed** (`services/attribution/dom_timeline.py` is deleted). It read
per-frame CSS classes out of the meeting UI, and when a platform reskinned it did
not error — it silently stopped matching and attributed every remote speaker to
"Unknown", surfacing days later as minutes with no owners. It was traded for a
signal that cannot break that way.

So `pyannote/speaker-diarization-3.1` now attributes every tab track
(`_remote_speaker_turns`). Consequences worth knowing:

- **The `[diarization]` extra is now required for the worker**, not optional:
  `pip install -e ".[diarization]"`, plus `HUGGINGFACE_TOKEN` and the accepted
  licences for **both** `segmentation-3.0` and `speaker-diarization-3.1`.
  `PYANNOTE_DEVICE` (default `cuda`) exists because pyannote otherwise runs on
  CPU. On Windows, torch must come from PyTorch's index — the PyPI wheel is
  CPU-only and fails silently into a slow CPU diarization.
- **Diarization runs in a child process** (`attribution/diarize_cli.py`), and this
  is not optional either. CTranslate2 and torch cannot both initialise cuDNN in
  one process: after Whisper touches the GPU, torch's cuDNN load aborts the
  interpreter with exit 127 and no traceback. Verified reproducible with both
  `cudnn64_9.dll` copies byte-identical, so it is not a version conflict — do not
  "simplify" the subprocess away. `workers/pipeline.py` must never import torch.
- **Every version bound in the `diarization` extra is load-bearing** and each was
  found by an install that broke: `pyannote.audio<4` (4.x renamed
  `use_auth_token`→`token` and needs FFmpeg shared libs via torchcodec),
  `torch<2.6` (2.6 flipped `torch.load(weights_only=True)`, which rejects
  pyannote's checkpoints), `torchaudio<2.6` (2.9 dropped
  `torchaudio.AudioMetaData`, imported by pyannote at module scope),
  `huggingface_hub<1` (1.0 removed the `use_auth_token` argument pyannote passes).
  Also: `speechbrain` must be `1.0.x` — 1.1's lazy `integrations.k2_fsa` import
  explodes under pyannote 3.x. And `nvidia-cudnn-cu12`/`nvidia-cublas-cu12` must
  match what torch bundles.
- **The mic track is never diarized** — it is the local user by definition — so
  clusters are always remote participants and the user is never asked to identify
  themselves.
- The roster (`getParticipants()` → `Speaker` rows, `source=DOM`) is *not* the
  fragile part. It survives as the name source: the candidate list for mapping,
  and the `max_speakers` bound passed to pyannote (`_remote_speaker_bound`:
  roster + 1, never an exact count). It is **polled every 5s and once more on
  stop**, not captured once. The single read taken in the record click's call
  stack was wrong in two ordinary ways, both silent: the tile grid may not have
  rendered yet (a real 2026-07-17 meeting came out with *nobody* on it and five
  unnamed clusters), and anyone joining later never appeared at all — no
  attendance, and no candidate offered for their own voice. Newcomers merge via
  `POST /recordings/meetings/{id}/participants`, which is **additive only**: it
  never renames, never deletes, and never touches a row a human ruled on at the
  mapping panel, because the roster arrives on a timer and an answer does not.
- **The roster's *timing* is robust; its *selectors* still rot, and now say so.**
  The poll fixed the empty-at-click-time race, but capture still depends on reading
  Meet's obfuscated DOM, and on 2026-07-18 it broke silently: Meet moved the
  participant name out of `data-self-name`/`data-participant-name` into a plain
  `span.notranslate`, so every `SELECTORS.name` hook missed and two real Meet calls
  recorded *nobody* — voices with no attendance and no map candidates, exactly the
  old failure wearing new clothes. The name now reads from `span.notranslate`
  (`.notranslate` is Google's translate-exclusion marker, not an obfuscated class,
  so it outlives reskins), filtered by `asName` against Material icon ligatures
  (`more_vert`, `devices`) that share that class — a ligature is lowercase
  snake_case, a name has a capital or a space. `data-self-name` went with it, so
  **self** is now the `(You)` marker resolved by participant-id (it often sits on
  the People-panel entry, not the grid tile) with a self-only-control fallback
  (`Reframe`/`Backgrounds and effects`) for when the panel is closed. The blind
  spot that let this hide: `MeetAdapter.checkHealth` treated zero tiles as "UI not
  loaded yet" and never warned. It now tracks `sawAnyTile` and, past the grace
  window, distinguishes **no tile at all** (`SELECTORS.tile` rotted — roster dead)
  from **tiles but no speech** (`SELECTORS.speaking` rotted — eval data only), and
  the content script warns at stop when it captured no roster all meeting.
- **Only rows that stand for a real person feed Whisper and pyannote**
  (`_real_people` in `workers/pipeline.py`), which is subtler than it sounds
  because both inputs are read *before* the same job clears the last run's rows.
  Two kinds of row have a `display_name` like `SPEAKER_02`: an unmapped cluster
  (deleted later in the same job) and — the durable one — a cluster someone
  *ignored*, which `_mark_excluded` moves to `MANUAL` without renaming and nothing
  ever deletes. Counted, they primed Whisper's `initial_prompt` with "SPEAKER_00"
  and inflated `max_speakers` on every reprocess. Reprocessing is a repair
  operation: it must hand both models what the first run did.

**Alignment absorbs the two clocks, not the disagreement.** Whisper's word times
and pyannote's turn boundaries come from different models and disagree by
~100–200ms even when they agree about *who* spoke. `services/alignment.py` treats
that as noise rather than signal, in two tightly-bounded places — both deliberately
too small to swallow real speech, since over-applying either silently puts one
person's words in another's mouth:

- `NEAREST_TURN_TOLERANCE_MS` (250): a word landing in the *seam* between two tight
  turns overlaps neither and used to come out `UNKNOWN` mid-sentence. It now goes to
  the nearest turn within the tolerance — and a near-miss on a real speaker beats a
  screen-share `PRESENTER` turn. Past the tolerance it is still `UNKNOWN`: a word in
  genuine silence is likely a hallucination, and it must not be handed to whoever
  spoke last.
- `MIN_RUN_MS` (300): a word or two flipping to a neighbour and straight back is
  jitter, not a turn. Absorbed only when *enclosed* by one other speaker — duration
  alone would eat real backchannel. Runs at a segment edge are left alone; there is
  no enclosure to judge them by.

`align` also sorts turns defensively: both searches stop early on ascending starts,
so an unsorted timeline would not raise, it would silently attribute everything
before the first turn to nobody.

**The mapping gate.** A diarized meeting stops after transcription/translation and
waits: minutes crediting `SPEAKER_01` are not a rough draft, they are a confident
wrong answer. The gate is `Speaker.source == DIARIZATION` — an unnamed cluster row
*is* the unmapped state, so there is no status column to keep in step
(`services/attribution/mapping.unmapped_cluster_count`, which lives outside
`workers/pipeline` so the API can import it without pulling in torch). Resolving
the last cluster queues `MINUTES` (`api/v1/routes/speakers.py`);
`POST /minutes/regenerate` returns **409** while any remain, and `run_minutes`
raises if it is ever reached with clusters outstanding.

Three resolutions, via `POST /meetings/{id}/speakers/{sid}/resolve`:
`target_speaker_id` **merges** the cluster into a roster speaker (reassigns the
segments and *deletes* the cluster row — renaming it would leave two Priyas),
`display_name` names a phone/late joiner in place, and `ignore` sets the new
`Speaker.is_excluded` for a shared video or hold music (its segments stay in the
transcript but `_minutable` withholds them from the minutes). All three mark the
segments `MANUAL`. Reprocessing deletes stale `DIARIZATION` rows, since cluster
numbering is not stable across runs.

The UI is `meeting/SpeakerMapping.tsx`, above the minutes on the meeting page:
each cluster leads with its **longest** lines (the identifying ones — a call opens
with "hi"), longest-talking cluster first. The dashboard derives `Needs speakers
(n)` straight off `meeting.speakers` with no extra request.

Opening the extension's meeting page with no `?id=` renders a **dashboard**
(`meeting/Dashboard.tsx`) listing every meeting with open / rename / delete /
map-speakers / regenerate-minutes — backed by `PATCH /meetings/{id}` (rename),
`DELETE /meetings/{id}` (cascades the row and calls `storage.delete_meeting` to
remove the audio), and `POST /meetings/{id}/minutes/regenerate`. The minutes
summary is stored and shown as point-wise bullets (`Minutes` heading), and
attendees appear under an `Attendance` heading tagged `Name (You)` for the local
user — excluding unnamed clusters and excluded ones, which are not attendance.
That section **always renders**: unnamed clusters are *counted* beneath the
register (`N voices are not identified yet`, linking to the mapping panel) rather
than listed in it. A meeting whose roster never got captured has nothing but
clusters, and rendering nothing at all made the names look lost rather than
unasked-for — while listing `SPEAKER_01` as an attendee would assert a person by
that name was there. Counting is the only honest third option.

The transcript below them is grouped into a collapsible `<details>` per
wall-clock minute (`Meeting.tsx`, `bucketByMinute`), summarised by range, line
count and speakers, with the first open and an `Expand all` for Ctrl+F. Segments
bucket on `startMs` only, so a segment straddling a boundary lands in exactly one
group; silent minutes get no row. Searching narrows each group to its matching
lines (text *or* speaker name), opens the survivors, marks the hits, and reports
`3 of 11` per group — the header counts lines rather than "matches", since one
line can highlight twice.

The Meet adapter still identifies the local user (by Meet's "(You)" marker, or a
self-only tile control when the People panel is closed — `data-self-name` was
dropped in the 2026-07-18 reskin) so their track is named rather than an invented
"You". It also still records active-speaker and presenter events to `SpeakerEvent`;
these no longer drive attribution and are kept only as evaluation data against the
diarizer. `SELECTORS.speaking` rotting is therefore no longer a correctness bug —
but `SELECTORS.tile` and `SELECTORS.name` rotting still is, since the roster (hence
attendance and the map candidates) is built on them; see the roster bullet above
for the health warnings that now make that failure loud instead of silent.

### The eval harness measures the transcript; nothing measures the minutes yet

`eval/` is built and tested (`python -m pytest eval` — no Postgres, no GPU, under a
second). **It has measured nothing**, because `eval/datasets/` is empty: the harness
is a working ruler with nothing to measure, and until real meetings land there, no
accuracy claim in this repository is evidenced.

- Gold and hypothesis share **one format** (`eval/transcript.py`) — a hypothesis is
  a transcript the machine wrote; gold is one a human corrected. So scoring is a
  pure function of two files (`--score-only`: no GPU, no Postgres, no audio), and
  bootstrapping gold is a rename plus an afternoon of correcting rather than a week
  of typing. Integer ms everywhere, and no word timings — every metric aligns word
  *sequences*, so a word inherits its segment's speaker.
- **Only `runner.py` imports `app`, and only inside the function that transcribes.**
  Same constraint as everywhere else here: `workers/pipeline` pulls torch and a
  Whisper backend in at module scope, and a scorer needing a GPU to compare two
  strings is a scorer nobody runs. A test asserts `torch` stays out of `sys.modules`.
- **`attrib` and `named` are two questions; reading either alone misleads.** The
  pipeline emits `SPEAKER_00`, so scoring maps each cluster onto the gold speaker it
  most co-occurs with — a cluster is a *question*, not a wrong answer, and scoring it
  as one measures the mapping gate rather than the diarizer. The mapping is
  many-to-one, mirroring what `target_speaker_id` merges, so over-clustering costs
  `attrib` nothing (`voices` is where that cost is visible). But it assumes perfect
  naming by construction, so `attrib` can never show the product names people right.
  `named` is computed on raw output and is what keeps that honest.
- **`keywords.txt` is scored, never fed to the model**; `agenda.txt` is the one that
  primes Whisper's decoder. Priming with the exact words recall is scored on would
  raise the number and prove nothing.
- `eval/datasets/example/` is committed (synthetic, no audio) so the scorer can prove
  itself with no GPU; `.gitignore` allows that one directory and still blocks audio
  inside it. Its numbers are pinned by hand-derived tests.
- `ruff.toml` and `mypy.ini` at the repo root exist only because `eval/` sits outside
  `backend/` and both tools resolve config by walking up. Keep them in step with
  `backend/pyproject.toml`.

Not yet built:

- Minutes accuracy — `eval/judge.py` is the design argument and no code. It needs an
  LLM judge, i.e. a nondeterministic scorer grading a nondeterministic system, which
  needs an argument before it needs an implementation. Grounding rejection rate is
  the cheap half and needs no judge: one query over `MinutesItem.is_grounded`, where
  `NULL` means "not yet run" and must not count as accepted.
- RAG — `services/rag/` and the `INDEX` job are stubbed; `api/v1/routes/qa.py`
  returns `501`. It is deliberately last, since Q&A inherits every upstream error.

## Running locally

- API: `backend/venv/Scripts/python -m uvicorn app.main:app --reload`
- Worker (separate process): `backend/venv/Scripts/python -m app.workers.run_worker`
- Extension: `cd extension && npm run build`, then load `extension/dist` unpacked
  at `chrome://extensions`.
- Requires PostgreSQL, `ffmpeg` on `PATH`, and an NVIDIA GPU. See `README.md`.
- The worker additionally requires `pip install -e ".[diarization]"` and a
  `HUGGINGFACE_TOKEN` with the pyannote licences accepted — without them every
  transcribe job fails at attribution.
- Tests need Postgres: `tests/conftest.py` creates and drops a `meeting_minutes_test`
  database (not SQLite — the models are Postgres-specific, and a stand-in that
  passed while the real database did otherwise would be worse than no test).