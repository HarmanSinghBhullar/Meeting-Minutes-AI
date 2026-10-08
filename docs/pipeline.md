# The pipeline

The worker is a separate process
([`app/workers/run_worker.py`](../backend/app/workers/run_worker.py)) that claims
jobs from Postgres and runs them. It owns the GPU; the API never touches it.

```
normalize ─> transcribe ─> attribute ─> align ─> [translate] ─> [MAPPING GATE] ─> minutes ─> ground
└──────────────── one TRANSCRIBE job ────────────┘   a job         a human          a job     a job
```

Only four stages are **jobs**: `TRANSCRIBE`, `TRANSLATE`, `MINUTES`, `GROUND`.
`normalize`, `attribute`, and `align` are function calls inside `run_transcribe`,
because they share the audio and the loaded models — splitting them into jobs
would mean reloading a 2GB model to gain nothing.

The order is forced by data dependency, not preference. You cannot align words you
have not transcribed against turns you have not diarized.

> **`workers/pipeline.py` must never import torch.** See
> [why diarization is a subprocess](#why-a-subprocess).

## The queue

[`app/workers/queue.py`](../backend/app/workers/queue.py). Postgres, no broker.

```python
select(Job).where(Job.status == JobStatus.PENDING)
    .order_by(Job.created_at).limit(1)
    .with_for_update(skip_locked=True)
```

`SKIP LOCKED` is what makes multiple workers safe: each claims a different row
rather than queueing behind the same one. `claim_next` then flips the row to
`RUNNING`, increments `attempts`, stamps `started_at`, and commits.

| Function | Behaviour |
|---|---|
| `enqueue(db, meeting_id, job_type)` | Inserts a `PENDING` job and **commits** |
| `claim_next(db)` | The query above, or `None` |
| `succeed(db, job)` | `SUCCEEDED`, `progress = 100`, `finished_at` |
| `fail(db, job, error)` | `error[:4000]`; back to `PENDING` if `attempts < MAX_ATTEMPTS`, else `FAILED` |
| `set_progress(db, job, progress)` | Clamped 0–100. Currently unused |

`MAX_ATTEMPTS = 3`. `POLL_INTERVAL_SECONDS = 2.0` in the worker loop.

The loop opens a **fresh session per iteration**, catches bare `Exception` around
the handler (`a worker must survive any job`), logs the traceback, and calls
`fail`. Nothing in the pipeline retries internally — every stage raises and the
worker decides.

> **Two gaps worth knowing.** (1) There is **no backoff**: a failed job is
> immediately eligible for re-claim on the next 2-second poll, so a
> deterministically failing job burns its three attempts in about six seconds.
> That is survivable but not intentional. (2) There is **no lease timeout or
> reaper**: `claim_next` only selects `PENDING`, so a worker killed mid-job leaves
> the row `RUNNING` forever. The `Job` docstring claims crash recovery; it does
> not have it.

## `run_transcribe`

The big one. [`workers/pipeline.py`](../backend/app/workers/pipeline.py).

1. Load the meeting → `ValueError` if missing.
2. Collect recordings where `is_finalized and chunk_count > 0 and source_path` →
   `RuntimeError("Meeting has no finalized audio to transcribe.")` if empty.
3. **`_normalize`** — ffmpeg each track to 16kHz mono PCM; store
   `normalized_path` and `duration_seconds` (from `ffprobe`).
4. **`_transcribe`** — Whisper over both tracks, one resident model.
5. **Language** — first non-null detection over `(TAB, MIC)`, written to
   `meeting.source_language`. Tab is checked first because it carries most of the
   speech.
6. **`_attribute`** → **`_persist_segments`**.
7. Enqueue `TRANSLATE` if the language is not English, else
   `_enqueue_minutes_unless_unmapped`. Commit.

### Normalization

[`services/audio/ffmpeg.py`](../backend/app/services/audio/ffmpeg.py):

```
ffmpeg -y -i <source> -ar 16000 -ac 1 -c:a pcm_s16le <dest>
```

16kHz mono is what both Whisper and pyannote want. `-y` makes a re-run
idempotent. ffmpeg is a **system dependency** — a binary on `PATH`, not a pip
package — and a missing one is the single most common cause of a fresh install
failing immediately, so `FfmpegError` says so explicitly and names `FFMPEG_BIN`.

`probe_duration` shells out to `ffprobe` and returns `None` on any failure rather
than raising: a missing duration is cosmetic, and it must not fail a transcript.

### Transcription

[`services/transcription/faster_whisper_provider.py`](../backend/app/services/transcription/faster_whisper_provider.py),
behind a `TranscriptionProvider` Protocol.

```python
model.transcribe(
    str(audio_path),
    language=language,
    initial_prompt=vocabulary_prompt,
    word_timestamps=True,
    vad_filter=settings.whisper_vad_filter,   # True
    condition_on_previous_text=False,
)
```

Every one of those arguments is a decision:

- **`word_timestamps=True`** — required by alignment. Not optional.
- **`vad_filter=True`** — Whisper's signature failure is inventing fluent
  sentences during silence, and meetings are mostly silence.
- **`condition_on_previous_text=False`** — feeding the previous segment back is
  what produces the repetition loop. Costs a little coherence, buys immunity from
  the worst hallucination mode.
- **`initial_prompt`** — `build_vocabulary_prompt` joins the title, the agenda, and
  the **named** participants. This is what makes "ChromaDB" decode as ChromaDB
  rather than "chroma DB".

Model settings come from config: `large-v3`, `cuda`, `int8_float16`. The quantization
keeps large-v3 near ~2GB VRAM, which is what lets it coexist with a browser on a
4GB card.

The tab track's detected language is **forced onto** the mic track, so two halves
of one conversation cannot come out in two languages.

#### `build_vocabulary_prompt` and `_real_people`

`_real_people` is the shared filter for "a `Speaker` row that stands for an actual
human":

```python
Speaker.meeting_id == meeting_id
Speaker.source != SpeakerSource.DIARIZATION
Speaker.is_excluded.is_(False)
```

Both conditions matter, and the second is the subtle one. Two kinds of row carry a
`display_name` like `SPEAKER_02`: an unmapped cluster (deleted later in this same
job) and — the durable one — **a cluster someone ignored**, which `_mark_excluded`
moved to `MANUAL` without renaming and which nothing ever deletes.

Both inputs are read *before* the job clears the last run's rows. Counted, they
primed Whisper with "SPEAKER_00" — spending the prompt on tokens that cannot
appear — and inflated `max_speakers` on every reprocess. **Reprocessing is a
repair operation: it must hand both models what the first run did.**

### Attribution

`_attribute` is where the [two-track split](architecture.md#two-tracks-not-one)
pays out:

```python
# mic: no inference at all. It is the local user, by definition.
align(mic_segments, turns=[], default_speaker=local.display_name,
      default_source=SpeakerSource.LOCAL_TRACK)

# tab: everyone else. Diarize.
align(tab_segments, turns=_remote_speaker_turns(...),
      default_speaker=None, default_source=SpeakerSource.UNKNOWN)
```

Then `merge_tracks(mic, tab)` interleaves both by `start_ms`. Overlapping speech
survives the merge, which is correct — it really did overlap.

**`_remote_speaker_bound`** counts real remote people and returns **`roster + 1`**,
or `None` for an empty roster. The spare seat is for a late joiner or a shared
laptop; `None` leaves pyannote unconstrained rather than bounding it at 1.
Excluded speakers are deliberately not counted, so the bound does not drift across
re-runs. It is a **hint, never a guarantee** — the roster is a name source, not a
census.

If `DIARIZATION_ENABLED` is false, `_remote_speaker_turns` logs a warning and
returns `[]`, and every remote speaker comes out Unknown. That is only sensible
with no GPU and no HF token.

### Diarization

[`services/attribution/`](../backend/app/services/attribution/). The
`AttributionProvider` Protocol is named for the *job*, not for today's
implementation:

```python
def attribute(*, meeting_id: str, audio_path: Path | None = None,
              max_speakers: int | None = None) -> list[SpeakerTurn]: ...
```

#### Why a subprocess

CTranslate2 (Whisper) and torch (pyannote) both link cuDNN and **cannot both
initialise it in one process**. After Whisper has touched the GPU, torch's cuDNN
load aborts the interpreter:

```
Could not load symbol cudnnGetLibConfig. Error code 127
```

Exit 127. No traceback. Nothing to catch. Unloading does not help — the process is
already poisoned. Verified reproducible with both `cudnn64_9.dll` copies
byte-identical, so **it is not a version conflict**, and only an OS process
boundary separates two CUDA runtimes.

Cost: ~2 seconds of interpreter start on a job measured in minutes. It also means
the GPU and the CUDA context are released by the OS when the child exits, rather
than by anyone remembering to call `unload()` — which is why `PyannoteProvider.unload()`
is a documented no-op kept only for interface symmetry.

**Do not simplify this away.**

#### The protocol

```
python -m app.services.attribution.diarize_cli \
    --audio <path.wav> --out <tmp>/turns.json [--max-speakers N]
```

Fixed argv, no shell, nothing on stdin. `TIMEOUT_SECONDS = 3600` — a deadlock
guard, not a performance budget.

**Results go to a file, not stdout**, because pyannote, lightning, and speechbrain
all write chatter to the console and stdout is not a channel you can trust to
carry JSON. Progress messages go to stderr; on failure `_tail` surfaces the last
15 non-blank stderr lines, which is where the real errors live (missing token,
unaccepted licence, CUDA).

`diarize()` imports torch **inside the function**, so `--help` does not pay for it.
It raises with a remedy for each of the three ways setup goes wrong:

| Condition | Message points at |
|---|---|
| `pyannote.audio` not importable | `pip install -e ".[diarization]"` |
| No `HUGGINGFACE_TOKEN` | the config |
| `Pipeline.from_pretrained(...)` returns `None` | **the unaccepted licence** |

That last one is the hour-loser: pyannote *returns `None`* rather than raising when
the token is valid but the licence has not been accepted — and it needs both
`pyannote/segmentation-3.0` **and** `pyannote/speaker-diarization-3.1`, because the
pipeline loads segmentation internally.

`_to_device` catches any failure moving to `PYANNOTE_DEVICE`, warns, and continues
on CPU: a slow transcript beats no transcript.

### Alignment

[`services/alignment.py`](../backend/app/services/alignment.py). This is where
Whisper's words meet pyannote's turns.

**The two clocks disagree.** Whisper's word times come from cross-attention;
pyannote's boundaries come from a segmentation model. They differ by ~100–200ms
even when they agree about *who* spoke. Alignment treats that as **noise, not
signal**, in two tightly-bounded places — both deliberately too small to swallow
real speech, because over-applying either silently puts one person's words in
another's mouth.

#### `NEAREST_TURN_TOLERANCE_MS = 250`

pyannote's turns are tight around speech, so a word landing in the **seam** between
two turns overlaps neither and used to come out `UNKNOWN` mid-sentence. It now
goes to the nearest turn within the tolerance.

Past the tolerance it stays `UNKNOWN`, and that is the guard: a word in genuine
silence is likely a hallucination, and it must not be handed to whoever happened
to speak last. 250ms is far shorter than any conversational pause.

#### `MIN_RUN_MS = 300`

A word or two flipping to a neighbour and straight back is jitter, not a turn.
`_absorb_short_runs` rewrites such a run to the surrounding speaker — but **only
when enclosed by one other speaker**:

- Duration alone would eat real backchannel ("mm-hm" is short *and* real).
- Enclosure is what marks it as jitter.
- Runs at a **segment edge are left alone** — there is no enclosure to judge them
  by.

300ms is under a spoken word and a half.

#### Presenter turns are a fallback, not a peer

`_speaker_for_word` splits turns into `speaking` and `presenting`, and tries
overlap-then-nearest on **speaking first**. A screen-share is one turn spanning the
whole share; treated as a peer it would swallow every word in it. So a near-miss on
a real speaker beats a direct hit on `PRESENTER`.

#### `align()`

```python
align(segments, turns, *, default_speaker=None,
      default_source=SpeakerSource.UNKNOWN) -> list[AttributedSegment]
```

1. **Sort the turns defensively.** Both searches break early on ascending starts, so
   an unsorted timeline would not raise — it would silently attribute everything
   before the first turn to nobody.
2. A segment with no words is labelled whole, via a synthetic `Word`.
3. Otherwise: label every word → `_absorb_short_runs` over the whole segment →
   walk, flushing a new `AttributedSegment` at every label change. **A segment that
   straddles a speaker change is split.**
4. Sort the output by `start_ms`.

Smoothing needs the whole segment's labels at once, which is why it cannot be
folded into the run-building walk.

### Persisting segments

`_persist_segments` deletes the meeting's segments, **then deletes its
`DIARIZATION` speakers**, then writes fresh rows.

Clearing the clusters is not tidiness: cluster numbering is not stable across runs,
so a stale `SPEAKER_01` row could let a *fresh* cluster inherit a name that a human
checked against different audio.

> **Known trade-off, documented in the code:** a re-run discards manual per-segment
> speaker corrections made via `PATCH .../segments/{id}/speaker`.

## `run_translate`

[`services/translation.py`](../backend/app/services/translation.py). Runs only when
`needs_translation(language)` — i.e. the language is set and does not start with
`en`.

A non-English meeting is transcribed **in the language spoken**, then translated
line-by-line through the same structured-LLM provider as the minutes, which handles
names, jargon, and code-switching better than Whisper's own translate task.

- **Writes `Segment.text_en`, never over `Segment.text`.** The original is
  evidence; the minutes cite it and grounding re-reads it.
- **`CHUNK_SIZE = 80`, no overlap.** Unlike extraction, translation is a
  one-to-one line map — overlap would re-translate (possibly *differently*) text
  already held. Smaller than the extractor's 120 because the output carries a full
  second copy of the text and can crowd the token ceiling on the way out.
- **Line boundaries are load-bearing.** The prompt says so explicitly: other parts
  of the system cite lines by index, so line 12 in must be line 12 out.
  `_apply_translations` maps strictly by index and ignores out-of-range ones, so a
  bad index cannot shift the whole mapping or leak one line's text onto its
  neighbour.
- **Fails open.** If a batch returns nothing, `text_en` stays null and
  `render_transcript`'s `text_en or text` fallback yields source-language minutes
  for those lines — rather than nothing.

## The mapping gate

`_enqueue_minutes_unless_unmapped` sits between translation and minutes. If
`unmapped_cluster_count` is nonzero it **logs and returns** — a successful outcome.
Raising would light the meeting red and invite a retry, when what is needed is a
human.

`unmapped_cluster_count` and `minutes_already_owned` live in
[`services/attribution/mapping.py`](../backend/app/services/attribution/mapping.py)
rather than in `workers/pipeline.py` **so the API can import them without pulling
in torch**. That file imports only the ORM.

`minutes_already_owned` is true when a `TRANSLATE` or `MINUTES` job is `PENDING` or
`RUNNING`. It guards the double-summarise race described in
[the API reference](api-reference.md#post-meetingsmeeting_idspeakersspeaker_idresolve).
It is deliberately **not** consulted by `run_translate` itself — its own row is
`RUNNING`, so it would see itself and never hand off.

## `run_minutes`

1. **The hard gate.** `unmapped_cluster_count` nonzero → `RuntimeError`. The API
   refuses to queue this, so reaching here means something bypassed it.
2. `_minutable(segments)` drops segments belonging to `is_excluded` speakers →
   `RuntimeError` if that empties the transcript. Excluding *here* rather than in
   the extractor also removes the speaker from the roster the model is shown.
3. `extract(...)` and `summarize(...)`.
4. Write a **new version** (`previous + 1`), never an overwrite.
5. Resolve each item's `owner_name` against the meeting's speakers; parse the due
   date; write `MinutesItem` rows with their `segment_ids`.
6. Enqueue `GROUND`.

`_parse_due_date` is the one place in the pipeline that swallows an error: an
unparseable date logs `"Discarding unparseable due date %r"` and becomes `None`,
because **a wrong deadline is worse than a missing one**.

### Extraction

[`services/minutes/extractor.py`](../backend/app/services/minutes/extractor.py).
Four rules, in priority order:

1. **Every item cites its segments** — `cites` is a required schema field, so an
   uncited item is *inexpressible*.
2. **Structured, not prose.**
3. **Owners must be real** — tell the model the roster *and check the answer
   against it*.
4. **Map-reduce over long meetings.**

`CHUNK_SIZE = 120`, `CHUNK_OVERLAP = 20`. The overlap exists because an action item
is often *stated* in one sentence and *accepted* in the next, and a hard boundary
loses the owner.

`render_transcript` emits `[12] Priya: text` — **line numbers, not UUIDs**. The
model would spend tokens on UUIDs and get them wrong. `_extract_chunk` maps the
returned indices back to real segment ids and **drops out-of-range citations**: a
citation that points nowhere is worse than no citation, because it looks
checkable. An item with no resolvable citation is dropped entirely.

An owner not in the roster is dropped with a log line —
`"Dropping invented owner %r (not a participant)"`.

`format_meeting_date` renders `Tuesday, 14 July 2026 (2026-07-14)`, and **the
weekday is the whole point**: "by Friday" is unresolvable from an ISO date alone
without calendar arithmetic. With no date, the prompt says so and forbids resolving
relative deadlines at all.

`_deduplicate` keys on `(type, normalized text)` and **unions the citations** rather
than taking the first — and fills in an owner or due date a later chunk caught that
an earlier one missed, which is what the overlap is for.

The system prompt's closing instruction is the product thesis in one line:

> *Prefer fewer, well-supported items. A missed item costs the reader one manual
> note. A fabricated one gets acted on.*

### Grounding

[`services/minutes/grounding.py`](../backend/app/services/minutes/grounding.py).
`run_ground` re-reads every item against **only its cited lines**, by a call that
has not seen the rest of the meeting.

**The isolation is the point.** A verifier holding the whole transcript would
confirm "Priya owns the migration" because she volunteered forty lines later —
making the claim true and the citation wrong. We are checking the citation, not the
claim.

The prompt rejects: lines merely *consistent* with the claim; a decision that was
only discussed; an owner who did not accept *in these lines*; an inference rather
than something the lines say. And: *"Default to rejecting when you are unsure."*

- `effort="medium"`, deliberately not `high` — a narrow, well-posed question that
  must stay cheap enough that nobody is tempted to switch it off. A grounding pass
  that gets disabled is no pass at all.
- **Fails closed.** No verdict → `is_grounded=False`, `"The grounding check did not
  return a verdict."` The opposite default would let an API blip mark everything
  approved.
- `meeting_date` is passed for one specific reason: without it the verifier would
  compare `2026-07-20` against a line saying "Friday" and reject every correctly
  dated action item.
- **Items that fail are flagged, not deleted.**
- If nothing is ever rejected, the log says to spot-check: a pass that never
  rejects is either lucky or broken. `scripts/check_grounding.py` exists to prove
  it still says no — see [Development](development.md#adversarial-grounding-check).

### The LLM provider seam

[`services/minutes/providers/`](../backend/app/services/minutes/providers/). One
Protocol:

```python
def complete(*, system: str, prompt: str, schema: type[ModelT],
             model: str, effort: Effort = "high",
             max_tokens: int = 16_000) -> ModelT | None: ...
```

The schema is enforced **server-side** by the provider, not parsed hopefully out of
prose. Callers must treat `None` as "no answer" — never as "approved".

`get_provider()` reads `LLM_PROVIDER` (`groq`, `google`, `openai`, or `anthropic`;
default `groq`) and imports that provider's SDK **lazily**, so an install using one
provider does not need the others' packages. Switching is a config change, not a
code change. (Groq alone enforces the schema client-side — JSON mode + Pydantic
validation — because it has no cross-model server-side schema mode; the interface's
"no malformed output reaches a caller" guarantee still holds via a `None` on a bad
shape.)

That seam is only worth having because everything that makes the output
trustworthy — required `cites`, the roster check, grounding's isolation, chunking,
dedup — sits **above** this interface. The provider is the last and least
interesting decision.
