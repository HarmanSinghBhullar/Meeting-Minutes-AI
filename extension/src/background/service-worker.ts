/**
 * Service worker: coordinates a recording.
 *
 * It captures nothing itself — MV3 workers have no DOM and so cannot hold a
 * MediaRecorder. Its job is to sequence the things that have to happen in the
 * right order, and to be the one place that talks to the backend.
 *
 * The ordering is not arbitrary:
 *
 *   1. Get a tab capture stream id. This must happen inside the user gesture's
 *      call stack — Chrome refuses otherwise — which is why recording can only
 *      start from a click in the popup, and why this cannot be moved into the
 *      offscreen document or fired on a timer. It goes first, so that nothing
 *      slow can be inserted between the click and this call.
 *   2. Ask the content script who is in the meeting, and open the meeting on the
 *      backend. Those names prime Whisper's decoder, so they have to arrive
 *      *before* any audio does — which they will, because recording has not
 *      started yet.
 *   3. Start the recorder, and take t=0 from the offscreen document itself.
 *   4. Tell the content script when recording started, so it can rebase the
 *      speaker timeline onto the audio clock.
 *
 * On the way out the order reverses, and it matters just as much: flush the
 * audio, flush the speaker events, and only then finalize — because finalizing is
 * what queues transcription, and anything still in flight at that moment is
 * something the transcript will never see.
 */

import { finalizeMeeting, openMeeting, postSpeakerEvents } from '@/lib/api';
import type {
  ExtensionMessage,
  MeetingContext,
  OffscreenState,
  RecordingState,
  Reply,
  StopResult,
  StoppedResult,
} from '@/lib/types';

const OFFSCREEN_PATH = 'src/offscreen/offscreen.html';

/** Must match `content_scripts[0].js` in the manifest — the same file, injected
 *  by hand when Chrome did not inject it for us. See `getMeetingContext`. */
const CONTENT_SCRIPT_PATH = 'src/content/content.js';

/** Where the live recording is kept. See `loadSession`. */
const SESSION_KEY = 'recording';

/**
 * The recording in progress, as persisted across service-worker restarts.
 *
 * The tab id is part of it because the speaker timeline lives in that tab's
 * content script, and at stop time we have to go back and ask it for the turns it
 * has not flushed.
 */
interface RecordingSession {
  meetingId: string;
  startedAt: number;
  tabId: number;
}

/**
 * Speaker-event batches still in flight.
 *
 * Posting them is fire-and-forget during the meeting, which is fine — but a batch
 * that lands *after* the transcription job has been queued is a batch the
 * attribution step never sees, and the speakers in it come out unnamed. So we
 * keep them and wait for them at the end.
 */
const pendingSpeakerPosts = new Set<Promise<void>>();

/**
 * Read the live recording.
 *
 * This is in `chrome.storage.session` rather than a module variable because **MV3
 * evicts an idle service worker after about thirty seconds**, taking its variables
 * with it — while the offscreen document, and its MediaRecorder, carry on
 * regardless. Any meeting longer than a coffee break outlives this worker several
 * times over.
 *
 * Holding the meeting id in memory therefore loses it mid-meeting, and everything
 * keyed on it goes quietly wrong: speaker events stop being posted because there
 * is no meeting to post them against, and Stop stops finalizing because there is
 * no meeting to finalize — so the recording ends and no transcription is ever
 * queued. Session storage survives the eviction; the variable did not.
 */
async function loadSession(): Promise<RecordingSession | null> {
  const stored = await chrome.storage.session.get(SESSION_KEY);
  return (stored[SESSION_KEY] as RecordingSession | undefined) ?? null;
}

async function saveSession(session: RecordingSession | null): Promise<void> {
  if (session) await chrome.storage.session.set({ [SESSION_KEY]: session });
  else await chrome.storage.session.remove(SESSION_KEY);
}

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
        void currentState().then(sendResponse);
        return true;

      case 'SPEAKER_EVENTS': {
        // The meeting id is looked up rather than remembered — this worker may
        // have been evicted and restarted several times since recording began.
        const post = (async () => {
          const session = await loadSession();
          if (!session) return;
          await postSpeakerEvents(session.meetingId, message.events);
        })().catch((err: unknown) => {
          console.error('[worker] failed to post speaker events', err);
        });

        pendingSpeakerPosts.add(post);
        void post.finally(() => pendingSpeakerPosts.delete(post));
        return false;
      }

      default:
        return false;
    }
  },
);

async function startRecording(tabId: number): Promise<void> {
  await ensureOffscreenDocument();

  // Ask the recorder, don't trust our own bookkeeping. The offscreen document
  // holds the MediaRecorder, so it *is* the world; everything else is a belief
  // about it. The two came apart once — a failed start left a recorder running
  // that nothing was tracking, so the popup cheerfully offered to start a second
  // one — and checking here is what makes that unrepresentable.
  if ((await offscreenState()).isRecording) {
    throw new Error('A recording is already in progress.');
  }

  // Inside the gesture's call stack. Cannot be deferred, so nothing slow goes
  // above it.
  const streamId = await getMediaStreamId(tabId);

  const meetingId = crypto.randomUUID();
  const context = await getMeetingContext(tabId);

  await openMeeting({
    id: meetingId,
    ...(context.title ? { title: context.title } : {}),
    platform: context.platform,
    meetingUrl: context.meetingUrl,
    speakers: context.participants,
  });

  // t=0 comes back from the offscreen document, measured next to `recorder.start()`
  // rather than out here once the round trip is done. Tens of milliseconds of
  // messaging latency would otherwise be baked into every speaker timestamp.
  const { startedAt } = await askOffscreen<{ startedAt: number }>({
    type: 'OFFSCREEN_START',
    streamId,
    meetingId,
  }).catch(async (err: unknown) => {
    // The message may well have been delivered even though the call reported a
    // failure. Leave nothing recording.
    await askOffscreen<StopResult>({ type: 'OFFSCREEN_STOP' }).catch(() => undefined);
    throw err;
  });

  // Persisted before anything else can go wrong. From here on, a Stop must be able
  // to find this meeting even if this worker has been evicted and restarted in the
  // meantime — which, over a real meeting, it certainly will have been.
  await saveSession({ meetingId, startedAt, tabId });

  // The pivot the whole attribution feature turns on: the content script needs
  // this to convert wall-clock speaker turns into offsets into the audio.
  await sendToTab(tabId, { type: 'RECORDING_STARTED', startedAt });

  // Not decoration. Recording people without their knowledge is illegal in
  // two-party-consent jurisdictions, so the fact of it must be impossible to miss.
  await chrome.action.setBadgeText({ text: 'REC' });
  await chrome.action.setBadgeBackgroundColor({ color: '#d93025' });
}

/**
 * Stop, flush, finalize — in that order, and never any other.
 *
 * Finalizing queues the transcription job. Anything not yet on the backend at
 * that moment is lost: an audio chunk still uploading becomes a hole in the
 * transcript, and a speaker event still in flight becomes a stretch of dialogue
 * with nobody's name on it.
 */
async function stopRecording(): Promise<void> {
  // Loaded, not remembered. By the time someone presses Stop, this worker has
  // almost certainly been evicted and restarted since the recording began.
  const session = await loadSession();
  const meetingId = session?.meetingId;
  const tabId = session?.tabId ?? null;

  try {
    // Returns only once every outstanding chunk has been uploaded.
    const audio = await askOffscreen<StopResult>({ type: 'OFFSCREEN_STOP' });
    if (audio.failed > 0) {
      console.error(
        `[worker] ${audio.failed} chunk(s) never uploaded — the transcript will have gaps.`,
      );
    }

    // The content script closes its open turn and hands back whatever it has not
    // flushed. Taking the events straight from the reply — rather than waiting for
    // it to post them itself — is what makes this deterministic. The last speaker
    // before someone hits stop is usually the one summarising what was just
    // agreed, which is precisely the turn worth having.
    if (tabId !== null) {
      const stopped = await sendToTab<StoppedResult>(tabId, { type: 'RECORDING_STOPPED' });
      if (meetingId && stopped && stopped.events.length > 0) {
        await postSpeakerEvents(meetingId, stopped.events);
      }
    }

    // And any batch a periodic flush left in the air.
    await Promise.all([...pendingSpeakerPosts]);

    if (meetingId) await finalizeMeeting(meetingId);
  } finally {
    // Whatever failed above, the UI must not be left claiming to record — and the
    // next Start must not be blocked by a recording that is already over.
    await saveSession(null);
    await chrome.action.setBadgeText({ text: '' });
  }
}

/**
 * What is actually happening, as opposed to what we last wrote down.
 *
 * Two sources, and they can disagree. The offscreen document knows whether audio
 * is being captured; session storage knows which meeting it belongs to. A crashed
 * recorder leaves the second without the first, and the popup would otherwise
 * offer a Stop button for a recording that no longer exists — so the stale entry
 * is cleared rather than reported.
 */
async function currentState(): Promise<RecordingState> {
  const [offscreen, session] = await Promise.all([offscreenState(), loadSession()]);

  if (!offscreen.isRecording) {
    if (session) {
      console.warn('[worker] the recorder is gone; clearing the recording.');
      await saveSession(null);
      await chrome.action.setBadgeText({ text: '' });
    }
    return { isRecording: false };
  }

  // Audio is being captured. Even if session storage were somehow empty, the
  // offscreen document still knows which meeting the chunks are going to — so the
  // popup can offer a Stop that finalizes the right one, rather than stranding a
  // recording no one can turn off.
  const meetingId = session?.meetingId ?? offscreen.meetingId;

  return {
    isRecording: true,
    ...(meetingId ? { meetingId } : {}),
    ...(session ? { startedAt: session.startedAt } : {}),
  };
}

/** Whether the offscreen document is holding a live recording. */
async function offscreenState(): Promise<OffscreenState> {
  const contexts = await chrome.runtime.getContexts({
    contextTypes: [chrome.runtime.ContextType.OFFSCREEN_DOCUMENT],
  });
  if (contexts.length === 0) return { isRecording: false };

  try {
    return await askOffscreen<OffscreenState>({ type: 'OFFSCREEN_GET_STATE' });
  } catch (err: unknown) {
    console.warn('[worker] offscreen document did not answer', err);
    return { isRecording: false };
  }
}

/**
 * Send a message to the offscreen document and unwrap its reply.
 *
 * Everything the offscreen document answers is a `Reply<T>`, so a failure inside
 * it surfaces here as a thrown error with its message intact, rather than as a
 * bare rejected promise that says only "the message port closed" — which is what
 * a listener that forgets to respond produces, and which tells you nothing about
 * what went wrong.
 */
async function askOffscreen<T>(message: ExtensionMessage): Promise<T> {
  const reply = (await chrome.runtime.sendMessage(message)) as Reply<T> | undefined;

  if (!reply) throw new Error('The offscreen document did not reply.');
  if (!reply.ok) throw new Error(reply.error);

  return reply.result;
}

/**
 * Ask the content script what it can see, injecting it first if it is not there.
 *
 * **Why the injection.** Chrome does not run `content_scripts` in tabs that were
 * already open when the extension was installed or reloaded — only in ones opened
 * afterwards. So the single most likely way to use this tool, joining a meeting
 * and *then* deciding to record it, is exactly the case where the content script
 * is missing. Worse, the obvious remedy is one the user cannot apply: you cannot
 * reload a Google Meet tab without leaving the call.
 *
 * This is not a rare edge. It cost us the speaker timeline on the first recording
 * that otherwise worked end to end — the audio and the minutes were fine, and
 * every speaker came out `unknown`, which is the one part of the output nobody
 * would have accepted. The `scripting` permission exists for precisely this, so
 * we inject on demand and a pre-existing tab behaves like any other.
 *
 * Still degrades rather than fails. On a genuinely unsupported page, injection is
 * refused and the recording continues without names: the backend falls back to
 * diarization, and unnamed speakers beat no recording.
 */
async function getMeetingContext(tabId: number): Promise<MeetingContext> {
  const existing = await askContentScript(tabId);
  if (existing) return existing;

  if (await injectContentScript(tabId)) {
    const injected = await askContentScript(tabId);
    if (injected) return injected;
  }

  console.warn('[worker] no content script on this tab; speakers will be unnamed');
  const tab = await chrome.tabs.get(tabId);
  return {
    platform: 'other',
    title: tab.title ?? null,
    meetingUrl: tab.url ?? '',
    participants: [],
  };
}

/** The meeting as the content script sees it, or null if it is not listening. */
async function askContentScript(tabId: number): Promise<MeetingContext | null> {
  try {
    const context = (await chrome.tabs.sendMessage(tabId, {
      type: 'GET_MEETING_CONTEXT',
    } satisfies ExtensionMessage)) as MeetingContext | undefined;

    return context ?? null;
  } catch {
    return null; // nobody listening — expected, and handled by the caller
  }
}

/**
 * Inject the content script into a tab Chrome did not inject it into.
 *
 * Resolves once the script has been evaluated, so its message listener is
 * registered by the time we ask it anything. Chrome refuses to script a page we
 * hold no host permission for — which is the answer we want on an unsupported
 * platform, so a refusal is reported rather than thrown.
 */
async function injectContentScript(tabId: number): Promise<boolean> {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: [CONTENT_SCRIPT_PATH],
    });
    console.info('[worker] injected the content script into a tab that predated it');
    return true;
  } catch (err: unknown) {
    console.warn('[worker] could not inject the content script', err);
    return false;
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

/**
 * Send to the content script, tolerating its absence.
 *
 * A meeting on an unsupported platform has no content script, and that is a
 * degraded recording rather than a failed one — so this warns and returns nothing
 * instead of throwing and aborting the stop path.
 */
async function sendToTab<T>(
  tabId: number,
  message: ExtensionMessage,
): Promise<T | undefined> {
  try {
    return (await chrome.tabs.sendMessage(tabId, message)) as T;
  } catch (err: unknown) {
    console.warn('[worker] could not reach content script', err);
    return undefined;
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
