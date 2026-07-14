/** Client for the backend API.
 *
 * The API speaks snake_case; the extension speaks camelCase. The mapping happens
 * here and nowhere else, so no component has to know what the wire format looks
 * like.
 */

import type {
  Meeting,
  Minutes,
  MinutesItem,
  Participant,
  Platform,
  Speaker,
  SpeakerEvent,
  Track,
  TranscriptSegment,
} from './types';

const BASE_URL = 'http://127.0.0.1:8000/api/v1';

export interface OpenMeetingRequest {
  id: string;
  title?: string;
  platform: Platform;
  meetingUrl?: string;
  agenda?: string;
  speakers: Participant[];
}

/** Open a meeting. Called when recording starts, before any audio is sent. */
export async function openMeeting(req: OpenMeetingRequest): Promise<void> {
  await post('/recordings/meetings', {
    id: req.id,
    title: req.title,
    platform: req.platform,
    meeting_url: req.meetingUrl,
    agenda: req.agenda,
    speakers: req.speakers.map((p) => ({
      display_name: p.displayName,
      external_ref: p.externalRef,
      is_local_user: p.isLocalUser,
    })),
  });
}

/**
 * Upload one timesliced audio chunk.
 *
 * Chunks go up while the meeting is still running rather than as one blob at the
 * end. An hour of audio is a lot to hold in memory and lose to a crash; this way
 * a crash costs the last few seconds.
 */
export async function uploadChunk(
  meetingId: string,
  track: Track,
  chunk: Blob,
): Promise<void> {
  const form = new FormData();
  form.append('track', track);
  form.append('chunk', chunk, `${track}.webm`);

  const res = await fetch(`${BASE_URL}/recordings/meetings/${meetingId}/chunks`, {
    method: 'POST',
    body: form,
  });
  if (!res.ok) throw new Error(`Chunk upload failed: ${res.status}`);
}

/** Post a batch of active-speaker intervals observed in the meeting UI. */
export async function postSpeakerEvents(
  meetingId: string,
  events: SpeakerEvent[],
): Promise<void> {
  if (events.length === 0) return;
  await post(`/recordings/meetings/${meetingId}/speaker-events`, {
    events: events.map((e) => ({
      speaker_name: e.speakerName,
      speaker_external_ref: e.speakerExternalRef,
      start_ms: e.startMs,
      end_ms: e.endMs,
    })),
  });
}

/** Close the recording and queue it for transcription. */
export async function finalizeMeeting(meetingId: string): Promise<void> {
  await post(`/recordings/meetings/${meetingId}/finalize`, {
    ended_at: new Date().toISOString(),
  });
}

/* --- Read side --- */

/** Recent meetings, newest first. */
export async function listMeetings(limit = 20): Promise<Meeting[]> {
  const rows = await get<RawMeeting[]>(`/meetings?limit=${limit}`);
  return rows.map(toMeeting);
}

export async function getMeeting(meetingId: string): Promise<Meeting> {
  return toMeeting(await get<RawMeeting>(`/meetings/${meetingId}`));
}

export async function getTranscript(meetingId: string): Promise<TranscriptSegment[]> {
  const rows = await get<RawSegment[]>(`/meetings/${meetingId}/transcript`);
  return rows.map((s) => ({
    id: s.id,
    index: s.index,
    startMs: s.start_ms,
    endMs: s.end_ms,
    text: s.text,
    textEn: s.text_en,
    speakerId: s.speaker_id,
    speakerSource: s.speaker_source,
  }));
}

/**
 * The minutes, or null if they have not been generated yet.
 *
 * A 404 here is a normal state, not an error — transcription and extraction take
 * minutes, and the page is expected to be opened while they are still running.
 */
export async function getMinutes(meetingId: string): Promise<Minutes | null> {
  const res = await fetch(`${BASE_URL}/meetings/${meetingId}/minutes`);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`minutes failed: ${res.status}`);

  const raw = (await res.json()) as RawMinutes;
  return {
    id: raw.id,
    meetingId: raw.meeting_id,
    summary: raw.summary,
    version: raw.version,
    items: raw.items.map(
      (i): MinutesItem => ({
        id: i.id,
        type: i.type,
        text: i.text,
        ownerSpeakerId: i.owner_speaker_id,
        dueDate: i.due_date,
        segmentIds: i.segment_ids,
        isGrounded: i.is_grounded,
        groundingNote: i.grounding_note,
      }),
    ),
  };
}

/* --- Wire types and mapping --- */

interface RawSpeaker {
  id: string;
  display_name: string;
  source: Speaker['source'];
  is_local_user: boolean;
}

interface RawMeeting {
  id: string;
  title: string | null;
  platform: Platform;
  started_at: string | null;
  ended_at: string | null;
  source_language: string | null;
  speakers: RawSpeaker[];
  jobs: { id: string; type: string; status: Job['status']; progress: number; error: string | null }[];
}

type Job = Meeting['jobs'][number];

interface RawSegment {
  id: string;
  index: number;
  start_ms: number;
  end_ms: number;
  text: string;
  text_en: string | null;
  speaker_id: string | null;
  speaker_source: TranscriptSegment['speakerSource'];
}

interface RawMinutes {
  id: string;
  meeting_id: string;
  summary: string | null;
  version: number;
  items: {
    id: string;
    type: MinutesItem['type'];
    text: string;
    owner_speaker_id: string | null;
    due_date: string | null;
    segment_ids: string[];
    is_grounded: boolean | null;
    grounding_note: string | null;
  }[];
}

function toMeeting(raw: RawMeeting): Meeting {
  return {
    id: raw.id,
    title: raw.title,
    platform: raw.platform,
    startedAt: raw.started_at,
    endedAt: raw.ended_at,
    sourceLanguage: raw.source_language,
    speakers: raw.speakers.map((s) => ({
      id: s.id,
      displayName: s.display_name,
      source: s.source,
      isLocalUser: s.is_local_user,
    })),
    jobs: raw.jobs,
  };
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`);
  if (!res.ok) throw new Error(`${path} failed: ${res.status}`);
  return (await res.json()) as T;
}

async function post(path: string, body: unknown): Promise<Response> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path} failed: ${res.status}`);
  return res;
}
