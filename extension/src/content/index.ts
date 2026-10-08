/**
 * Content script: watches the meeting UI and reports who is speaking.
 *
 * The adapter does the platform-specific reading. This file owns the two things
 * that are the same on every platform: converting wall-clock turns onto the
 * recording's clock, and getting them to the backend.
 *
 * ## The clock
 *
 * The adapter reports turns in wall-clock time, because that is all it can know.
 * Whisper reports words in seconds from the start of the audio. Those two only
 * line up if we subtract the moment recording began — so `recordingStartedAt` is
 * the pivot the entire attribution feature turns on. Get it wrong by two seconds
 * and every speaker label slides two seconds out of place, which is worse than
 * having no labels at all, because it is wrong rather than absent.
 */

import type {
  ExtensionMessage,
  MeetingContext,
  Participant,
  Platform,
  Settings,
  SpeakerEvent,
  StoppedResult,
} from '@/lib/types';
import { getAdapter, type ObservedTurn } from './adapters';
import { RecordPanel } from './record-panel';

/** How often buffered turns are flushed to the service worker. */
const FLUSH_INTERVAL_MS = 10_000;

/**
 * How often the roster is re-read.
 *
 * The roster used to be read exactly once, in the call stack of the click that
 * starts recording, and that read was treated as the meeting's cast list. It is
 * wrong in both directions: the tile grid may not have rendered yet, in which
 * case the meeting has *nobody* in it for its whole duration; and anyone who
 * joins later never appears at all, which is not an edge case — it is how most
 * meetings begin.
 *
 * Re-reading costs a `querySelectorAll` and a few string reads, so the interval
 * is set by how quickly a newcomer should show up rather than by expense. Five
 * seconds is far finer than people actually join.
 */
const ROSTER_INTERVAL_MS = 5_000;

/**
 * How often we check whether the user is in a call, for the auto-record nudge and
 * auto-stop. Coarser than the roster poll — joining and leaving are human-scale
 * events, and a couple of seconds' latency on either is imperceptible.
 */
const CALL_DETECT_INTERVAL_MS = 3_000;

/**
 * Consecutive same-reading ticks required before we believe a call has started or
 * ended. Meet's UI flickers during layout changes and reconnects, and a single
 * stray reading either way would fire a spurious nudge or, worse, auto-stop a live
 * recording. Two ticks (~6s) is well inside human join/leave time and immune to a
 * one-frame blip.
 */
const CALL_DETECT_CONFIRMATIONS = 2;

/**
 * A human-readable stamp logged the moment this script loads. Reloading the
 * extension does *not* replace the content script already running in an open
 * meeting tab — only a real tab refresh does — so during development it is easy
 * to test against stale code without realising it. This line makes "which build
 * is this tab running?" answerable at a glance: bump it whenever the adapter
 * changes, refresh the tab, and confirm the new stamp appears.
 */
const BUILD = 'meet-adapter 2026-07-18e (name=span.notranslate, speaking=.sxlEM, +self-detect, +presenter, +roster-poll, +tile-health, +auto-record, +record-panel, +shortcut)';

const platform = detectPlatform();
const adapter = getAdapter(platform);

console.info('[meeting-intelligence] content script loaded — %s', BUILD);

/** Wall-clock instant that recording started. Null when not recording. */
let recordingStartedAt: number | null = null;
let pending: SpeakerEvent[] = [];
let flushTimer: number | null = null;
let rosterTimer: number | null = null;
/** Total speaker events produced this recording — 0 means everyone is Unknown. */
let totalEvents = 0;

/**
 * Roster members already handed to the service worker.
 *
 * Keyed by the platform's participant id where there is one, since that survives
 * a rename mid-call, and by name otherwise. Deliberately *not* seeded with the
 * roster sent at `openMeeting`: re-sending those costs one request the server
 * discards, and buys the repair for the case that started all this — a roster
 * that came back empty at click time is refilled by the first tick.
 */
let sentParticipants = new Set<string>();

/**
 * Debounced view of whether the user is in a call, for the auto-record nudge and
 * auto-stop. `callConfirm` counts consecutive ticks that disagree with `inCall`,
 * and the flip only happens once it reaches `CALL_DETECT_CONFIRMATIONS` — see the
 * constant for why a single reading is not trusted.
 */
let inCall = false;
let callConfirm = 0;

/** The in-page record panel, and whether the user waved it away this call. */
const panel = new RecordPanel();
/** The bound start/stop shortcut (e.g. "Alt+Shift+R"), cached after first fetch.
 *  '' means unbound; `null` means not yet asked. Shown in the panel as the reliable
 *  start path — the one Chrome accepts when the in-page button is refused. */
let recordShortcut: string | null = null;
/** Dismissing the join prompt suppresses it until the next call — not forever, or
 *  the feature would silently disable itself after one idle click. Reset on leave. */
let promptDismissed = false;

chrome.runtime.onMessage.addListener(
  (message: ExtensionMessage, _sender, sendResponse: (response?: unknown) => void) => {
    switch (message.type) {
      case 'GET_MEETING_CONTEXT':
        sendResponse(getContext());
        return false;

      case 'RECORDING_STARTED':
        start(message.startedAt);
        sendResponse({ ok: true });
        return false;

      case 'RECORDING_STOPPED':
        // Whatever was never flushed goes back in the reply, so the service
        // worker can post it *before* it finalizes the meeting.
        sendResponse(stop() satisfies StoppedResult);
        return false;

      default:
        return false;
    }
  },
);

function getContext(): MeetingContext {
  return {
    platform,
    title: adapter?.getTitle() ?? document.title,
    meetingUrl: window.location.href,
    participants: adapter?.getParticipants() ?? [],
  };
}

function start(startedAt: number): void {
  if (!adapter || recordingStartedAt !== null) return;

  recordingStartedAt = startedAt;

  totalEvents = 0;
  sentParticipants = new Set();
  adapter.observe((turn: ObservedTurn) => {
    const event = rebase(turn);
    if (event) {
      pending.push(event);
      totalEvents += 1;
    }
  });

  flushTimer = window.setInterval(flush, FLUSH_INTERVAL_MS);
  rosterTimer = window.setInterval(flushRoster, ROSTER_INTERVAL_MS);

  // Recording began (from the popup, or from the panel's own button) — the panel
  // now offers Stop instead of Start.
  void updatePanel();
}

/**
 * Stop watching, and return the turns that have not been sent yet.
 *
 * They are *returned* rather than flushed as another message, because the service
 * worker finalizes the meeting the moment this call comes back — and finalizing is
 * what queues transcription. A batch sent as a separate message would be racing
 * that job, and the batch would sometimes lose, leaving the closing minutes of
 * the meeting with no name against them.
 */
function stop(): StoppedResult {
  if (!adapter || recordingStartedAt === null) return { events: [], participants: [] };

  // Emits whatever turn was still open. The last speaker before someone hits
  // stop is often the one summarising what was just agreed, so dropping them is
  // expensive.
  adapter.disconnect();

  if (flushTimer !== null) {
    window.clearInterval(flushTimer);
    flushTimer = null;
  }

  if (rosterTimer !== null) {
    window.clearInterval(rosterTimer);
    rosterTimer = null;
  }

  if (totalEvents === 0) {
    console.warn(
      '[content] no speaker events captured on %s — every remote speaker will be ' +
        '"Unknown". If this is Meet, run __meetCalibrate() to refresh the selectors.',
      platform,
    );
  }

  const remaining = pending;
  pending = [];
  recordingStartedAt = null;

  // Recording is over. Fold the panel away rather than re-offering Start — someone
  // who just stopped is not looking to immediately record the same call again.
  // Treating it as a dismissal means the prompt stays gone until they leave and
  // rejoin (where `detectCallTick` clears the flag).
  promptDismissed = true;
  void updatePanel();

  // One last read on the way out. Someone who joined in the closing seconds is as
  // real an attendee as anyone, and this is the only chance left to notice them:
  // the service worker finalizes immediately after this returns.
  const participants = takeNewParticipants();

  // Not one roster member captured all meeting — not here, and not on any 5s
  // tick. That is the "no attendance recorded" failure at its source: the meeting
  // will have no attendees and its diarized voices no name candidates. On a real
  // call it means the roster read itself is broken, so say so now rather than let
  // it surface days later as minutes with no owners. (`totalEvents === 0` above is
  // the sibling signal for the speaking timeline; this one is for the roster.)
  if (sentParticipants.size === 0) {
    console.warn(
      '[content] no roster captured on %s for the whole meeting — attendance will ' +
        'be empty and diarized voices will have no name candidates. If this is Meet, ' +
        'the participant-tile selector has likely rotted; see the [meet] warning above.',
      platform,
    );
  }

  return { events: remaining, participants };
}

/** Convert a wall-clock turn onto the recording's clock. */
function rebase(turn: ObservedTurn): SpeakerEvent | null {
  if (recordingStartedAt === null) return null;

  const startMs = turn.startedAt - recordingStartedAt;
  const endMs = turn.endedAt - recordingStartedAt;

  // A turn that closed after recording started but *began* before it gets
  // clamped rather than dropped: the words are in the audio, so the speaker
  // should be too.
  if (endMs <= 0) return null;

  return {
    speakerName: turn.speakerName,
    ...(turn.speakerExternalRef ? { speakerExternalRef: turn.speakerExternalRef } : {}),
    ...(turn.source ? { source: turn.source } : {}),
    startMs: Math.max(0, startMs),
    endMs,
  };
}

function flush(): void {
  if (pending.length === 0) return;

  const events = pending;
  pending = [];

  void chrome.runtime
    .sendMessage({ type: 'SPEAKER_EVENTS', events } satisfies ExtensionMessage)
    .catch((err: unknown) => {
      // Put them back. A lost batch is a stretch of transcript with no name on
      // it, which is precisely the failure this whole mechanism exists to avoid.
      pending = [...events, ...pending];
      console.error('[content] failed to flush speaker events', err);
    });
}

/**
 * Roster members we have not handed over yet.
 *
 * Marks them sent, so the caller owns delivering them and must put them back if
 * it cannot — same contract as `flush`, and for the same reason: a participant
 * dropped here is one who silently never attended.
 */
function takeNewParticipants(): Participant[] {
  if (!adapter) return [];

  const fresh = adapter
    .getParticipants()
    .filter((p) => !sentParticipants.has(participantKey(p)));

  for (const p of fresh) sentParticipants.add(participantKey(p));
  return fresh;
}

function participantKey(p: Participant): string {
  // Prefixed, so a platform id can never collide with somebody's actual name.
  return p.externalRef ?? `name:${p.displayName}`;
}

function flushRoster(): void {
  const participants = takeNewParticipants();
  if (participants.length === 0) return;

  void chrome.runtime
    .sendMessage({ type: 'PARTICIPANTS', participants } satisfies ExtensionMessage)
    .catch((err: unknown) => {
      // Un-mark them, so the next tick tries again rather than writing them off.
      for (const p of participants) sentParticipants.delete(participantKey(p));
      console.error('[content] failed to send roster', err);
    });
}

/**
 * Watch for the user joining or leaving the call, and tell the service worker.
 *
 * This runs from the moment the script loads and for the whole life of the tab —
 * independent of recording — because the join is what we want to react to, and it
 * usually happens *before* anyone thinks to record. The two signals it sends do
 * very different things (see the message docs): a join is a gentle nudge, a leave
 * can stop an in-progress recording. Neither ever *starts* one.
 *
 * The reading is debounced through `CALL_DETECT_CONFIRMATIONS` so a one-frame UI
 * blip cannot fire a spurious nudge or, far worse, auto-stop a live recording.
 */
function detectCallTick(): void {
  if (!adapter) return;

  const reading = adapter.isInCall();
  if (reading === inCall) {
    callConfirm = 0; // steady state; nothing pending
    return;
  }

  callConfirm += 1;
  if (callConfirm < CALL_DETECT_CONFIRMATIONS) return;

  inCall = reading;
  callConfirm = 0;

  // A fresh call gets a fresh prompt: someone who dismissed the panel in the last
  // meeting should still be offered the next one.
  if (!inCall) promptDismissed = false;

  // The worker may be evicted; sending wakes it. A failure here (extension
  // reloading, worker torn down mid-send) is swallowed: the next real transition
  // re-syncs, and for a leave-while-recording the worker's own tab-close listener
  // is the backstop.
  void chrome.runtime
    .sendMessage({ type: inCall ? 'MEETING_JOINED' : 'MEETING_LEFT' } satisfies ExtensionMessage)
    .catch(() => undefined);

  void updatePanel();
}

/**
 * Show the right panel for the current state, or nothing.
 *
 * The single place that decides what the in-page card displays, so the state
 * machine lives in one function instead of being smeared across every event:
 *
 *   - recording        → the Stop card (regardless of call detection, since that
 *                        is the state the user most needs to be able to end);
 *   - in a call, not yet dismissed, nudge enabled → the Start card;
 *   - anything else     → hidden.
 */
async function updatePanel(): Promise<void> {
  if (recordingStartedAt !== null) {
    panel.recording(requestStop);
    return;
  }

  if (!inCall || promptDismissed) {
    panel.hide();
    return;
  }

  // Settings come from the worker, not a direct import — see `GET_SETTINGS`. An
  // unanswered query (worker mid-restart) defaults to showing the prompt: the
  // shipped default is on, and a missed read should not silently disable a feature
  // the user did not turn off.
  const settings = (await chrome.runtime
    .sendMessage({ type: 'GET_SETTINGS' } satisfies ExtensionMessage)
    .catch(() => null)) as Settings | null;

  if (settings?.autoPromptOnJoin === false) {
    panel.hide();
    return;
  }

  panel.joinPrompt(requestStart, dismissPrompt, await getRecordShortcut());
}

/** The bound start/stop shortcut, asked of the worker once and then cached. Falls
 *  back to '' (no hint shown) if the worker cannot be reached. */
async function getRecordShortcut(): Promise<string> {
  if (recordShortcut !== null) return recordShortcut;

  const shortcut = (await chrome.runtime
    .sendMessage({ type: 'GET_START_SHORTCUT' } satisfies ExtensionMessage)
    .catch(() => '')) as string;

  recordShortcut = shortcut ?? '';
  return recordShortcut;
}

/** Ask the service worker to record this tab. It replies ok/why-not; on failure
 *  the panel explains and offers a retry (see `RecordPanel.error`). */
function requestStart(): void {
  panel.starting();
  void chrome.runtime
    .sendMessage({ type: 'REQUEST_START_RECORDING' } satisfies ExtensionMessage)
    .then(async (res: { ok: boolean; error?: string } | undefined) => {
      // Success arrives as a RECORDING_STARTED message, which repaints the panel
      // via `start()`. Only the failure has to be handled here — and it is the
      // common one, since Chrome refuses tab capture from this in-page click.
      if (!res?.ok) {
        panel.error(
          res?.error ?? 'Recording could not be started.',
          requestStart,
          dismissPrompt,
          await getRecordShortcut(),
        );
      }
    })
    .catch(async (err: unknown) => {
      panel.error(String(err), requestStart, dismissPrompt, await getRecordShortcut());
    });
}

/** Stop from the panel. The worker finalizes and messages RECORDING_STOPPED back,
 *  which hides the panel via `stop()`. */
function requestStop(): void {
  void chrome.runtime
    .sendMessage({ type: 'STOP_RECORDING' } satisfies ExtensionMessage)
    .catch(() => undefined);
}

function dismissPrompt(): void {
  promptDismissed = true;
  panel.hide();
}

function detectPlatform(): Platform {
  const host = window.location.hostname;
  if (host.includes('meet.google.com')) return 'meet';
  if (host.includes('zoom.us')) return 'zoom';
  if (host.includes('teams.')) return 'teams';
  return 'other';
}

// Only platforms with a real adapter can report call state; on an unsupported page
// `getAdapter` returns null and there is nothing to watch.
if (adapter) {
  window.setInterval(detectCallTick, CALL_DETECT_INTERVAL_MS);
  detectCallTick(); // don't wait a full interval to notice an already-joined call
}
