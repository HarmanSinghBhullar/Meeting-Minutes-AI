/** Shared types across the extension's four contexts. */

export type Platform = 'meet' | 'zoom' | 'teams' | 'other';

/** Which audio track a chunk belongs to. */
export type Track = 'mic' | 'tab';

/** A participant, as read from the meeting UI's participant list. */
export interface Participant {
  displayName: string;
  externalRef?: string;
  isLocalUser: boolean;
}

/**
 * One interval during which the meeting UI showed someone as the active speaker.
 *
 * This is the product's primary speaker-attribution signal: the meeting platform
 * already knows who is talking and puts their real name on screen, so reading it
 * beats asking a model to guess.
 *
 * Times are **milliseconds from the start of the recording**, so they line up
 * with the word timestamps the backend gets from Whisper. The adapters emit
 * wall-clock times; the content script rebases them onto the recording clock.
 */
export interface SpeakerEvent {
  speakerName: string;
  speakerExternalRef?: string;
  startMs: number;
  endMs: number;
  /** How the name was determined. Defaults to `dom` (active speaker) on the
   *  backend; `presenter` marks audio attributed to the screen-sharer instead. */
  source?: 'dom' | 'presenter';
}

/** What the content script knows about the meeting when recording starts. */
export interface MeetingContext {
  platform: Platform;
  title: string | null;
  meetingUrl: string;
  participants: Participant[];
}

/** Messages passed between the popup, the service worker, the content script,
 *  and the offscreen document. */
export type ExtensionMessage =
  // popup -> service worker
  | { type: 'START_RECORDING'; tabId: number }
  | { type: 'STOP_RECORDING' }
  | { type: 'GET_RECORDING_STATE' }

  // service worker -> offscreen document, which is the only context that can
  // hold a MediaRecorder (MV3 service workers have no DOM).
  | { type: 'OFFSCREEN_START'; streamId: string; meetingId: string }
  | { type: 'OFFSCREEN_STOP' }
  | { type: 'OFFSCREEN_GET_STATE' }

  // service worker -> content script
  | { type: 'GET_MEETING_CONTEXT' }
  | { type: 'RECORDING_STARTED'; startedAt: number }
  | { type: 'RECORDING_STOPPED' }

  // content script -> service worker
  | { type: 'SPEAKER_EVENTS'; events: SpeakerEvent[] };

export interface RecordingState {
  isRecording: boolean;
  meetingId?: string;
  startedAt?: number;
}

/**
 * The shape every reply to a `sendMessage` takes.
 *
 * A listener that returns without replying still *receives* the message, but the
 * promise `sendMessage` returned rejects. That combination — the work happens,
 * the caller is told it failed — is how a recording once kept running for ten
 * minutes after it had been stopped. So replies are mandatory, and typing them
 * is how we keep them that way.
 */
export type Reply<T> = { ok: true; result: T } | { ok: false; error: string };

/** Reply to `OFFSCREEN_GET_STATE`.
 *
 *  The offscreen document holds the MediaRecorder, so it — not the service
 *  worker's `state` variable — is the authority on whether audio is being
 *  captured. */
export interface OffscreenState {
  isRecording: boolean;
  meetingId?: string;
}

/** Reply to `OFFSCREEN_STOP`, sent only once every chunk has been uploaded.
 *
 *  `failed` is surfaced rather than swallowed: a chunk that never arrived is a
 *  silent hole in the transcript, and a hole nobody knows about is one nobody
 *  can account for when the minutes come out short. */
export interface StopResult {
  uploaded: number;
  failed: number;
}

/** Reply to `RECORDING_STOPPED`: the speaker turns the content script had not
 *  flushed yet, handed back directly so the service worker can post them before
 *  it finalizes the meeting. */
export interface StoppedResult {
  events: SpeakerEvent[];
}

/* --- Read models, as returned by the API --- */

/** How a speaker was identified. The UI shows a guessed name differently from a
 *  known one, so the user knows which to double-check. */
export type SpeakerSource =
  | 'local_track'
  | 'dom'
  | 'presenter'
  | 'diarization'
  | 'manual'
  | 'unknown';

export type MinutesItemType =
  | 'decision'
  | 'action_item'
  | 'open_question'
  | 'risk'
  | 'topic';

export type JobStatus = 'pending' | 'running' | 'succeeded' | 'failed';

export interface Speaker {
  id: string;
  displayName: string;
  /** `'diarization'` is the one value that means "this is not a person yet" —
   *  it is a voice the diarizer separated out that nobody has named. That makes
   *  it the meeting's unmapped-speaker signal, and it is why the dashboard can
   *  show "needs speakers" without asking the server anything extra. */
  source: SpeakerSource;
  isLocalUser: boolean;
  /** Marked by a human as not-a-participant: a shared video, hold music. Still
   *  in the transcript, kept out of the minutes. */
  isExcluded: boolean;
}

/** A voice the diarizer found and nobody has identified yet.
 *
 *  `samples` is the load-bearing field. "SPEAKER_01" identifies nobody; the
 *  longest thing that voice said usually identifies them instantly to anyone who
 *  was in the meeting. Without the samples this UI would be a guessing game. */
export interface SpeakerCluster {
  id: string;
  displayName: string;
  segmentCount: number;
  totalMs: number;
  samples: string[];
}

/** Everything the speaker-mapping panel needs, in one response. */
export interface SpeakerMapping {
  /** Unnamed voices, longest-talking first. */
  clusters: SpeakerCluster[];
  /** Roster participants a cluster can be mapped onto. Never includes the local
   *  user: their audio is the mic track, which is never diarized. */
  candidates: Speaker[];
  /** True when that resolution was the last one and the minutes are now queued. */
  minutesQueued: boolean;
}

/** How a cluster was identified. Exactly one field, matching the API. */
export type SpeakerResolution =
  | { targetSpeakerId: string }
  | { displayName: string }
  | { ignore: true };

export interface Job {
  id: string;
  type: string;
  status: JobStatus;
  progress: number;
  error: string | null;
}

export interface Meeting {
  id: string;
  title: string | null;
  platform: Platform;
  startedAt: string | null;
  endedAt: string | null;
  sourceLanguage: string | null;
  speakers: Speaker[];
  jobs: Job[];
}

export interface TranscriptSegment {
  id: string;
  index: number;
  startMs: number;
  endMs: number;
  text: string;
  textEn: string | null;
  speakerId: string | null;
  speakerSource: SpeakerSource;
}

export interface MinutesItem {
  id: string;
  type: MinutesItemType;
  text: string;
  ownerSpeakerId: string | null;
  dueDate: string | null;
  /** The transcript lines this was drawn from. The whole point: a claim you
   *  cannot check is a claim you eventually stop believing. */
  segmentIds: string[];
  /** False means the grounding pass could not support this from its citations.
   *  Shown, never hidden — a reader who can see what was rejected has a reason
   *  to trust what was kept. */
  isGrounded: boolean | null;
  groundingNote: string | null;
}

export interface Minutes {
  id: string;
  meetingId: string;
  summary: string | null;
  version: number;
  items: MinutesItem[];
}
