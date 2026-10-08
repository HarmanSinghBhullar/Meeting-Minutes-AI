# Data model

PostgreSQL, via SQLAlchemy 2.0 declarative mapping. Models live in
[`backend/app/db/models/`](../backend/app/db/models/).

**Segments are the spine.** Every feature downstream — minutes, citations,
translation, the mapping UI, and eventually RAG — is a read of the `segments`
table. Everything else exists to produce it or to annotate it.

## Shared mixins

Defined in [`db/base.py`](../backend/app/db/base.py). Every model below inherits
`Base, UUIDPrimaryKey, Timestamps`, so every table has these three columns:

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID` (`as_uuid=True`) | Primary key, `default=uuid.uuid4` |
| `created_at` | `TIMESTAMPTZ` | `server_default=now()`, not null |
| `updated_at` | `TIMESTAMPTZ` | `server_default=now()`, `onupdate=now()`, not null |

UUIDs rather than serial ids because the **extension generates the meeting id**
before the meeting exists server-side, which is what makes
`POST /recordings/meetings` idempotent on retry.

## Enums

All enum columns are declared `native_enum=False` — they are stored as `VARCHAR`
with a `CHECK` constraint rather than as Postgres enum types, because adding a
value to a native enum is a migration that cannot run inside a transaction on
older Postgres and is awkward to reverse. **The member *name* (uppercase) is what
is persisted**, not the value.

From [`db/models/enums.py`](../backend/app/db/models/enums.py):

| Enum | Members |
|---|---|
| `Platform` | `MEET="meet"`, `ZOOM="zoom"`, `TEAMS="teams"`, `OTHER="other"` |
| `Track` | `MIC="mic"`, `TAB="tab"`, `MIXED="mixed"` |
| `SpeakerSource` | `LOCAL_TRACK="local_track"`, `DOM="dom"`, `PRESENTER="presenter"`, `DIARIZATION="diarization"`, `MANUAL="manual"`, `UNKNOWN="unknown"` |
| `JobType` | `NORMALIZE`, `TRANSCRIBE`, `DIARIZE`, `ALIGN`, `TRANSLATE`, `MINUTES`, `GROUND`, `INDEX` |
| `JobStatus` | `PENDING="pending"`, `RUNNING="running"`, `SUCCEEDED="succeeded"`, `FAILED="failed"` |
| `MinutesItemType` | `DECISION`, `ACTION_ITEM`, `OPEN_QUESTION`, `RISK`, `TOPIC` |

`Track.MIXED` is reserved and unused — nothing mixes the tracks today, and the
[two-track split](architecture.md#two-tracks-not-one) is the reason.

Five `JobType` members have handlers (`TRANSCRIBE`, `TRANSLATE`, `MINUTES`,
`GROUND`, `INDEX`). `NORMALIZE`, `DIARIZE`, and `ALIGN` are stages *inside* the
transcribe job. `GROUND` queues `INDEX`, which refreshes the derived Chroma
windows for the meeting.

### `SpeakerSource` is load-bearing

It is not a decorative provenance tag. Three of its values drive behaviour:

- **`DIARIZATION` on a `Speaker` row means "this voice has no name yet."** That
  *is* the mapping gate — see [the gate](#the-mapping-gate-in-the-schema) below.
- **`LOCAL_TRACK`** is what the mic track's segments get, with no inference at
  all.
- **`PRESENTER`** on a turn makes it a *fallback* in alignment rather than a peer:
  a screen-share is one turn spanning the entire share and would otherwise
  swallow every word in it.

## Tables

### `meetings`

| Column | Type | Null | Default |
|---|---|---|---|
| `title` | `VARCHAR(512)` | yes | — |
| `platform` | `Platform` | no | `OTHER` |
| `meeting_url` | `TEXT` | yes | — |
| `started_at` | `TIMESTAMPTZ` | yes | — |
| `ended_at` | `TIMESTAMPTZ` | yes | — |
| `source_language` | `VARCHAR(8)` | yes | ISO-639-1, detected by Whisper |
| `agenda` | `TEXT` | yes | — |

Relationships, **all `cascade="all, delete-orphan"`**: `recordings`, `speakers`,
`speaker_events`, `segments`, `minutes`, `jobs`. Deleting a meeting is meant to
take everything with it — `DELETE /meetings/{id}` relies on this, then removes
the audio directory separately.

`agenda` earns its keep: it feeds Whisper's `initial_prompt`, and without it
"ChromaDB" reliably decodes as "chroma DB".

### `recordings`

One row per track per meeting.

| Column | Type | Null | Default |
|---|---|---|---|
| `meeting_id` | `UUID` FK → `meetings.id` `ON DELETE CASCADE`, indexed | no | — |
| `track` | `Track` | no | — |
| `source_path` | `TEXT` | yes | the appended `.webm` |
| `normalized_path` | `TEXT` | yes | the 16kHz mono `.wav` |
| `mime_type` | `VARCHAR(64)` | yes | — |
| `duration_seconds` | `FLOAT` | yes | from `ffprobe` |
| `size_bytes` | `INTEGER` | yes | — |
| `chunk_count` | `INTEGER` | no | `0` |
| `is_finalized` | `BOOLEAN` | no | `False` |

`is_finalized AND chunk_count > 0 AND source_path` is the filter `run_transcribe`
uses to decide there is anything to transcribe. A track that was never spoken on
still gets a row; it just never finalizes.

> **Known gap:** there is no unique constraint on `(meeting_id, track)`, even
> though the chunk-upload route does `scalar_one_or_none()` on that pair and
> would raise if a duplicate ever existed.

### `segments`

The spine. One row per attributed span of speech.

| Column | Type | Null | Default |
|---|---|---|---|
| `meeting_id` | `UUID` FK → `meetings.id` CASCADE, indexed | no | — |
| `recording_id` | `UUID` FK → `recordings.id` CASCADE, indexed | no | which track it came from |
| `speaker_id` | `UUID` FK → `speakers.id` **`ON DELETE SET NULL`** | yes | — |
| `index` | `INTEGER` | no | position within the meeting |
| `start_ms` | `INTEGER` | no | — |
| `end_ms` | `INTEGER` | no | — |
| `text` | `TEXT` | no | as spoken, never overwritten |
| `text_en` | `TEXT` | yes | English rendering, if translated |
| `language` | `VARCHAR(8)` | yes | — |
| `words` | `JSONB` | yes | `[{"word","start","end","probability"}]` |
| `speaker_source` | `SpeakerSource` | no | `UNKNOWN` |
| `avg_logprob` | `FLOAT` | yes | Whisper confidence |
| `no_speech_prob` | `FLOAT` | yes | high + fluent text = hallucination |

Index: `ix_segments_meeting_start` on `(meeting_id, start_ms)` — every read of
this table is "one meeting, in order".

`speaker_id` is `SET NULL` rather than `CASCADE` **on purpose**: merging a cluster
into a roster speaker deletes the cluster row, and the segments must survive that.
Losing the transcript because someone corrected a name would be an absurd outcome.

`text` and `text_en` are separate columns, not one column overwritten, because
**the original is the evidence** — the minutes cite it and the grounding pass
re-reads it.

`words` holds Whisper's word-level timings. It is what alignment consumes, and it
is why `word_timestamps=True` is not optional.

### `speakers`

Both real participants and anonymous voice clusters live here. Which one a row is
depends entirely on `source`.

| Column | Type | Null | Default |
|---|---|---|---|
| `meeting_id` | `UUID` FK → `meetings.id` CASCADE, indexed | no | — |
| `display_name` | `VARCHAR(256)` | no | a name, or `SPEAKER_01` |
| `source` | `SpeakerSource` | no | `UNKNOWN` |
| `external_ref` | `VARCHAR(256)` | yes | the platform's participant id |
| `is_local_user` | `BOOLEAN` | no | `False` |
| `is_excluded` | `BOOLEAN` | no | `False` |

#### The mapping gate, in the schema

`source == DIARIZATION` **is** the unmapped state. There is no status column, and
that is the design:

| Row looks like | `source` | `is_excluded` | Meaning |
|---|---|---|---|
| `Priya` | `DOM` | false | Roster participant, a mapping candidate |
| `Harman` | `DOM` / `LOCAL_TRACK` | false | The local user (`is_local_user`) |
| `SPEAKER_01` | `DIARIZATION` | false | **Unnamed. The gate is closed.** |
| `Priya` | `MANUAL` | false | A cluster that was named or merged |
| `SPEAKER_02` | `MANUAL` | **true** | A cluster someone marked *not a person* |

Every resolution path moves the row off `DIARIZATION` — merge *deletes* it,
rename and ignore rewrite it to `MANUAL` — so `unmapped_cluster_count` reaching
zero *is* the mapping being complete. Nothing to keep in step, nothing to
backfill.

**The last row in that table is subtler than it looks and has bitten before.** An
ignored cluster keeps its `SPEAKER_02` name forever: `_mark_excluded` moves it to
`MANUAL` without renaming, and nothing ever deletes it. So a `display_name` like
`SPEAKER_02` does *not* imply an unnamed cluster. This is why `_real_people`
filters on `source != DIARIZATION` **and** `is_excluded is False` — counted, these
rows primed Whisper's `initial_prompt` with "SPEAKER_00" and inflated
`max_speakers` on every reprocess.

`is_excluded` withholds a speaker from the *minutes* and from *attendance*, but
their words stay in the transcript. A shared video is not a participant; it is
also not nothing.

### `speaker_events`

Kept, but **no longer drives attribution**.

| Column | Type | Null | Default |
|---|---|---|---|
| `meeting_id` | `UUID` FK → `meetings.id` CASCADE, indexed | no | — |
| `speaker_id` | `UUID` FK → `speakers.id` **CASCADE**, indexed | no | — |
| `start_ms` | `INTEGER`, indexed | no | — |
| `end_ms` | `INTEGER` | no | — |
| `source` | `SpeakerSource` | no | `DOM` |

The Meet adapter still records active-speaker and presenter turns here. They are
**evaluation data** — the DOM timeline the diarizer can be scored against — and
nothing reads them for correctness. A rotted `SELECTORS.speaking` is a stale-eval
problem now, not a wrong-minutes problem, which was the entire point of the
change.

`CASCADE` here rather than `SET NULL` because an event with no speaker is
meaningless, unlike a segment.

### `minutes`

| Column | Type | Null | Default |
|---|---|---|---|
| `meeting_id` | `UUID` FK → `meetings.id` CASCADE, indexed | no | — |
| `summary` | `TEXT` | yes | newline-separated bullets |
| `model` | `String(128)` | yes | which model wrote them |
| `version` | `INTEGER` | no | `1` |

**Versioned, not overwritten.** Regenerating writes `version = previous + 1`;
reads take `ORDER BY version DESC LIMIT 1`. Minutes get circulated, and being
able to see what the last run said — after a speaker correction, or a provider
switch — is worth the rows.

`model` is stored so an A/B on the eval set is answerable after the fact.

### `minutes_items`

| Column | Type | Null | Default |
|---|---|---|---|
| `minutes_id` | `UUID` FK → `minutes.id` CASCADE, indexed | no | — |
| `type` | `MinutesItemType` | no | — |
| `text` | `TEXT` | no | — |
| `owner_speaker_id` | `UUID` FK → `speakers.id` **`SET NULL`** | yes | — |
| `due_date` | `DATE` | yes | — |
| `segment_ids` | `UUID[]` | no | `[]` — **the citation** |
| `is_grounded` | `BOOLEAN` | yes | `NULL` = not checked; `False` = unsupported |
| `grounding_note` | `TEXT` | yes | why it was rejected |

`segment_ids` is the citation, and it is what makes every claim one click from
the line behind it.

`is_grounded` is **three-valued and needs to be**: `NULL` means the grounding pass
has not run, `False` means it ran and the item is not supported by its own cited
lines. Collapsing those into a boolean would make "not yet checked" and "failed
the check" indistinguishable, and the UI renders them very differently.

Failed items are **flagged, not deleted** — silently dropping them would hide the
fact that the extractor produced them.

### `jobs`

| Column | Type | Null | Default |
|---|---|---|---|
| `meeting_id` | `UUID` FK → `meetings.id` CASCADE, indexed | no | — |
| `type` | `JobType` | no | — |
| `status` | `JobStatus`, **indexed** | no | `PENDING` |
| `progress` | `INTEGER` (0–100) | no | `0` |
| `attempts` | `INTEGER` | no | `0` |
| `error` | `TEXT` | yes | truncated to 4000 chars |
| `started_at` | `TIMESTAMPTZ` | yes | — |
| `finished_at` | `TIMESTAMPTZ` | yes | — |

`status` is indexed because `claim_next` polls on it every 2 seconds, forever.

`error` is kept on the row rather than only logged, because the extension shows
it: a meeting that failed should say why on the page the user is already looking
at, not in a terminal they do not have open.

## Entity relationships

```
Meeting 1─┬─* Recording      (mic, tab)
          ├─* Speaker  ──┐   (roster + clusters)
          ├─* SpeakerEvent   (eval data only)
          ├─* Segment ───┘   speaker_id ON DELETE SET NULL
          ├─* Minutes ──* MinutesItem
          │                  owner_speaker_id ──> Speaker (SET NULL)
          │                  segment_ids: UUID[] ──> Segment (no FK)
          └─* Job
```

`MinutesItem.segment_ids` is a `UUID[]`, not a join table. It is a citation — read
whole, always, and never queried in the other direction ("which items cite this
segment?" is not a question anything asks). A join table would buy referential
integrity for a list that is regenerated wholesale anyway.

## Migrations

Two, linear, in [`backend/alembic/versions/`](../backend/alembic/versions/):

| Revision | Down revision | What |
|---|---|---|
| `2baf545b9e3e` | `None` | Initial schema — all eight tables and their indexes |
| `7c41a0d9e3b2` | `2baf545b9e3e` | Adds `speakers.is_excluded` (current head) |

`7c41a0d9e3b2` adds the column with `server_default=sa.false()` and then
immediately drops the default — the existing rows need a value, but from then on
the application owns it.

Note that the **tests do not use migrations**: `conftest.py` builds the schema
with `Base.metadata.create_all`, deliberately, so tests assert against the models
as written and a missing migration cannot hide behind a passing suite. The
trade-off is that only running `alembic upgrade head` on a real database catches
migration drift — which is exactly how the gap below was found.

> **Known drift:** the initial migration's `CHECK` constraint for `SpeakerSource`
> lists `'LOCAL_TRACK', 'DOM', 'DIARIZATION', 'MANUAL', 'UNKNOWN'` — **`PRESENTER`
> is missing**, in all three places it appears (`speakers.source`,
> `segments.speaker_source`, `speaker_events.source`). `PRESENTER` is reachable:
> `SpeakerEventIn.source` accepts it and the speaker-events route writes it
> straight through. On a database built from these migrations, posting
> `"source": "presenter"` passes Pydantic validation and then fails the check
> constraint on insert. It needs a migration.
