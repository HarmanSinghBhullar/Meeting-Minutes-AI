/**
 * Microsoft Teams (web client) adapter.
 *
 * Not yet implemented. Same approach as Meet: calibrate against a live call to
 * find the speaking indicator (Teams rings the speaker's avatar), then fill in
 * the selectors. As with Zoom, only the web client is visible to an extension;
 * the desktop app falls back to diarization.
 */

import type { Participant } from '@/lib/types';
import type { MeetingAdapter, ObservedTurn } from './index';

export class TeamsAdapter implements MeetingAdapter {
  getParticipants(): Participant[] {
    return [];
  }

  getTitle(): string | null {
    return document.title || null;
  }

  observe(_onTurn: (turn: ObservedTurn) => void): void {
    // No timeline emitted; the backend falls back to diarization.
  }

  disconnect(): void {
    // Nothing observed, nothing to tear down.
  }
}
