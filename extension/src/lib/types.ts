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

/* --- Read models, as returned by the API --- */

/** How a speaker was identified. The UI shows a guessed name differently from a
 *  known one, so the user knows which to double-check. */
export type SpeakerSource = 'local_track' | 'dom' | 'diarization' | 'manual' | 'unknown';

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
  source: SpeakerSource;
  isLocalUser: boolean;
}

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
