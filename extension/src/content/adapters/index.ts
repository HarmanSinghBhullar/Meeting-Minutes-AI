/**
 * Platform adapters.
 *
 * One per meeting platform, all behind the same interface, so that a redesign at
 * Zoom cannot break Meet. An adapter that returns nothing is not an error — it
 * means the backend falls back to pyannote diarization for that meeting, and the
 * speakers come out anonymous rather than named.
 */

import type { Participant, Platform } from '@/lib/types';
import { MeetAdapter } from './meet';
import { TeamsAdapter } from './teams';
import { ZoomAdapter } from './zoom';

/**
 * One interval during which the meeting UI showed someone as the active speaker.
 *
 * Timestamps are wall-clock (`Date.now()`), not recording-relative. An adapter
 * watches the page and has no idea when recording started — that is the content
 * script's business — so it reports when things happened and lets the caller
 * rebase. Trying to make the adapter recording-aware couples it to a lifecycle
 * it does not participate in.
 */
export interface ObservedTurn {
  speakerName: string;
  speakerExternalRef?: string;
  startedAt: number;
  endedAt: number;
  /**
   * How the name was arrived at. `dom` (the default) means the UI showed this
   * person as the active *speaker*; `presenter` means they were *sharing their
   * screen* while audio played and no one was flagged speaking — a weaker signal
   * that real speaking overrides during alignment. Absent is treated as `dom`.
   */
  source?: 'dom' | 'presenter';
}

export interface MeetingAdapter {
  /** Everyone currently in the meeting, as the UI lists them. */
  getParticipants(): Participant[];

  /** The meeting's title, if the page exposes one. */
  getTitle(): string | null;

  /**
   * Whether the user has actually *joined* the call — past any pre-join / green
   * room screen and into the meeting proper.
   *
   * This drives the auto-record nudge, so it must not fire while someone is still
   * setting up their camera. It is a best-effort read of the meeting UI: an
   * adapter that cannot tell should return `false`, which simply means no nudge on
   * that platform and recording stays a manual click — a safe degradation, the
   * same one an unimplemented adapter already makes for attribution.
   */
  isInCall(): boolean;

  /** Start watching. Turns are emitted as they *close*, not as they open. */
  observe(onTurn: (turn: ObservedTurn) => void): void;

  /**
   * Stop watching and emit any turn still open.
   *
   * Called when recording stops. Without it, whoever was talking at the end
   * loses their last turn — and the end of a meeting is where the action items
   * live, so that is an expensive thing to drop.
   */
  disconnect(): void;
}

export function getAdapter(platform: Platform): MeetingAdapter | null {
  switch (platform) {
    case 'meet':
      return new MeetAdapter();
    case 'zoom':
      return new ZoomAdapter();
    case 'teams':
      return new TeamsAdapter();
    case 'other':
      return null;
  }
}
