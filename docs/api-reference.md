# API reference

FastAPI, mounted at **`/api/v1`**. Interactive docs are at
`http://127.0.0.1:8000/docs` while the server is running — they are generated from
the same schemas and are the authority on request shapes.

There is **no authentication**. The API binds to `127.0.0.1` and CORS is limited
to `chrome-extension://.*` by default. This is a local-first tool; the day it is
not, that is the first thing that has to change.

All request and response bodies are JSON unless noted. Field names on the wire are
`snake_case`; the extension maps to `camelCase` in exactly one place,
[`src/lib/api.ts`](../extension/src/lib/api.ts).

## Contents

- [Health](#health)
- [Recordings](#recordings) — the extension's write path
- [Meetings](#meetings) — the dashboard's read/manage path
- [Speakers](#speakers) — the mapping gate
- [Minutes](#minutes)
- [Q&A](#qa)

---

## Health

### `GET /health`

Outside the `/api/v1` prefix. Returns `{"status": "ok"}`. No database check — it
answers "is the process up", nothing more.

---

## Recordings

Prefix `/api/v1/recordings`. This is the extension's write path, and the order of
these calls is load-bearing: the roster must exist before the audio, because the
names prime Whisper's decoder.

### `POST /recordings/meetings` → `201`

Opens a meeting. **Idempotent on the client-supplied `id`** — if the meeting
already exists it is returned unchanged, still `201`. The extension generates the
UUID with `crypto.randomUUID()`, which is what makes a retry safe: a network blip
on this call must not produce two meetings.

**Body** — `MeetingCreate`:

| Field | Type | Default |
|---|---|---|
| `id` | `UUID` | **required** — client-generated |
| `title` | `string \| null` | `null` |
| `platform` | `Platform` | `"other"` |
| `meeting_url` | `string \| null` | `null` |
| `agenda` | `string \| null` | `null` |
| `speakers` | `SpeakerIn[]` | `[]` |

`SpeakerIn`: `{ display_name: string, external_ref?: string | null, is_local_user?: bool }`

**Effects:** creates the `Meeting` with `started_at = now()`; one `Speaker` per
entry with `source = DOM`; and two `Recording` rows (`mic`, `tab`), both
`mime_type = "audio/webm"`.

**Response:** `MeetingOut`. No error codes.

### `POST /recordings/meetings/{meeting_id}/chunks` → `202`

Appends one `MediaRecorder` chunk. Called every 5 seconds per track during a
recording — the hottest endpoint in the system.

**`multipart/form-data`:**

| Part | Type |
|---|---|
| `track` | form field, `Track` (`mic` \| `tab`) |
| `chunk` | file |

**Effects:** `storage.append_chunk` opens the track's `.webm` in `"ab"` and writes.
WebM is a streaming container, so appending timesliced chunks yields a valid file
— which is why a browser crash mid-meeting costs the last few seconds, not the
recording. Then increments `chunk_count` and refreshes `source_path` /
`size_bytes`.

**Response:** `{"chunk_count": int}`.

**Errors:** `404` — `"No {track} track for that meeting."`

### `POST /recordings/meetings/{meeting_id}/participants` → `202`

The roster as the adapter currently reads it, posted every 5s during a recording
and once more on stop.

**Body** — `SpeakerBatch`: `{ participants: SpeakerIn[] }`, where `SpeakerIn` is
`{ display_name: string, external_ref?: string | null, is_local_user?: bool }`.
The **whole list**, not a delta: the client stays dumb and the endpoint decides
what is new.

**Why it exists:** `open_meeting` reads the roster once, inside the call stack of
the click that starts recording, and that read is wrong in two ordinary cases —
the tile grid may not have rendered yet (the meeting then has *nobody* in it for
its whole duration), and anyone who joins later never appears at all. Both failed
silently: no attendance, and no candidate offered for their own voice at the
mapping panel.

**Effects:** matches each participant on `external_ref` first, then
`display_name`, and inserts a `DOM` speaker for anyone unmatched. Backfills a
missing `external_ref` onto a row matched by name — that is how someone stays one
row across a reconnect, and it is the only field this endpoint ever updates.

**Additive only.** It never renames, never deletes, and never touches a row a
human has ruled on at the mapping panel. The roster arrives on a timer; a person's
answer does not, and a timer must not be able to overwrite it. A participant who
renames mid-call keeps the name we first saw — matched by id, so they are not
duplicated, but not renamed either.

**Response:** `{"created": int}` — how many rows were new.

**Errors:** `404` — `"Meeting not found."`

### `POST /recordings/meetings/{meeting_id}/speaker-events` → `202`

Batched active-speaker and presenter turns from the content script, flushed every
10s and again on stop.

**Body** — `SpeakerEventBatch`: `{ events: SpeakerEventIn[] }`, where
`SpeakerEventIn` is `{ speaker_name: string, speaker_external_ref?: string | null,
start_ms: int, end_ms: int, source?: SpeakerSource = "dom" }`. Times are
milliseconds **from the start of the recording**, rebased in the content script.

**Effects:** resolves each `speaker_name` against the meeting's speakers by
`display_name`, creating a `DOM` speaker if unseen; inserts a `SpeakerEvent` per
event.

**Response:** `{"accepted": int}`. No error codes — it does not verify the meeting
exists.

> These rows are **evaluation data only**. Nothing reads them for attribution; see
> [`speaker_events`](data-model.md#speaker_events).

### `POST /recordings/meetings/{meeting_id}/finalize` → `200`

Ends the recording and starts the pipeline.

**Body** — `MeetingFinalize`: `{ ended_at?: datetime | null }`.

**Effects:** sets `ended_at`; sets `is_finalized = True` on recordings **with
`chunk_count > 0`** only (a track nobody spoke on should not make the worker look
for audio that is not there); enqueues a `TRANSCRIBE` job.

**Response:** `MeetingOut`. **Errors:** `404` — `"Meeting not found."`

This returns immediately. Everything expensive happens in the worker.

---

## Meetings

Prefix `/api/v1/meetings`.

### `GET /meetings` → `200`

Query: `limit: int = 50`. Returns `MeetingOut[]`, newest first (`created_at DESC`).
Note there is no trailing slash.

### `GET /meetings/{meeting_id}` → `200`

Returns `MeetingOut` — including `speakers`, `recordings`, and `jobs`, which is
what lets the meeting page derive its whole status (and "Needs speakers (n)") from
one request.

**Errors:** `404` — `"Meeting not found."`

### `PATCH /meetings/{meeting_id}` → `200`

Rename. **Body** — `MeetingUpdate`: `{ title: string }`, stripped, 1–512 chars
(enforced by `StringConstraints`, so an empty title is a `422`, not a silent
no-op).

**Response:** `MeetingOut`. **Errors:** `404`.

### `DELETE /meetings/{meeting_id}` → `204`

Deletes the row (Postgres cascades to segments, speakers, minutes, jobs,
recordings) and **then** calls `storage.delete_meeting` to remove the audio
directory.

The order matters: the row goes first. If file deletion fails, the meeting is
still gone from the user's view and the leftover is a stray directory — the
recoverable failure. The reverse leaves a meeting that lists in the dashboard and
explodes when opened.

**Errors:** `404`.

### `GET /meetings/{meeting_id}/transcript` → `200`

Returns `SegmentOut[]` ordered by `start_ms`. **No 404** — an unknown or
not-yet-transcribed meeting returns `[]`, because "no transcript yet" is a normal
state, not an error.

### `PATCH /meetings/{meeting_id}/segments/{segment_id}/speaker` → `200`

Reassign one segment. **Body** — `SegmentCorrection`: `{ speaker_id: UUID }`.
Sets `speaker_source = MANUAL`.

**Response:** `SegmentOut`. **Errors:** `404` — `"Segment not found."`, also when
the segment belongs to a different meeting (checked, rather than trusting the path).

> A **reprocess discards this**: `_persist_segments` deletes and rewrites the
> meeting's segments. Known trade-off, documented in the pipeline.

---

## Speakers

Prefix `/api/v1/meetings`. This is [the mapping gate](architecture.md#the-mapping-gate).

### `GET /meetings/{meeting_id}/speaker-mapping` → `200`

Everything the mapping UI needs, in one request.

**Response** — `SpeakerMappingOut`:

| Field | Type | Notes |
|---|---|---|
| `clusters` | `SpeakerClusterOut[]` | unnamed voices, **longest-talking first** |
| `candidates` | `SpeakerOut[]` | roster speakers, alphabetical |
| `minutes_queued` | `bool` | `false` here; meaningful on `resolve` |

`SpeakerClusterOut`: `{ id, display_name, segment_count, total_ms, samples: string[] }`

**`samples` are the three *longest* lines, not the first three** (`SAMPLE_COUNT = 3`,
truncated at `SAMPLE_MAX_CHARS = 160`). A call opens with "hi" and "can you hear
me", which identify nobody; "I'll take the ChromaDB migration" identifies a
colleague instantly.

`candidates` excludes clusters, the local user (never diarized, so never a
cluster's answer), and excluded speakers.

**Errors:** `404` — `"Meeting not found."`

### `POST /meetings/{meeting_id}/speakers/{speaker_id}/resolve` → `200`

Resolve one cluster. **Exactly one** of three fields must be set — enforced by a
model validator, so ambiguity is a `422` and never a guess.

**Body** — `SpeakerResolution`:

| Field | Meaning | Effect |
|---|---|---|
| `target_speaker_id: UUID` | "this voice is Priya, who is on the roster" | **Merge**: reassigns the cluster's segments to the target and **deletes the cluster row** |
| `display_name: string` | "this voice is Deepak, who dialled in" | **Name in place**: `display_name`, `source = MANUAL` |
| `ignore: true` | "this is a shared video / hold music" | **Exclude**: `source = MANUAL`, `is_excluded = True` |

All three mark the affected segments `speaker_source = MANUAL`.

Merge **deletes** rather than renames because renaming would leave two Priyas —
two rows the roster then offers as two different candidates. The segments survive
the delete via `ON DELETE SET NULL`… which is not relied on: they are repointed to
the target first.

Ignore **keeps** the row and the segments. The words stay in the transcript
(something was said); `_minutable` withholds them from the minutes (nobody said
it).

**Then, in the same transaction:** if `unmapped_cluster_count == 0` **and** no
`TRANSLATE`/`MINUTES` job is already `PENDING`/`RUNNING`, enqueue `MINUTES` and
set `minutes_queued: true`.

That second condition guards a real race: a user can finish naming speakers *while
translation is still running*, and both doors would queue minutes — two LLM runs,
two bills, two versions circulated.

**Response:** the whole `SpeakerMappingOut` after the change, so the UI never has
to re-derive state it can be handed.

**Errors:**

| Code | When |
|---|---|
| `404` | Meeting not found; speaker not found or not in this meeting |
| `409` | The speaker is not an unnamed cluster — `"'{name}' is not an unnamed cluster; it has already been identified."` (a double-submit) |
| `400` | Merge target is not in this meeting; merging a cluster into itself; merging a cluster into another cluster |
| `422` | Zero or more than one resolution field set |

---

## Minutes

Prefix `/api/v1/meetings`.

### `GET /meetings/{meeting_id}/minutes` → `200`

Returns the **highest `version`** (`ORDER BY version DESC LIMIT 1`).

**Response** — `MinutesOut`: `{ id, meeting_id, summary, version, items: MinutesItemOut[] }`

`MinutesItemOut`: `{ id, type, text, owner_speaker_id, due_date, segment_ids,
is_grounded, grounding_note }`

`summary` is newline-separated bullets; the viewer splits on `\n`. `is_grounded`
is three-valued — see [`minutes_items`](data-model.md#minutes_items).

**Errors:** `404` — `"No minutes yet. They are generated after transcription
completes."` The extension treats this 404 as `null` rather than an error, because
it is a normal state.

### `POST /meetings/{meeting_id}/minutes/regenerate` → `202`

Enqueues a `MINUTES` job. Use after correcting speakers, or to A/B a provider.

**Response:** `JobOut`.

**Errors:** `409` while any cluster is unmapped:

> *"N speakers in this meeting still need identifying. Map them to participants
> first — minutes that credit 'SPEAKER_01' are worse than none."*

The worker's `run_minutes` raises on the same condition. The API refusing to queue
is the polite door; the worker raising is the lock — a guarantee enforced only at
the edge is not enforced.

---

## Q&A

### `POST /api/v1/qa` → **`200`**

**Body** — `QuestionIn`: `{ question: string, top_k: int = 8 }`, where `top_k`
is 1–20. Response is `AnswerOut` (`{ answer, citations: CitationOut[] }`).

`meeting_id` is optional: the meeting chat sends it so retrieval cannot leak in
similarly worded evidence from a different call; omitting it searches every
indexed meeting.

The route retrieves overlapping transcript windows from Chroma and asks the
configured structured LLM to answer only from those windows. It returns an
answer only when the model names valid evidence-window citations; otherwise the
answer explicitly says the evidence was insufficient and `citations` is empty.
Each citation carries the meeting id, title, participant(s), timestamp, and the
retrieved text window.

**Errors:** `503` if the optional RAG dependencies are not installed. Install
them with `pip install -e ".[rag]"`.

---

## Status codes at a glance

| Code | Where it comes from |
|---|---|
| `200` | Reads, finalize, resolve, rename, segment correction |
| `201` | `POST /recordings/meetings` (including the idempotent hit) |
| `202` | Chunk upload, speaker events, minutes regenerate |
| `204` | `DELETE /meetings/{id}` |
| `400` | Invalid merge target |
| `404` | Missing meeting / speaker / segment / minutes; wrong-meeting scoping |
| `409` | Already-resolved cluster; regenerate while unmapped |
| `422` | Pydantic validation — including "exactly one of" on a resolution |
| `503` | RAG optional dependencies unavailable |
