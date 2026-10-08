/**
 * Audio capture. This is the trickiest file in the extension, so the reasoning
 * behind each decision is written down here rather than left to be rediscovered.
 *
 * **Why an offscreen document at all.** MV3 service workers have no DOM, so they
 * cannot construct a MediaRecorder. Capture has to live in an offscreen document
 * and the worker can only coordinate it. There is no way around this.
 *
 * **Why two tracks and not one.** chrome.tabCapture gives us the tab's audio —
 * the remote participants. It does *not* give us the local microphone; capture
 * the tab alone and you transcribe everyone except the person using the tool.
 * So we take the mic separately, and rather than mixing them into one stream we
 * record them as two.
 *
 * That is the single highest-leverage decision in the project. The mic track is
 * the local user by definition and the tab track is everyone else by definition,
 * which hands us a perfectly clean speaker boundary with zero inference — and it
 * removes the largest source of diarization error, which is the local speaker
 * bleeding into remote turns.
 *
 * **Why the tab audio gets piped to the speakers.** Once you pull a tab's audio
 * through getUserMedia, it stops coming out of the user's speakers. If we did
 * nothing, starting a recording would silence the meeting. So the tab stream is
 * routed back to the audio context's destination as well as into the recorder.
 * Forgetting this is a classic tabCapture bug and it is very confusing to debug.
 *
 * **Why every message is answered.** See the listener below. This is the one that
 * actually bit us.
 */

import { uploadChunk } from '@/lib/api';
import type { ExtensionMessage, OffscreenState, StopResult, Track } from '@/lib/types';

/**
 * How often MediaRecorder hands us a blob. Small enough that a browser crash
 * costs a few seconds rather than the whole meeting; large enough that we are
 * not making an HTTP request every heartbeat.
 */
const TIMESLICE_MS = 5_000;

interface ActiveRecording {
  meetingId: string;
  recorders: MediaRecorder[];
  streams: MediaStream[];
  audioContext: AudioContext;
  /** Uploads still in flight. `stop` waits on these — see below. */
  uploads: Set<Promise<void>>;
  uploaded: number;
  failed: number;
}

let active: ActiveRecording | null = null;

/**
 * Answer every message. Every single one.
 *
 * `chrome.runtime.sendMessage` returns a promise that **rejects** if the listener
 * returns without replying — but the message is still delivered and the work
 * still happens. A silent listener therefore starts the recorder and tells the
 * caller it failed.
 *
 * That is not hypothetical. It is exactly what this file used to do, and the
 * consequences were not local to it: the service worker's `await` threw, so it
 * never marked itself as recording, never told the content script to start its
 * speaker timeline, and never ran a stop that reached the recorder. Audio
 * uploaded for ten minutes after the user pressed stop, and no meeting was ever
 * finalized. One missing `sendResponse` cost the entire end-to-end path.
 */
chrome.runtime.onMessage.addListener(
  (message: ExtensionMessage, _sender, sendResponse: (response?: unknown) => void) => {
    switch (message.type) {
      case 'OFFSCREEN_START':
        void start(message.streamId, message.meetingId).then(
          (startedAt) => sendResponse({ ok: true, result: { startedAt } }),
          (err: unknown) => sendResponse({ ok: false, error: String(err) }),
        );
        return true; // async response

      case 'OFFSCREEN_STOP':
        void stop().then(
          (result) => sendResponse({ ok: true, result }),
          (err: unknown) => sendResponse({ ok: false, error: String(err) }),
        );
        return true;

      case 'OFFSCREEN_GET_STATE': {
        const result: OffscreenState = active
          ? { isRecording: true, meetingId: active.meetingId }
          : { isRecording: false };
        sendResponse({ ok: true, result });
        return false;
      }

      default:
        return false;
    }
  },
);

/**
 * Begin recording, and return the instant recording actually began.
 *
 * That instant is t=0 for every word timestamp the backend will produce, and the
 * content script rebases every speaker turn onto it. It is measured *here*, next
 * to `recorder.start()`, rather than in the service worker once this call
 * returns: the message round trip is tens of milliseconds and would push t=0 late
 * by that much, sliding every speaker label out of place. Wrong labels are worse
 * than no labels.
 */
async function start(streamId: string, meetingId: string): Promise<number> {
  if (active) throw new Error('A recording is already in progress.');

  let tabStream: MediaStream | undefined;
  let micStream: MediaStream | undefined;
  let audioContext: AudioContext | undefined;

  try {
    // The tab's audio: the remote participants.
    tabStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        mandatory: {
          chromeMediaSource: 'tab',
          chromeMediaSourceId: streamId,
        },
      },
    } as MediaStreamConstraints);

    // The microphone: the local user. Permission must already have been granted
    // via the permission page — an offscreen document cannot show a prompt.
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    // Keep the meeting audible. Capturing a tab diverts its audio away from the
    // speakers; routing it to the destination puts it back.
    audioContext = new AudioContext();
    audioContext.createMediaStreamSource(tabStream).connect(audioContext.destination);

    const recording: ActiveRecording = {
      meetingId,
      recorders: [],
      streams: [tabStream, micStream],
      audioContext,
      uploads: new Set(),
      uploaded: 0,
      failed: 0,
    };

    recording.recorders = [
      createRecorder(tabStream, 'tab', recording),
      createRecorder(micStream, 'mic', recording),
    ];

    const startedAt = Date.now();
    for (const recorder of recording.recorders) recorder.start(TIMESLICE_MS);

    active = recording;
    return startedAt;
  } catch (err: unknown) {
    // Leave nothing running. A half-started recording is worse than a failed one:
    // it holds the microphone open with no one tracking it and no way to stop it
    // short of reloading the extension.
    for (const stream of [tabStream, micStream]) {
      if (stream) for (const track of stream.getTracks()) track.stop();
    }
    if (audioContext) void audioContext.close();
    throw err;
  }
}

/**
 * Wire one stream to one recorder, uploading each timeslice as it arrives.
 *
 * WebM is a streaming container, so the chunks a timesliced MediaRecorder emits
 * append into a valid file server-side. That is what makes incremental upload
 * work at all.
 */
function createRecorder(
  stream: MediaStream,
  track: Track,
  recording: ActiveRecording,
): MediaRecorder {
  const recorder = new MediaRecorder(stream, { mimeType: 'audio/webm;codecs=opus' });

  recorder.ondataavailable = (event: BlobEvent) => {
    if (event.data.size === 0) return;

    // Not awaited — a slow upload must not stall the recorder — but *tracked*, so
    // that `stop` can wait for the last chunk to land. Finalizing while chunks are
    // still in flight queues transcription against a file ffmpeg is about to read
    // while the API is still writing to it.
    const upload = uploadChunk(recording.meetingId, track, event.data).then(
      () => {
        recording.uploaded += 1;
      },
      (err: unknown) => {
        recording.failed += 1;
        console.error(`[recorder] ${track} chunk upload failed`, err);
      },
    );

    recording.uploads.add(upload);
    void upload.finally(() => recording.uploads.delete(upload));
  };

  return recorder;
}

/** Stop recording, and do not return until every chunk has been uploaded. */
async function stop(): Promise<StopResult> {
  if (!active) return { uploaded: 0, failed: 0 };

  const recording = active;
  active = null; // no further chunks can be attributed to this recording

  // `MediaRecorder.stop()` emits one final blob, and it does so *asynchronously*.
  // Waiting for each recorder's own stop event is what guarantees that last blob
  // has been through `ondataavailable` — and is therefore in `uploads` — before we
  // wait on the uploads themselves. Skip this and the final few seconds of the
  // meeting, which is usually where the decisions get restated, never arrive.
  await Promise.all(recording.recorders.map(stopRecorder));
  await Promise.all([...recording.uploads]);

  for (const stream of recording.streams) {
    for (const track of stream.getTracks()) track.stop();
  }
  await recording.audioContext.close();

  return { uploaded: recording.uploaded, failed: recording.failed };
}

/** Resolves once the recorder has emitted its final blob and gone inactive. */
function stopRecorder(recorder: MediaRecorder): Promise<void> {
  if (recorder.state === 'inactive') return Promise.resolve();

  return new Promise((resolve) => {
    recorder.addEventListener('stop', () => resolve(), { once: true });
    recorder.stop();
  });
}
