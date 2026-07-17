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
  SpeakerEvent,
  StoppedResult,
} from '@/lib/types';
import { getAdapter, type ObservedTurn } from './adapters';

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
 * A human-readable stamp logged the moment this script loads. Reloading the
 * extension does *not* replace the content script already running in an open
 * meeting tab — only a real tab refresh does — so during development it is easy
 * to test against stale code without realising it. This line makes "which build
 * is this tab running?" answerable at a glance: bump it whenever the adapter
 * changes, refresh the tab, and confirm the new stamp appears.
 */
const BUILD = 'meet-adapter 2026-07-17b (speaking=.sxlEM, +self-detect, +presenter, +roster-poll)';

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

  // One last read on the way out. Someone who joined in the closing seconds is as
  // real an attendee as anyone, and this is the only chance left to notice them:
  // the service worker finalizes immediately after this returns.
  return { events: remaining, participants: takeNewParticipants() };
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

function detectPlatform(): Platform {
  const host = window.location.hostname;
  if (host.includes('meet.google.com')) return 'meet';
  if (host.includes('zoom.us')) return 'zoom';
  if (host.includes('teams.')) return 'teams';
  return 'other';
}
