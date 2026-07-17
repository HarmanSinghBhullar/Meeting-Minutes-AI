# Development

## Tests

```bash
cd backend
venv/Scripts/python -m pytest
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

**Current state: `eval/metrics.py` defines the metric dataclasses but
`word_error_rate` and `keyword_recall` both raise `NotImplementedError`, and there
is no runner.** The dataclasses do encode the intent:

- `TranscriptionMetrics`: `wer`, `keyword_recall`
- `AttributionMetrics`: `word_level_accuracy`, `named_speaker_rate`
- `MinutesMetrics`: action-item and decision precision/recall, plus
  `grounding_rejection_rate` — with a comment noting that a rejection rate of zero
  means the grounding pass should be **distrusted, not celebrated**.

> `eval/README.md` is stale: it still describes reporting a DOM attribution path and
> a pyannote fallback separately. Diarization has been the sole attribution source
> since the DOM timeline was deleted.

## Code style

```bash
venv/Scripts/python -m ruff check .
venv/Scripts/python -m mypy app
```

`ruff` — line length 100, target py311, rules `E, F, I, UP, B`.
`mypy` — `strict = true`, with the pydantic plugin.

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
| `eval/metrics.py` functions unimplemented; no runner | [above](#evaluation) |
| `eval/README.md` still describes the deleted DOM attribution path | [above](#evaluation) |
| README cites `torch<2.9`; the real bound is `<2.6`, and its install line is unpinned | [operations](operations.md#the-torch-install-is-a-separate-step-and-it-is-the-one-that-goes-wrong) |
| `TRANSLATION_MODEL` missing from `.env.example` | [configuration](configuration.md#llm) |
| `vite.config.ts` comment describes an `inlineDynamicImports` that is not there | [extension](extension.md#build) |
| The offscreen document is never closed | [extension](extension.md#the-service-worker) |
| `check_grounding.py` hardcodes the speaker name `"Harman"` | [above](#adversarial-grounding-check) |
| `npm run lint` references an uninstalled eslint | [above](#code-style) |
| `probe_duration` derives `ffprobe` by naive string replace | [configuration](configuration.md#audio-storage) |
