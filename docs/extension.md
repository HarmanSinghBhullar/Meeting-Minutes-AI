# The extension

Chrome MV3, React 18, TypeScript (strict), Vite. Source in
[`extension/src/`](../extension/src/).

## Why five contexts

MV3 forces the layout:

| Context | Why it has to exist |
|---|---|
| **Service worker** | The only thing that can call `chrome.tabCapture.getMediaStreamId`. Has **no DOM**, so it cannot hold a `MediaRecorder`, and is evicted after ~30s idle, so it cannot hold state in a variable. |
| **Offscreen document** | A DOM that outlives the worker. This is where capture actually happens. |
| **Content script** | The only context inside the meeting page, so the only one that can read the roster. |
| **Popup** | The user gesture. `getMediaStreamId` must be in its call stack. |
| **Permission page** | An offscreen document may *use* the mic but cannot *prompt* for it. This page's whole job is one `getUserMedia` call. |

`manifest.json` permissions: `tabCapture`, `offscreen`, `storage`, `activeTab`,
`scripting`. Host permissions cover `127.0.0.1:8000` plus Meet, Zoom, and Teams.
`minimum_chrome_version: "116"` — offscreen documents.

## The message protocol

All types live in [`src/lib/types.ts`](../extension/src/lib/types.ts) as
`ExtensionMessage`.

```
popup ──START_RECORDING {tabId}──> worker
popup ──STOP_RECORDING──────────> worker
popup ──GET_RECORDING_STATE─────> worker ──> RecordingState

worker ──GET_MEETING_CONTEXT────> content ──> MeetingContext
worker ──RECORDING_STARTED {startedAt}──> content
worker ──RECORDING_STOPPED──────> content ──> StoppedResult {events}
content ─SPEAKER_EVENTS {events}─> worker   (every 10s, and on stop)

worker ──OFFSCREEN_START {streamId, meetingId}──> offscreen ──> {startedAt}
worker ──OFFSCREEN_STOP─────────────────────────> offscreen ──> StopResult
worker ──OFFSCREEN_GET_STATE────────────────────> offscreen ──> OffscreenState
```

Everything the offscreen document returns is wrapped in
`Reply<T> = { ok: true, result: T } | { ok: false, error: string }`, and
`askOffscreen` unwraps it — so a failure inside the recorder surfaces as a thrown
error in the worker rather than as `undefined` three calls later.

## The service worker

[`src/background/service-worker.ts`](../extension/src/background/service-worker.ts).
Orchestration only.

**State lives in `chrome.storage.session`**, under the key `recording`, as
`{ meetingId, startedAt, tabId }`. Not a module variable — MV3 evicts the worker
after ~30 seconds of idle, and a recording lasts an hour.

### `startRecording(tabId)` — the order is load-bearing

1. `ensureOffscreenDocument()`
2. Refuse if the offscreen doc is already recording.
3. **`getMediaStreamId(tabId)`** — must happen in the gesture call stack.
4. `crypto.randomUUID()` for the meeting id; ask the content script for context.
5. **`openMeeting(...)` — before any audio.** The roster primes Whisper; posting it
   afterwards would be too late for the model that needs it.
6. `OFFSCREEN_START`. On failure, send `OFFSCREEN_STOP` (swallowed) and rethrow —
   so a half-started recorder does not linger.
7. Save the session. `startedAt` is measured **inside the offscreen document**, not
   here, because that is where `recorder.start()` actually happens and every
   speaker event is rebased against it.
8. Tell the content script to start observing.
9. **Badge**: `REC` in `#d93025`.

### `stopRecording()` — the reverse

1. `OFFSCREEN_STOP` → drains uploads. Logs
   `N chunk(s) never uploaded — the transcript will have gaps.` if any failed.
2. `RECORDING_STOPPED` → the content script returns its buffered events.
3. **Await `pendingSpeakerPosts`** — in-flight event batches must land before
   finalize enqueues the job.
4. `finalizeMeeting(meetingId)`.
5. `finally`: clear the session and the badge. Always, even on failure — a stuck
   `REC` badge with no recording is worse than a lost error.

### `currentState()`

Reconciles the two sources of truth. If the offscreen document is not recording but
a session exists, the recorder died: it warns, clears the session and badge, and
reports not-recording. The UI should never show `REC` over a recorder that is gone.

> **Note:** the offscreen document is created but **never closed** — there is no
> `chrome.offscreen.closeDocument()` call anywhere. It idles cheaply, but it is a
> deliberate-looking omission that is not documented as one.

## The recorder

[`src/offscreen/recorder.ts`](../extension/src/offscreen/recorder.ts).

```js
// tab: the remote participants
getUserMedia({ audio: { mandatory: {
    chromeMediaSource: 'tab', chromeMediaSourceId: streamId } } })

// mic: the local user
getUserMedia({ audio: { echoCancellation: true,
    noiseSuppression: true, autoGainControl: true } })
```

Two streams, two `MediaRecorder`s, **never mixed** — that is the
[clean speaker boundary](architecture.md#two-tracks-not-one) the whole attribution
design rests on.

- `mimeType: 'audio/webm;codecs=opus'`. No bitrate override.
- `TIMESLICE_MS = 5_000` — a chunk every five seconds. This is what bounds crash
  loss to the last few seconds.
- **Playback restore**: capturing a tab *mutes* it. An `AudioContext` pipes the tab
  stream back to `destination` so the user can still hear the meeting they are in.
  Without this the extension silently deafens them.
- Uploads are fired but not awaited per-chunk; each promise is tracked in
  `recording.uploads` and counted into `uploaded` / `failed`.
- `stop()` sets `active = null` first, then awaits each recorder's `stop` event (so
  the final blob passes through `ondataavailable`), then all uploads, then closes
  the tracks and the `AudioContext`.

## The content script and adapters

[`src/content/index.ts`](../extension/src/content/index.ts) detects the platform
from the hostname and gets an adapter.

```ts
interface MeetingAdapter {
  getParticipants(): Participant[];
  getTitle(): string | null;
  observe(onTurn: (turn: ObservedTurn) => void): void;  // turns emitted as they CLOSE
  disconnect(): void;                                    // emits any still-open turn
}
```

Turns arrive in **wall-clock** and are rebased to milliseconds-from-recording-start
by `rebase()`, which drops turns that ended before recording began and clamps ones
that started before it. Events buffer and flush every `FLUSH_INTERVAL_MS = 10_000`;
a failed flush **re-prepends the batch** rather than dropping it.

### What the adapters are actually for now

**Only the roster and the local-user identification are load-bearing.** The
active-speaker timeline these adapters produce is
[evaluation data](data-model.md#speaker_events) — pyannote does attribution. A
rotted `SELECTORS.speaking` is a stale-eval problem, not a wrong-minutes problem.
That is the entire point of the change.

### `meet.ts` — the only real adapter

`getParticipants()` walks `[data-participant-id]` tiles, reads a name, dedupes by
display name, and keeps the platform's participant id as `external_ref`.

**Self-detection** (`isSelfTile`) uses three signals, any of which suffices: a
`[data-self-name]` attribute on or in the tile; a match against the page's
`data-self-name`; or Meet's `(You)` marker in the label or text. `isLocalUser` is
computed **before** the `(You)` suffix is stripped. This is why the local user's
track carries their real name rather than an invented "You" — and, since the mic
track is never diarized, they are never one of the voices you are asked to name.

Speaker sampling polls every `POLL_INTERVAL_MS = 150` rather than using a
`MutationObserver` — the speaking indicator is a high-frequency class toggle, and
observing it fires far more often than sampling it. `SILENCE_HOLD_MS = 700` closes
a turn; `MIN_TURN_MS = 400` drops the flickers; a turn's `endedAt` is the last time
the speaker was *seen* speaking, not when the hold expired.

Presenter turns (`PRESENTER_HOLD_MS = 3_000`) require the indicator to sit inside a
tile, so the toolbar's own "Present now" button does not match.

`SELECTORS.speaking` is `['[data-is-speaking="true"]', '[aria-label*="speaking" i]',
'.kssMZb']`. The semantic selectors come first and the obfuscated class last —
`.kssMZb` is from a calibration run on 2026-07-16, and its predecessors `.BlxGDf`
and `.wnrUse.IisKdb` both rotted. **That rot is the whole story of this project's
attribution design** ([README](../README.md)).

`checkHealth` warns once, after a 20s grace, if tiles exist but nothing has ever
looked like speaking — telling the user to run `__meetCalibrate()`.

#### `__meetCalibrate()`

Exposed on `window` in the content script. Run it in the Meet tab's console when
`SELECTORS.speaking` rots:

```js
__meetCalibrate()   // then: speak for 6s, stay silent for 6s, follow the banner
```

It samples every tile's classes and attributes across both phases, filters out
volatile attributes and framework classes, and prints the signals that were present
while speaking (`ratio >= 0.6`) and absent while silent (`<= 0.25`). Paste the
winner into `SELECTORS.speaking`.

### `zoom.ts` and `teams.ts` are stubs

`getParticipants()` returns `[]`, `observe()` is a no-op. The backend falls back to
pyannote, which is the sole attribution source anyway — so the cost of a stub
adapter is a mapping step with no candidate names, not a broken meeting.

Zoom's docstring notes the real limit: only the *browser* client is reachable, and
the desktop app produces no DOM at all.

## The API client

[`src/lib/api.ts`](../extension/src/lib/api.ts). `BASE_URL =
'http://127.0.0.1:8000/api/v1'`. The snake_case↔camelCase mapping lives **only**
here — `RawMeeting` in, `Meeting` out. No auth headers.

Two behaviours worth knowing:

- **`getMinutes` maps 404 → `null`.** "No minutes yet" is a normal state, not an
  error.
- **`deleteMeeting` treats 404 as success.** The row is gone either way.
- `regenerateMinutes` surfaces the API's `detail` string via `errorMessage`, which
  is how the 409 gate message reaches the user verbatim.

## The UI

### Popup — [`src/popup/App.tsx`](../extension/src/popup/App.tsx)

Start/stop, a mic-permission nudge, and the ten most recent meetings. A failed
`listMeetings` swallows to `[]` — a backend that is not running is a normal state
for a local tool, not an error banner.

The mic check is `navigator.permissions.query({ name: 'microphone' })`, and on
failure it assumes **granted**: if we cannot tell, do not nag.

While recording it shows, in red:

> **Recording. Everyone in the meeting should know.**

That notice and the `REC` badge are **product requirements, not disclaimers**.
Recording without telling participants is illegal in two-party-consent
jurisdictions. Do not quietly remove them.

### Meeting page — [`src/meeting/`](../extension/src/meeting/)

One page, two views, routed on `?id=`:

- **No id** → [`Dashboard.tsx`](../extension/src/meeting/Dashboard.tsx): every
  meeting, with open / rename / delete / map speakers / regenerate. Polls every 4s
  while any job is pending. Rename and delete are **optimistic**, restoring the
  previous state on failure. Titles are plain `<a href="?id=...">` links, so they
  are bookmarkable.
- **An id** → [`Meeting.tsx`](../extension/src/meeting/Meeting.tsx): attendance,
  minutes, transcript, and the mapping UI. One `Promise.all` over four endpoints;
  polls every 3s while jobs are pending.

The **transcript is grouped into one collapsible `<details>` per wall-clock
minute**, bucketed on each segment's `startMs` so no line appears twice and the
counts do not lie. A minute nobody spoke in gets no group — empty rows would pad
out a long silence and say nothing. Each collapsed group carries what a reader
needs to decide whether to open it: the range, the line count, and who spoke.
The first opens by default (a wall of collapsed rows reads as an empty
transcript), and *Expand all* exists because Ctrl+F cannot find text inside a
closed group.

**Transcript search** is the other half of that bargain. Typing narrows each
group to its matching lines, drops the groups with none, opens what is left —
a hit inside a collapsed group is a hit you cannot read — and marks the runs.
The **speaker's name matches too**: "what did Harry say" is at least as common a
question as "where was ChromaDB mentioned", and the name is on screen, so
typing it has every reason to work. While a search is active a group reports
`3 of 11` rather than `11 lines`, and the header counts *lines*, not hits — a
line matching on both its speaker and its words highlights twice, and a count
that says "matches" over a different number is exactly the sort of small lie
this page is in the business of not telling.

`StatusBadge` precedence: running → `{type}…`; failed → `{type} failed`; **unmapped
> 0 → `Needs speakers (N)`**; ground succeeded → `Minutes ready`; else
`Transcribed`. The unmapped count is derived straight off `meeting.speakers`
(`source === 'diarization'`) with no extra request — the
[gate's schema design](data-model.md#the-mapping-gate-in-the-schema) paying out in
the UI.

Attendance filters out clusters and excluded speakers, sorts the local user first,
and tags them `Name (You)`. An unnamed voice is not attendance.

Each minutes item can show **the lines it came from** — one click from any claim to
its evidence. An item with `isGrounded === false` renders struck with *"Not
supported by its own citation"*, because
[failed items are flagged, not deleted](pipeline.md#grounding).

### Speaker mapping — [`SpeakerMapping.tsx`](../extension/src/meeting/SpeakerMapping.tsx)

Sits **above** the minutes on the meeting page, because it is what is blocking them.

Each cluster leads with its **longest** lines, rendered as a blockquote *before* the
control — evidence first, then the question. Longest-talking cluster first. Three
answers: a roster candidate, `Someone else — type a name…`, or `Not a person
(shared video, noise)`.

## Build

`npm run build` → `tsc --noEmit && vite build`, output in `extension/dist`, loaded
unpacked at `chrome://extensions`. `npm run dev` is `vite build --watch`.

Six Rollup entries: `service-worker`, `content`, `offscreen`, `popup`, `permission`,
`meeting`. `entryFileNames` pins the worker and content script to the exact paths
`manifest.json` names — a hashed filename there would simply not load. A small
plugin copies `manifest.json` into `dist` on `closeBundle`.

`tsconfig.json` is strict, plus `noUncheckedIndexedAccess` and
`exactOptionalPropertyTypes` — the latter is why `...(x ? { k: x } : {})` appears
everywhere instead of `k: x ?? undefined`.

> **Two inconsistencies in the build setup.** (1) The `vite.config.ts` header
> comment explains that the content-script entry uses `inlineDynamicImports`,
> because a Rollup-emitted `import` statement is a syntax error in a content script
> and would make it silently never run — but **no such option is actually in the
> config**. `src/content/index.ts` does import `./adapters` at runtime, so the
> split it warns about is possible in principle. (2) `package.json` defines an
> `eslint` script, but there is no eslint dependency or config in the extension
> directory.
