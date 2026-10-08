/**
 * Zoom (web client) adapter.
 *
 * Not yet implemented. Zoom's web DOM needs the same treatment Meet got: run the
 * calibration tool from `meet.ts` against a live call to find the speaking
 * indicator, then fill in the selectors here.
 *
 * Worth knowing before anyone budgets time for this: only the *browser* client is
 * reachable from an extension at all. A participant in the Zoom desktop app
 * produces no DOM for us to read, and those meetings fall back to pyannote
 * diarization — anonymous speakers that a human then names. The desktop app is
 * what most people actually use, which caps how much this adapter can ever be
 * worth relative to Meet.
 */

import type { Participant } from '@/lib/types';
import type { MeetingAdapter, ObservedTurn } from './index';

export class ZoomAdapter implements MeetingAdapter {
  getParticipants(): Participant[] {
    return [];
  }

  getTitle(): string | null {
    return document.title || null;
  }

  isInCall(): boolean {
    // Unimplemented, like the rest of this adapter: no reliable in-call selector
    // yet, so no auto-record nudge on Zoom and recording stays a manual click.
    return false;
  }

  observe(_onTurn: (turn: ObservedTurn) => void): void {
    // No timeline emitted, so the backend falls back to diarization. That is the
    // designed degradation, not a bug — we lose the names, not the meeting.
  }

  disconnect(): void {
    // Nothing observed, nothing to tear down.
  }
}
