# Development

## Tests

```bash
cd backend
venv/Scripts/python -m pytest

# The evaluation harness has its own suite, at the repository root.
# No Postgres, no GPU, no API key — it runs anywhere in under a second.
backend/venv/Scripts/python -m pytest eval
```

**Tests need a real Postgres.** [`conftest.py`](../backend/tests/conftest.py)
connects to the `postgres` admin database at `DATABASE_URL`, drops and creates
`meeting_minutes_test`, and builds the schema. If Postgres is unavailable the suite
**skips** rather than fails.

SQLite was considered and rejected. The models are Postgres-specific — UUID primary
keys, JSONB word timings, array columns — and speaker mapping leans on
`ON DELETE SET NULL` and multi-row `UPDATE`s. **A stand-in that passed while the
real database did otherwise would be worse than no test.**

The schema is built with `Base.metadata.create_all`, **not** `alembic upgrade head`,
so tests assert against the models as written and a missing migration cannot hide
behind a green suite. The trade-off is real, and it is how the
[`PRESENTER` check-constraint gap](data-model.md#migrations) survived: only running
migrations against a real database catches migration drift.

### Fixtures

| Fixture | Scope | What |
|---|---|---|
| `engine` | session | Creates and drops the test database |
| `db` | function | A session. **Truncates** all tables rather than recreating them |
| `client` | function | `TestClient` with `get_db` overridden to the test's session, so the API sees uncommitted rows |
| `meeting` | function | A finalized Meet meeting with a tab recording |
| `speakers` | function | A `SpeakerFactory` |

`SpeakerFactory` builds the three rows that matter, and its shape mirrors
[the gate's schema](data-model.md#the-mapping-gate-in-the-schema):

```python
speakers.roster("Priya")              # source=DOM       — a mapping candidate
speakers.cluster("SPEAKER_00")        # source=DIARIZATION — the gate is closed
speakers.ignored_cluster("SPEAKER_02")# source=MANUAL, is_excluded — the durable one
speakers.says(priya, "I'll take the migration.", duration_ms=4000)
```

`ignored_cluster` exists because that row is the trap: it keeps its `SPEAKER_02`
name forever, and counting it as a person is what inflated `max_speakers` and primed
Whisper with "SPEAKER_00" on every reprocess.

### What is covered

| File | Covers | DB? |
|---|---|---|
| `test_alignment.py` | 14 tests over `align` / `merge_tracks` | no |
| `test_speaker_mapping.py` | ~24 tests over the gate, end to end through the API | yes |
| `test_pipeline_inputs.py` | `build_vocabulary_prompt`, `_remote_speaker_bound` | yes |
| `test_minutes_extractor.py` | Chunking, dedup, rendering, date handling | no |
| `test_translation.py` | Batching, strict index mapping, `needs_translation` | no |

The two DB-free suites are the ones worth reading first — they encode the reasoning
in [Pipeline](pipeline.md) as assertions:

- A segment straddling a speaker change **is split**.
- A word 100ms past a turn goes to the nearest speaker; **500ms past stays
  `UNKNOWN`** — that is the hallucination guard.
- A 150ms flip enclosed by one speaker is absorbed; a 550ms run survives; **a short
  run at a segment edge is left alone**.
- Real speaking beats a presenter turn; so does a near-miss on a real speaker.
- The vocabulary prompt asserts `"SPEAKER" not in prompt`.
- `_remote_speaker_bound` is unchanged by a previous run's clusters.
- Translation batches **partition** without overlap; extraction chunks **overlap by
  exactly `CHUNK_OVERLAP`**.

`test_speaker_mapping.py` is the contract test: **no minutes are written while a
voice is unnamed.** It covers all three resolutions, the 409s, the double-resolve,
the `regenerate` gate, and the double-summarise race (a `RUNNING` translate job
makes resolve defer).

> **Import gotcha, and it is not optional:** tests do `from conftest import
> SpeakerFactory`, **not** `from tests.conftest import ...`. `pyannote.pipeline`
> ships a real top-level `tests` package in site-packages that wins over this
> implicit namespace package. `[tool.ruff.lint.isort]` has
> `known-local-folder = ["conftest"]` for the same reason.

## Scripts

### Smoke tests

```bash
python scripts/smoke_transcribe.py <mic.wav> <tab.wav>   # pipeline directly
python scripts/smoke_queue.py <mic.wav> <tab.wav>        # via the HTTP path
```

See [Operations](operations.md#verifying-without-the-extension). `smoke_queue`
imports no pipeline code on purpose — it exercises what the extension exercises.

### Adversarial grounding check

```bash
python scripts/check_grounding.py <meeting_id>
```

This one is worth understanding. A grounding pass that never rejects anything is
either lucky or broken, and you cannot tell which from the passing cases. So this
script feeds the verifier six claims against real segments from a real meeting —
one true, one with a correctly resolved deadline, and **four that must be
rejected**:

| Claim | Expected |
|---|---|
| True claim, correct owner | grounded |
| Correctly resolved relative deadline | grounded |
| Wrong owner | **rejected** |
| Wrong resolved date | **rejected** |
| Discussion inflated to a decision | **rejected** |
| Unsupported extra work | **rejected** |

Exits `1` if any case fails. Run it after touching the grounding prompt or switching
`GROUNDING_MODEL`.

> It currently hardcodes the speaker name `"Harman"` when selecting cited segments.
> Change it to match your own data.

## Migrations

```bash
cd backend
venv/Scripts/python -m alembic upgrade head
venv/Scripts/python -m alembic revision --autogenerate -m "what changed"
```

Two things in [`alembic/env.py`](../backend/alembic/env.py) are load-bearing:

- `import app.db.models  # noqa: F401` — purely for the side effect of registering
  every model with `Base.metadata`. Autogenerate cannot see a model nobody imported,
  and would cheerfully generate a migration that drops its table.
- `compare_type=True` — the enums are non-native (VARCHAR + CHECK), and without this
  autogenerate misses type changes.

`alembic.ini` leaves `sqlalchemy.url` blank; the URL comes from `DATABASE_URL`.

**Always read an autogenerated migration before committing it.** The enum check
constraints are exactly the kind of thing it gets subtly wrong — see the
[`PRESENTER` gap](data-model.md#migrations).

## Evaluation

[`eval/`](../eval/README.md). The argument: without a gold set, every change is a
coin flip you cannot evaluate, and "accurate" is a vibe.

The design calls for 3–5 real, **hard** meetings — accents, crosstalk, a bad mic,
internal product names — each in `eval/datasets/<name>/` with the raw `mic.webm` /
`tab.webm` exactly as uploaded, a hand-corrected `transcript.gold.json`, a
`keywords.txt`, and the `minutes.gold.md` you wish the tool had produced.
`eval/datasets/` is gitignored: it contains real meeting audio.

`keywords.txt` exists because **WER averages over "the" and "and"** — a model can
score well while mangling every proper noun in the meeting. Keyword recall is the
headline number.

**Current state: the harness works; the gold set does not exist.** Which means it
has measured nothing yet, and no accuracy claim in this repository is evidenced.

```bash
python -m eval.runner                # transcribe and score every dataset
python -m eval.runner --score-only   # score existing hypotheses, no GPU
python -m pytest eval                # the harness's own tests
```

Gold and hypothesis share **one format** (`eval/transcript.py`) — a hypothesis is a
transcript the machine wrote, gold is one a human corrected. That is what makes
scoring a pure function of two files: `--score-only` needs no GPU, no Postgres, and
no audio, which is why the scoring half has tests and the pipeline-driving half does
not. It also means bootstrapping gold is a rename plus an afternoon of correcting,
rather than a week of typing.

Everything except `runner.py` is stdlib-pure and imports no `app` — load-bearing,
since `app.workers.pipeline` pulls torch and a Whisper backend in at module scope.
`runner.py` keeps its `app` imports inside the transcribe function, and a test
asserts `torch` stays out of `sys.modules` on a `--score-only` run.

What it reports, per meeting: `WER`, `keyword` recall, `attrib` (word-level
attribution accuracy), `named` (named speaker rate), and `voices` (clusters found
against real people).

**`attrib` and `named` are two questions and neither survives being read alone.**
The pipeline emits `SPEAKER_00`, not "Priya", so scoring maps each cluster onto the
gold speaker it most co-occurs with, and `attrib` is computed after that — a cluster
is a *question*, not a wrong answer, and scoring it as one measures the mapping gate
instead of the diarizer. The mapping is many-to-one, mirroring `target_speaker_id`'s
merge, so over-clustering costs `attrib` nothing (`voices` is where that cost shows).
But the mapping assumes perfect naming by construction, so `attrib` can never be
evidence the product names anyone correctly. `named` is computed on the raw output
and is what keeps that honest. Full argument in `eval/scoring.py`.

**`keywords.txt` must never be fed to the model.** `agenda.txt` is the file that
primes Whisper's decoder. Priming it with the exact rare words keyword recall then
scores would raise the number and prove nothing.

Not measured yet: **minutes accuracy** (needs an LLM judge — the design argument is
in `eval/judge.py`, and it is unbuilt because a nondeterministic scorer grading a
nondeterministic system needs an argument before it needs code), **grounding
rejection rate** (the cheap half — one query over `MinutesItem.is_grounded`, where
`NULL` means "not yet run" and must not count as accepted), and **latency**.

`eval/datasets/example/` is committed — synthetic, no audio, five invented lines —
so the scorer can prove itself with no GPU. `test_example_dataset.py` pins its
numbers, hand-derived rather than recorded from a run.

## Code style

```bash
cd backend && venv/Scripts/python -m ruff check .    # backend/pyproject.toml
cd backend && venv/Scripts/python -m mypy app

backend/venv/Scripts/python -m ruff check eval       # ruff.toml, from the repo root
backend/venv/Scripts/python -m mypy                  # mypy.ini, from the repo root
```

`ruff` — line length 100, target py311, rules `E, F, I, UP, B`.
`mypy` — `strict = true`, with the pydantic plugin.

**Two config files, because `eval/` lives outside `backend/`.** Both tools discover
config by walking up from the file, so nothing under `backend/` ever reaches the
root `ruff.toml` / `mypy.ini`, and without them `eval/` would silently fall back to
ruff's defaults and go unchecked by mypy entirely. They mirror `backend/pyproject.toml`
and must be kept in step with it. `mypy.ini` carries three deliberate deviations,
each documented in the file: `mypy_path = backend` (mypy does not follow the editable
install's `.pth`), `python_version = 3.12` (numpy's stubs need it; the venv is 3.12
anyway), and `follow_imports = silent` for `app.*` (eval is held to its own standard,
not made responsible for the backend's).

Extension: `npm run typecheck` (`tsc --noEmit`), which `npm run build` runs first.
`tsconfig.json` adds `noUncheckedIndexedAccess` and `exactOptionalPropertyTypes` on
top of `strict`. (The `npm run lint` script references eslint, which is not
installed in the extension directory.)

## Conventions worth keeping

**Docstrings carry the *why*.** This codebase's docstrings are unusually
argumentative, and that is deliberate — nearly every constant and boundary in it was
paid for by something breaking, and the comment is the receipt. `MIN_RUN_MS = 300`
is meaningless without the paragraph explaining that duration alone would eat real
backchannel. When you change one of these, change its reasoning too, or delete the
claim.

**Keep [`CLAUDE.md`](../CLAUDE.md#current-status) and these docs current in the same
change that changes the behaviour**, so they never drift from the code.

**The failure mode is the design criterion.** The recurring argument in this
codebase is not "what is most accurate" but "how does this fail, and will anyone
notice". The DOM scrape was more accurate than diarization on a good day; it was
replaced because its bad day was silent. When adding something, ask what it does
when it breaks:

- Prefer a **visible question** to a confident guess (the mapping gate).
- Prefer **failing closed** where a wrong answer gets acted on (grounding), and
  **failing open** where a missing answer is merely degraded (translation).
- Prefer **flagging** to silently dropping (ungrounded items).
- Prefer a **predictable manual step** to an unpredictable silent failure.

## Known gaps

Collected from across these docs, for anyone looking for something to fix:

| Gap | Where |
|---|---|
| `PRESENTER` missing from the migration's `speakersource` CHECK constraint — a reachable 500 | [data model](data-model.md#migrations) |
| No unique constraint on `recordings (meeting_id, track)` | [data model](data-model.md#recordings) |
| No retry backoff — 3 attempts burn in ~6 seconds | [pipeline](pipeline.md#the-queue) |
| No lease timeout/reaper; a killed worker leaves a job `RUNNING` forever | [pipeline](pipeline.md#the-queue) |
| Reprocessing discards manual per-segment speaker corrections | [pipeline](pipeline.md#persisting-segments) |
| **No gold set exists, so the eval harness has measured nothing** | [above](#evaluation) |
| Minutes accuracy unmeasured; `eval/judge.py` is design notes only | [above](#evaluation) |
| Grounding rejection rate not computed, though the data is already persisted | [above](#evaluation) |
| Latency (finalize → minutes) not timed by the runner | [above](#evaluation) |
| `backend` ruff reports 66 pre-existing findings; `mypy app` reports 3 | [above](#code-style) |
| README cites `torch<2.9`; the real bound is `<2.6`, and its install line is unpinned | [operations](operations.md#the-torch-install-is-a-separate-step-and-it-is-the-one-that-goes-wrong) |
| `TRANSLATION_MODEL` missing from `.env.example` | [configuration](configuration.md#llm) |
| `vite.config.ts` comment describes an `inlineDynamicImports` that is not there | [extension](extension.md#build) |
| The offscreen document is never closed | [extension](extension.md#the-service-worker) |
| `check_grounding.py` hardcodes the speaker name `"Harman"` | [above](#adversarial-grounding-check) |
| `npm run lint` references an uninstalled eslint | [above](#code-style) |
| `probe_duration` derives `ffprobe` by naive string replace | [configuration](configuration.md#audio-storage) |
