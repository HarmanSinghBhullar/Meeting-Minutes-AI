/**
 * Service worker: coordinates a recording.
 *
 * It captures nothing itself — MV3 workers have no DOM and so cannot hold a
 * MediaRecorder. Its job is to sequence the four things that have to happen in
 * the right order, and to be the one place that talks to the backend.
 *
 * The ordering is not arbitrary:
 *
 *   1. Ask the content script who is in the meeting. Those names prime Whisper's
 *      decoder, so they have to reach the backend *before* any audio does.
 *   2. Open the meeting on the backend, so the chunk uploads have somewhere to go.
 *   3. Get a tab capture stream id. This must happen inside the user gesture's
 *      call stack — Chrome refuses otherwise — which is why recording can only
 *      start from a click in the popup, and why this cannot be moved into the
 *      offscreen document or fired on a timer.
 *   4. Tell the content script when recording started, so it can rebase the
 *      speaker timeline onto the audio clock.
 */

import { finalizeMeeting, openMeeting, postSpeakerEvents } from '@/lib/api';
import type { ExtensionMessage, MeetingContext, RecordingState } from '@/lib/types';

const OFFSCREEN_PATH = 'src/offscreen/offscreen.html';

let state: RecordingState = { isRecording: false };

/** The tab being recorded, so speaker events can be matched to the meeting. */
let recordedTabId: number | null = null;

chrome.runtime.onMessage.addListener(
  (message: ExtensionMessage, _sender, sendResponse: (response?: unknown) => void) => {
    switch (message.type) {
      case 'START_RECORDING':
        void startRecording(message.tabId).then(
          () => sendResponse({ ok: true }),
          (err: unknown) => sendResponse({ ok: false, error: String(err) }),
        );
        return true; // async response

      case 'STOP_RECORDING':
        void stopRecording().then(
          () => sendResponse({ ok: true }),
          (err: unknown) => sendResponse({ ok: false, error: String(err) }),
        );
        return true;

      case 'GET_RECORDING_STATE':
        sendResponse(state);
        return false;

      case 'SPEAKER_EVENTS':
        if (state.meetingId) {
          void postSpeakerEvents(state.meetingId, message.events).catch((err: unknown) => {
            console.error('[worker] failed to post speaker events', err);
          });
        }
        return false;

      default:
        return false;
    }
  },
);

async function startRecording(tabId: number): Promise<void> {
  if (state.isRecording) return;

  const meetingId = crypto.randomUUID();
  const context = await getMeetingContext(tabId);

  await openMeeting({
    id: meetingId,
    ...(context.title ? { title: context.title } : {}),
    platform: context.platform,
    meetingUrl: context.meetingUrl,
    speakers: context.participants,
  });

  await ensureOffscreenDocument();

  // Inside the gesture's call stack. Cannot be deferred.
  const streamId = await getMediaStreamId(tabId);

  await chrome.runtime.sendMessage({
    type: 'OFFSCREEN_START',
    streamId,
    meetingId,
  } satisfies ExtensionMessage);

  const startedAt = Date.now();
  state = { isRecording: true, meetingId, startedAt };
  recordedTabId = tabId;

  // The pivot the whole attribution feature turns on: the content script needs
  // this to convert wall-clock speaker turns into offsets into the audio.
  await sendToTab(tabId, { type: 'RECORDING_STARTED', startedAt });

  // Not decoration. Recording people without their knowledge is illegal in
  // two-party-consent jurisdictions, so the fact of it must be impossible to miss.
  await chrome.action.setBadgeText({ text: 'REC' });
  await chrome.action.setBadgeBackgroundColor({ color: '#d93025' });
}

async function stopRecording(): Promise<void> {
  const { meetingId } = state;

  await chrome.runtime.sendMessage({ type: 'OFFSCREEN_STOP' } satisfies ExtensionMessage);

  // Before finalizing: this makes the content script close its open turn and
  // flush. Finalizing first would queue transcription against a timeline that is
  // still missing its last speaker.
  if (recordedTabId !== null) {
    await sendToTab(recordedTabId, { type: 'RECORDING_STOPPED' });
  }

  if (meetingId) await finalizeMeeting(meetingId);

  state = { isRecording: false };
  recordedTabId = null;
  await chrome.action.setBadgeText({ text: '' });
}

/**
 * Ask the content script what it can see.
 *
 * Degrades rather than fails: an unsupported platform, or a meeting tab that was
 * already open when the extension was installed (so no content script is
 * injected), yields no participants. The recording still works — the backend
 * simply falls back to diarization and the speakers come out unnamed.
 */
async function getMeetingContext(tabId: number): Promise<MeetingContext> {
  try {
    return (await chrome.tabs.sendMessage(tabId, {
      type: 'GET_MEETING_CONTEXT',
    } satisfies ExtensionMessage)) as MeetingContext;
  } catch (err: unknown) {
    console.warn('[worker] no content script on this tab; speakers will be unnamed', err);
    const tab = await chrome.tabs.get(tabId);
    return {
      platform: 'other',
      title: tab.title ?? null,
      meetingUrl: tab.url ?? '',
      participants: [],
    };
  }
}

/**
 * Promisified `chrome.tabCapture.getMediaStreamId`.
 *
 * The API is callback-only, and wrapping it keeps `startRecording` linear — which
 * matters, because the ordering of the steps in there is load-bearing and a
 * callback nested in the middle would obscure it.
 */
function getMediaStreamId(tabId: number): Promise<string> {
  return new Promise((resolve, reject) => {
    chrome.tabCapture.getMediaStreamId({ targetTabId: tabId }, (streamId) => {
      const error = chrome.runtime.lastError;
      if (error) reject(new Error(error.message));
      else resolve(streamId);
    });
  });
}

async function sendToTab(tabId: number, message: ExtensionMessage): Promise<void> {
  try {
    await chrome.tabs.sendMessage(tabId, message);
  } catch (err: unknown) {
    console.warn('[worker] could not reach content script', err);
  }
}

/** Create the offscreen document if it does not already exist. */
async function ensureOffscreenDocument(): Promise<void> {
  const existing = await chrome.runtime.getContexts({
    contextTypes: [chrome.runtime.ContextType.OFFSCREEN_DOCUMENT],
  });
  if (existing.length > 0) return;

  await chrome.offscreen.createDocument({
    url: OFFSCREEN_PATH,
    reasons: [chrome.offscreen.Reason.USER_MEDIA],
    justification: 'Recording meeting audio for transcription.',
  });
}
