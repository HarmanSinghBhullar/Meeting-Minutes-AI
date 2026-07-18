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

import {
  finalizeMeeting,
  openMeeting,
  postParticipants,
  postSpeakerEvents,
} from '@/lib/api';
import { getSettings } from '@/lib/settings';
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
 * Posts from the content script still in flight — speaker events and roster
 * updates alike.
 *
 * Posting them is fire-and-forget during the meeting, which is fine — but a batch
 * that lands *after* the transcription job has been queued is a batch the
 * attribution step never sees. For events, the speakers in it come out unnamed;
 * for the roster, a late joiner misses the `initial_prompt` and the
 * `max_speakers` bound, and is not on the candidate list for their own voice. So
 * we keep them and wait for them at the end.
 */
const pendingPosts = new Set<Promise<void>>();

/** Run `work` in the background, holding finalize open until it lands. */
function trackPost(work: () => Promise<void>, what: string): void {
  const post = work().catch((err: unknown) => {
    console.error(`[worker] failed to post ${what}`, err);
  });

  pendingPosts.add(post);
  void post.finally(() => pendingPosts.delete(post));
}

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
  (message: ExtensionMessage, sender, sendResponse: (response?: unknown) => void) => {
    switch (message.type) {
      case 'MEETING_JOINED':
        // The sender's own tab is the one in a call — trust the sender, not any
        // "active tab", since the meeting may not be focused.
        void onMeetingJoined(sender.tab?.id);
        return false;

      case 'MEETING_LEFT':
        void onMeetingLeft(sender.tab?.id);
        return false;

      case 'START_RECORDING':
        void startRecording(message.tabId).then(
          () => sendResponse({ ok: true }),
          (err: unknown) => sendResponse({ ok: false, error: String(err) }),
        );
        return true; // async response

      case 'REQUEST_START_RECORDING': {
        // The in-page panel's Start button. It cannot know its own tab id, so we
        // take it from the sender. Same recorder path as the popup; the only
        // difference is where the click came from — and Chrome may refuse tab
        // capture that did not originate in a toolbar-click gesture, which surfaces
        // as a start error the panel then explains.
        const tabId = sender.tab?.id;
        if (tabId === undefined) {
          sendResponse({ ok: false, error: 'No tab to record.' });
          return false;
        }
        void startRecording(tabId).then(
          () => sendResponse({ ok: true }),
          (err: unknown) => sendResponse({ ok: false, error: friendlyStartError(err) }),
        );
        return true;
      }

      case 'STOP_RECORDING':
        void stopRecording().then(
          () => sendResponse({ ok: true }),
          (err: unknown) => sendResponse({ ok: false, error: String(err) }),
        );
        return true;

      case 'GET_RECORDING_STATE':
        void currentState().then(sendResponse);
        return true;

      case 'GET_SETTINGS':
        // The content script reads settings through here rather than importing
        // `lib/settings`, which would make its classic-script bundle pull an
        // unexecutable `import`. The worker is a module, so it can own the read.
        void getSettings().then(sendResponse);
        return true;

      case 'GET_START_SHORTCUT':
        // The actual bound shortcut, so the panel shows what really works — the
        // suggested key may be unbound (taken by another extension) or rebound by
        // the user at chrome://extensions/shortcuts.
        void startShortcut().then(sendResponse);
        return true;

      case 'SPEAKER_EVENTS': {
        // The meeting id is looked up rather than remembered — this worker may
        // have been evicted and restarted several times since recording began.
        trackPost(async () => {
          const session = await loadSession();
          if (!session) return;
          await postSpeakerEvents(session.meetingId, message.events);
        }, 'speaker events');
        return false;
      }

      case 'PARTICIPANTS': {
        trackPost(async () => {
          const session = await loadSession();
          if (!session) return;
          await postParticipants(session.meetingId, message.participants);
        }, 'roster');
        return false;
      }

      default:
        return false;
    }
  },
);

// Closing the meeting tab is a leave the content script can no longer report — it
// went with the tab. This is the backstop that finalizes a recording whose tab was
// closed outright rather than left via the UI. Registered at the top level so the
// event can wake an evicted worker.
chrome.tabs.onRemoved.addListener((tabId) => {
  void onRecordingTabClosed(tabId);
});

// The keyboard shortcut. This is the *only* way, besides the toolbar click, to
// begin recording with the gesture Chrome demands for tab capture — the in-page
// panel's button cannot, because a click in the page does not invoke the extension.
// So the panel teaches this shortcut, and here is where the key press lands.
chrome.commands.onCommand.addListener((command) => {
  if (command === 'toggle-recording') void onToggleShortcut();
});

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
  // This also overwrites any green "click to record" nudge with the REC badge.
  await showRecordingBadge();
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
      if (meetingId && stopped) {
        // The roster first. Both must land before finalize, but this one names
        // people the events may only refer to, and posting it first means a late
        // joiner's own words never arrive before the row that says who they are.
        await postParticipants(meetingId, stopped.participants);
        await postSpeakerEvents(meetingId, stopped.events);
      }
    }

    // And anything a periodic flush left in the air.
    await Promise.all([...pendingPosts]);

    if (meetingId) await finalizeMeeting(meetingId);
  } finally {
    // Whatever failed above, the UI must not be left claiming to record — and the
    // next Start must not be blocked by a recording that is already over. The badge
    // is cleared rather than returned to the green nudge: someone who just stopped
    // does not want to be immediately re-invited to record the same call.
    await saveSession(null);
    await clearBadge();
  }
}

/**
 * The user joined a call: nudge them to record it, but never record on their
 * behalf. The nudge is a badge on the toolbar icon, not a recording — starting one
 * requires the deliberate click that consent (and Chrome's gesture rule) demand.
 *
 * Skipped when a recording is already running: its REC badge owns the icon, and a
 * green "click to record" over a live recording would be a lie.
 */
async function onMeetingJoined(tabId: number | undefined): Promise<void> {
  if (tabId === undefined) return; // not from a tab; nothing to nudge about

  const { autoPromptOnJoin } = await getSettings();
  if (!autoPromptOnJoin) return;

  if ((await currentState()).isRecording) return;

  await showRecordPrompt();
}

/**
 * The user left a call. Two cases, told apart by whether the recording lives on
 * the tab they left:
 *
 *   - It does, and `autoStopOnLeave` is on → stop and finalize. Leaving the
 *     meeting is the clearest possible "I'm done" signal, and a recording that
 *     runs on past the call it was capturing only accumulates silence and risks
 *     never being stopped at all.
 *   - It does not (nothing recording, or recording on another tab) → the green
 *     nudge, if any, is now stale, so clear it — but only when nothing is
 *     recording, so a REC badge for a recording elsewhere is left untouched.
 */
async function onMeetingLeft(tabId: number | undefined): Promise<void> {
  if (tabId === undefined) return;

  const session = await loadSession();
  if (session && session.tabId === tabId) {
    const { autoStopOnLeave } = await getSettings();
    if (autoStopOnLeave) {
      console.info('[worker] call ended on the recording tab (%d) — finalizing.', tabId);
      await stopRecording(); // clears the badge in its own finally
    }
    // If auto-stop is off, the recording continues by the user's preference and
    // its REC badge stays put.
    return;
  }

  if (!(await currentState()).isRecording) await clearBadge();
}

/**
 * The tab hosting the live recording was closed. The content script cannot report
 * this leave — it died with the tab — so this is the only signal left. Finalize,
 * so a closed tab does not strand a recording that never gets transcribed.
 */
async function onRecordingTabClosed(tabId: number): Promise<void> {
  const session = await loadSession();
  if (!session || session.tabId !== tabId) return;

  const { autoStopOnLeave } = await getSettings();
  if (!autoStopOnLeave) return;

  console.info('[worker] recording tab %d was closed — finalizing.', tabId);
  await stopRecording();
}

/**
 * The keyboard shortcut fired: stop if recording, otherwise record the active tab.
 *
 * `onCommand` runs with a user gesture and grants activeTab for the focused tab —
 * which is precisely what `getMediaStreamId` needs and what the in-page panel's
 * click cannot supply. So this is the reliable start path, and toggling keeps it to
 * one binding for both directions.
 */
async function onToggleShortcut(): Promise<void> {
  if ((await currentState()).isRecording) {
    await stopRecording();
    return;
  }

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id === undefined) return;

  try {
    await startRecording(tab.id);
  } catch (err: unknown) {
    // A shortcut has no UI to show an error in; the tab's content script does.
    console.error('[worker] shortcut start failed', err);
  }
}

/** The shortcut actually bound to `toggle-recording`, or '' if none is. */
async function startShortcut(): Promise<string> {
  const commands = await chrome.commands.getAll();
  return commands.find((c) => c.name === 'toggle-recording')?.shortcut ?? '';
}

/** Badge text for the two states the icon advertises. */
const PROMPT_BADGE = '●';
const RECORDING_BADGE = 'REC';

/** Green: a meeting is here and one click away from being recorded. */
async function showRecordPrompt(): Promise<void> {
  await chrome.action.setBadgeText({ text: PROMPT_BADGE });
  await chrome.action.setBadgeBackgroundColor({ color: '#1a7f37' });
  await chrome.action.setTitle({ title: 'Meeting detected — click to record' });
}

/** Red: recording, and unmissably so. */
async function showRecordingBadge(): Promise<void> {
  await chrome.action.setBadgeText({ text: RECORDING_BADGE });
  await chrome.action.setBadgeBackgroundColor({ color: '#d93025' });
  await chrome.action.setTitle({ title: 'Recording — click to stop' });
}

/** Back to the resting state: no badge, default tooltip. */
async function clearBadge(): Promise<void> {
  await chrome.action.setBadgeText({ text: '' });
  await chrome.action.setTitle({ title: 'Meeting Intelligence' });
}

/**
 * Turn a start failure into something the in-page panel can show a human.
 *
 * The one worth translating is Chrome's refusal to capture a tab from a click that
 * did not happen on the extension's own toolbar button: `getMediaStreamId` reports
 * it as "extension has not been invoked", which means nothing to a user. The panel
 * already tells them the fix (click the icon), so here we just make the reason
 * legible. Everything else is passed through — a backend error or a busy recorder
 * says something useful on its own.
 */
function friendlyStartError(err: unknown): string {
  const message = String(err);
  if (/not been invoked|user gesture|activeTab/i.test(message)) {
    return 'Chrome only allows recording to start from the extension icon on this page.';
  }
  return message;
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
      await clearBadge();
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
