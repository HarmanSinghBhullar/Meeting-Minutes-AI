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
 */

import { uploadChunk } from '@/lib/api';
import type { ExtensionMessage, Track } from '@/lib/types';

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
}

let active: ActiveRecording | null = null;

chrome.runtime.onMessage.addListener((message: ExtensionMessage) => {
  if (message.type === 'OFFSCREEN_START') {
    void start(message.streamId, message.meetingId);
  } else if (message.type === 'OFFSCREEN_STOP') {
    stop();
  }
});

async function start(streamId: string, meetingId: string): Promise<void> {
  if (active) return;

  // The tab's audio: the remote participants.
  const tabStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: 'tab',
        chromeMediaSourceId: streamId,
      },
    },
  } as MediaStreamConstraints);

  // The microphone: the local user. Permission must already have been granted
  // via the permission page — an offscreen document cannot show a prompt.
  const micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });

  // Keep the meeting audible. Capturing a tab diverts its audio away from the
  // speakers; routing it to the destination puts it back.
  const audioContext = new AudioContext();
  audioContext.createMediaStreamSource(tabStream).connect(audioContext.destination);

  const recorders = [
    createRecorder(tabStream, 'tab', meetingId),
    createRecorder(micStream, 'mic', meetingId),
  ];

  for (const recorder of recorders) recorder.start(TIMESLICE_MS);

  active = { meetingId, recorders, streams: [tabStream, micStream], audioContext };
}

/**
 * Wire one stream to one recorder, uploading each timeslice as it arrives.
 *
 * WebM is a streaming container, so the chunks a timesliced MediaRecorder emits
 * append into a valid file server-side. That is what makes incremental upload
 * work at all.
 */
function createRecorder(stream: MediaStream, track: Track, meetingId: string): MediaRecorder {
  const recorder = new MediaRecorder(stream, { mimeType: 'audio/webm;codecs=opus' });

  recorder.ondataavailable = (event: BlobEvent) => {
    if (event.data.size === 0) return;
    // Fire-and-forget: a slow upload must not stall the recorder. A failed chunk
    // is a hole in the transcript, so this is where a retry queue belongs.
    void uploadChunk(meetingId, track, event.data).catch((err: unknown) => {
      console.error(`[recorder] ${track} chunk upload failed`, err);
    });
  };

  return recorder;
}

function stop(): void {
  if (!active) return;

  for (const recorder of active.recorders) {
    if (recorder.state !== 'inactive') recorder.stop();
  }
  for (const stream of active.streams) {
    for (const track of stream.getTracks()) track.stop();
  }
  void active.audioContext.close();

  active = null;
}
