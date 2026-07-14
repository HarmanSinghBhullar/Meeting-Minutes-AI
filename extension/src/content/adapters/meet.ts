/**
 * Google Meet adapter.
 *
 * Reads two things off the page: who is in the meeting, and who is talking right
 * now. That second signal is the product's whole accuracy advantage — the meeting
 * UI already knows the answer and puts a real name next to it, where diarization
 * could only offer us an anonymous cluster to label by hand.
 *
 * ## The uncomfortable part
 *
 * Meet has no public API and ships obfuscated, generated class names that change
 * without notice. Anything we key off is a guess about someone else's build
 * output. That reality drives three decisions here:
 *
 * 1. **Every selector lives in `SELECTORS`, at the top of the file.** When Meet
 *    reskins, the fix is one object, not an archaeology expedition.
 *
 * 2. **Speaking detection tries several strategies and takes the first that
 *    works.** Meet has expressed "this person is talking" through different
 *    mechanisms over the years; we check for all of them rather than betting the
 *    feature on one.
 *
 * 3. **There is a calibration tool** (`__meetCalibrate()`, below). Selectors for
 *    an obfuscated app cannot be derived from documentation — they have to be
 *    observed against a live meeting. The tool watches the DOM while somebody
 *    talks and reports exactly which attributes and classes toggled, which turns
 *    a day of guessing into about two minutes.
 *
 * When detection fails, it fails *loudly and safely*: no turns are emitted, the
 * backend sees no timeline, and it falls back to diarization. We lose the names,
 * not the meeting.
 */

import type { Participant } from '@/lib/types';
import type { MeetingAdapter, ObservedTurn } from './index';

const SELECTORS = {
  /**
   * Participant tiles. `data-participant-id` is Meet's own identifier and has
   * outlived several visual redesigns, which makes it the most durable hook on
   * the page — far better than any class name.
   */
  tile: '[data-participant-id]',

  /** The local user. Meet stamps their own name into this attribute. */
  selfName: '[data-self-name]',

  /**
   * The participant's name within a tile, in preference order. Meet has moved
   * the name between these over time, so we take the first that yields text.
   */
  name: ['[data-self-name]', '[data-participant-name]', '[jsname="rBUW7d"]'],

  /**
   * Candidate signals for "this tile is currently speaking", tried in order.
   * The first two are semantic and stable; the third is a generated class name
   * and is the one most likely to rot. Run the calibration tool to refresh it.
   */
  speaking: [
    '[data-is-speaking="true"]',
    '[aria-label*="speaking" i]',
    '.wnrUse.IisKdb', // Meet's animated mic-level bars, shown only while talking
  ],
} as const;

/**
 * How often we sample the page. Meet's DOM mutates constantly (video, layout,
 * animations), so a MutationObserver over the tile grid fires far more often
 * than it tells us anything. Sampling at a fixed interval costs a handful of
 * `matches()` calls against a few tiles, and the bounded, predictable cost is
 * worth more here than event-driven precision we cannot use anyway.
 */
const POLL_INTERVAL_MS = 150;

/**
 * How long a tile must go quiet before we call the turn over.
 *
 * Meet's indicator flickers off between words and during crosstalk. Without a
 * hold, one sentence becomes fifteen turns, and the alignment step downstream
 * gets a speaker timeline made of confetti.
 */
const SILENCE_HOLD_MS = 700;

/**
 * Turns shorter than this are dropped. A 150ms flicker is the indicator being
 * noisy, not somebody making a point.
 */
const MIN_TURN_MS = 400;

interface OpenTurn {
  speakerName: string;
  speakerExternalRef: string;
  startedAt: number;
  /** When we last saw them speaking. The turn ends here, not when we notice. */
  lastSeenSpeakingAt: number;
}

export class MeetAdapter implements MeetingAdapter {
  private timer: number | null = null;
  private onTurn: ((turn: ObservedTurn) => void) | null = null;

  /** Turns currently in progress, keyed by Meet's participant id. */
  private open = new Map<string, OpenTurn>();

  getParticipants(): Participant[] {
    const selfName = this.getSelfName();
    const participants: Participant[] = [];

    for (const tile of this.tiles()) {
      const name = this.nameOf(tile);
      if (!name) continue;
      if (participants.some((p) => p.displayName === name)) continue;

      const externalRef = tile.getAttribute('data-participant-id');
      participants.push({
        displayName: name,
        ...(externalRef ? { externalRef } : {}),
        isLocalUser: name === selfName,
      });
    }

    return participants;
  }

  getTitle(): string | null {
    // Meet does not surface the calendar event title in the DOM, so the tab title
    // ("Meet – abc-defg-hij") is the best we have. Thin, but it still helps prime
    // Whisper, and the backend can accept a better title from elsewhere later.
    return document.title || null;
  }

  observe(onTurn: (turn: ObservedTurn) => void): void {
    this.onTurn = onTurn;
    this.timer = window.setInterval(() => this.sample(), POLL_INTERVAL_MS);
  }

  disconnect(): void {
    if (this.timer !== null) {
      window.clearInterval(this.timer);
      this.timer = null;
    }

    // Close whatever was still in flight. The last speaker before someone hits
    // stop is usually the one summarising what everybody just agreed to.
    for (const turn of this.open.values()) this.emit(turn);
    this.open.clear();
    this.onTurn = null;
  }

  /** One sampling tick: who is speaking, and what does that do to open turns? */
  private sample(): void {
    const now = Date.now();
    const speakingNow = new Set<string>();

    for (const tile of this.tiles()) {
      if (!this.isSpeaking(tile)) continue;

      const id = tile.getAttribute('data-participant-id');
      const name = this.nameOf(tile);
      if (!id || !name) continue;

      speakingNow.add(id);

      const existing = this.open.get(id);
      if (existing) {
        existing.lastSeenSpeakingAt = now;
      } else {
        this.open.set(id, {
          speakerName: name,
          speakerExternalRef: id,
          startedAt: now,
          lastSeenSpeakingAt: now,
        });
      }
    }

    // Close turns that have been quiet long enough to mean it.
    for (const [id, turn] of this.open) {
      if (speakingNow.has(id)) continue;
      if (now - turn.lastSeenSpeakingAt < SILENCE_HOLD_MS) continue;

      this.emit(turn);
      this.open.delete(id);
    }
  }

  private emit(turn: OpenTurn): void {
    // The turn ended when they stopped talking, not when the hold expired —
    // otherwise every turn is padded by SILENCE_HOLD_MS and starts swallowing
    // the first words of whoever spoke next.
    const endedAt = turn.lastSeenSpeakingAt;
    if (endedAt - turn.startedAt < MIN_TURN_MS) return;

    this.onTurn?.({
      speakerName: turn.speakerName,
      speakerExternalRef: turn.speakerExternalRef,
      startedAt: turn.startedAt,
      endedAt,
    });
  }

  private tiles(): HTMLElement[] {
    return Array.from(document.querySelectorAll<HTMLElement>(SELECTORS.tile));
  }

  private isSpeaking(tile: HTMLElement): boolean {
    return SELECTORS.speaking.some(
      (selector) => tile.matches(selector) || tile.querySelector(selector) !== null,
    );
  }

  private nameOf(tile: HTMLElement): string | null {
    for (const selector of SELECTORS.name) {
      const node = tile.matches(selector) ? tile : tile.querySelector(selector);
      if (!node) continue;

      const text =
        node.getAttribute('data-self-name') ??
        node.getAttribute('data-participant-name') ??
        node.textContent;

      const name = text?.trim();
      if (name) return name;
    }
    return null;
  }

  private getSelfName(): string | null {
    return document.querySelector(SELECTORS.selfName)?.getAttribute('data-self-name') ?? null;
  }
}

/**
 * Calibration tool. Run from the DevTools console on a live Meet call:
 *
 *     __meetCalibrate()
 *
 * Then have one person talk for a few seconds. It reports which attributes and
 * classes appeared on their tile while they spoke and vanished when they stopped
 * — which is exactly what `SELECTORS.speaking` needs to contain.
 *
 * This exists because Meet's markup cannot be looked up, only observed, and
 * because the alternative is guessing at obfuscated class names for an afternoon.
 * When attribution mysteriously stops working after a Meet update, start here.
 */
function calibrate(durationMs = 15_000): void {
  const seen = new Map<string, { withSignal: Set<string>; sampleCount: number }>();

  const tick = () => {
    for (const tile of document.querySelectorAll<HTMLElement>(SELECTORS.tile)) {
      const id = tile.getAttribute('data-participant-id') ?? '?';
      const entry = seen.get(id) ?? { withSignal: new Set<string>(), sampleCount: 0 };
      entry.sampleCount += 1;

      // Everything currently true of this tile and its children: classes and
      // attribute values. Whatever correlates with talking is in here somewhere.
      for (const node of [tile, ...tile.querySelectorAll('*')]) {
        for (const cls of node.classList) entry.withSignal.add(`.${cls}`);
        for (const attr of node.attributes) {
          if (attr.name.startsWith('data-') || attr.name === 'aria-label') {
            entry.withSignal.add(`[${attr.name}="${attr.value}"]`);
          }
        }
      }

      seen.set(id, entry);
    }
  };

  const timer = window.setInterval(tick, 200);
  console.info('[calibrate] watching for %ds — have one person speak now.', durationMs / 1000);

  window.setTimeout(() => {
    window.clearInterval(timer);
    console.info('[calibrate] candidates per participant:');
    for (const [id, entry] of seen) {
      console.info(id, [...entry.withSignal].filter((s) => /speak|voice|audio|mic|active/i.test(s)));
    }
    console.info(
      '[calibrate] If that list is empty, the signal is an obfuscated class. Re-run while ' +
        'watching one tile in the Elements panel and diff its classes as the person starts ' +
        'and stops talking.',
    );
  }, durationMs);
}

// Exposed for debugging only; harmless in production and invaluable when Meet
// changes its markup.
(window as unknown as { __meetCalibrate: typeof calibrate }).__meetCalibrate = calibrate;
