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

import type { ExtensionMessage, MeetingContext, Platform, SpeakerEvent } from '@/lib/types';
import { getAdapter, type ObservedTurn } from './adapters';

/** How often buffered turns are flushed to the service worker. */
const FLUSH_INTERVAL_MS = 10_000;

const platform = detectPlatform();
const adapter = getAdapter(platform);

/** Wall-clock instant that recording started. Null when not recording. */
let recordingStartedAt: number | null = null;
let pending: SpeakerEvent[] = [];
let flushTimer: number | null = null;

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
        stop();
        sendResponse({ ok: true });
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

  adapter.observe((turn: ObservedTurn) => {
    const event = rebase(turn);
    if (event) pending.push(event);
  });

  flushTimer = window.setInterval(flush, FLUSH_INTERVAL_MS);
}

function stop(): void {
  if (!adapter || recordingStartedAt === null) return;

  // Emits whatever turn was still open. The last speaker before someone hits
  // stop is often the one summarising what was just agreed, so dropping them is
  // expensive.
  adapter.disconnect();

  if (flushTimer !== null) {
    window.clearInterval(flushTimer);
    flushTimer = null;
  }

  flush();
  recordingStartedAt = null;
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

function detectPlatform(): Platform {
  const host = window.location.hostname;
  if (host.includes('meet.google.com')) return 'meet';
  if (host.includes('zoom.us')) return 'zoom';
  if (host.includes('teams.')) return 'teams';
  return 'other';
}
