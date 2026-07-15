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
 * output. That reality drives four decisions here:
 *
 * 1. **Every selector lives in `SELECTORS`, at the top of the file.** When Meet
 *    reskins, the fix is one object, not an archaeology expedition.
 *
 * 2. **Speaking detection tries several strategies and takes the first that
 *    works.** Meet has expressed "this person is talking" through different
 *    mechanisms over the years; we check for all of them rather than betting the
 *    feature on one. Prefer semantic signals (`aria-*`, `data-*`) over class
 *    names — obfuscated classes are the first thing to rot.
 *
 * 3. **The adapter watches its own health.** If the meeting has participants but
 *    no speaking signal ever matches, that is the exact failure that fills a
 *    transcript with "Unknown Speaker" — so instead of failing silently, it says
 *    so loudly in the console and points at the fix. A recording full of
 *    `unknown` that nobody warned you about is the one output we cannot ship.
 *
 * 4. **There is a calibration tool** (`__meetCalibrate()`, below). Selectors for
 *    an obfuscated app cannot be derived from documentation — they have to be
 *    observed against a live meeting. The tool watches the DOM while somebody
 *    talks and reports exactly which attributes and classes *toggled* on their
 *    tile — which is what a speaking indicator does and what layout markup does
 *    not — turning a day of guessing into about two minutes.
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
   * `aria-label` is the last resort — durable, but noisier (it can carry status
   * text like "Priya, muted"), so `nameOf` trims it.
   */
  name: ['[data-self-name]', '[data-participant-name]', '[jsname="rBUW7d"]'],

  /**
   * Candidate signals for "this tile is currently speaking", tried in order.
   * The first two are semantic and stable; the third is a generated class name
   * and is the one most likely to rot.
   *
   * When remote speakers start coming out as "Unknown", this array is almost
   * always the reason. Run `__meetCalibrate()` (bottom of this file) on a live
   * call and paste the selector it reports here.
   */
  speaking: [
    '[data-is-speaking="true"]',
    '[aria-label*="speaking" i]',
    // Confirmed with __meetCalibrate() on 2026-07-15: the class Meet toggles onto
    // a tile while its participant is talking. Obfuscated, so it *will* rot — when
    // remote speakers start coming out "Unknown", re-run the calibration tool and
    // replace this. (The previous value, `.wnrUse.IisKdb`, had already rotted.)
    '.BlxGDf',
  ],

  /**
   * Signals that a participant is *presenting* (sharing their screen), tried in
   * order. This is the second attribution path, and a very different one: the
   * active-speaker indicator above only fires for a participant's **microphone**,
   * so audio from a shared video or slideshow lights up nothing and the tab track
   * comes out entirely "Unknown". When that audio is playing, the one thing the UI
   * *does* tell us is who is presenting — so we attribute it to them, at a lower
   * confidence that real speaking always overrides (see `alignment.py`).
   *
   * Semantic-only on purpose: presentation state is exposed through `aria-label`
   * ("… is presenting") far more durably than through any class, and a wrong
   * obfuscated guess here would silently mislabel a whole meeting. If screen-share
   * audio still comes out "Unknown", these are what to refresh.
   */
  presenting: [
    '[data-is-presenting="true"]',
    '[aria-label*="is presenting" i]',
    '[aria-label*="presentation" i]',
  ],
} as const;

/**
 * Meet suffixes the local participant's name with a localized "(You)" — in the
 * tile, the participant panel, or an accessible label. It is the single most
 * durable "this is me" signal the page exposes (far steadier than any class), so
 * we both detect self by it and strip it back off for the display name.
 */
const SELF_SUFFIX = /\s*\(\s*you\s*\)\s*$/i;
/** The same marker, unanchored, for scanning a tile's whole text/label. */
const SELF_MARKER = /\(\s*you\s*\)/i;

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

/**
 * After this long with participants on screen but not one speaking signal ever
 * matched, we conclude `SELECTORS.speaking` no longer matches Meet's markup —
 * the exact failure that fills a transcript with "Unknown Speaker" — and warn,
 * with the fix. The grace period allows for a genuinely quiet opening.
 */
const ATTRIBUTION_HEALTH_GRACE_MS = 20_000;

/**
 * How long the presenting signal may drop before we call a presentation over.
 * Far longer than `SILENCE_HOLD_MS`: a screen share is one continuous interval,
 * not a flicker, so the only thing this guards against is a momentary miss while
 * Meet re-lays-out the grid. We would rather bridge a one-second gap than split a
 * twenty-minute demo into two.
 */
const PRESENTER_HOLD_MS = 3_000;

interface OpenTurn {
  speakerName: string;
  speakerExternalRef: string;
  startedAt: number;
  /** When we last saw them speaking. The turn ends here, not when we notice. */
  lastSeenSpeakingAt: number;
}

/** A presentation in progress: one continuous interval, attributed to the sharer. */
interface OpenPresentation {
  speakerName: string;
  speakerExternalRef: string | null;
  startedAt: number;
  lastSeenAt: number;
}

export class MeetAdapter implements MeetingAdapter {
  private timer: number | null = null;
  private onTurn: ((turn: ObservedTurn) => void) | null = null;

  /** Turns currently in progress, keyed by Meet's participant id. */
  private open = new Map<string, OpenTurn>();

  /** The presentation in progress, if someone is sharing their screen. */
  private presenter: OpenPresentation | null = null;

  // --- Attribution health. These exist so a silent selector failure becomes a
  // loud, actionable warning instead of a transcript full of "Unknown". ---
  private observeStartedAt = 0;
  private sawAnySpeaker = false;
  private turnsEmitted = 0;
  private healthWarned = false;

  getParticipants(): Participant[] {
    const participants: Participant[] = [];

    for (const tile of this.tiles()) {
      const raw = this.nameOf(tile);
      if (!raw) continue;

      // Decide self *before* stripping, since the "(You)" marker is the signal;
      // then show the person by their real name, not "Priya Sharma (You)".
      const isLocalUser = this.isSelfTile(tile, raw);
      const name = this.displayName(raw);
      if (!name) continue;
      if (participants.some((p) => p.displayName === name)) continue;

      const externalRef = tile.getAttribute('data-participant-id');
      participants.push({
        displayName: name,
        ...(externalRef ? { externalRef } : {}),
        isLocalUser,
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
    this.observeStartedAt = Date.now();
    this.sawAnySpeaker = false;
    this.turnsEmitted = 0;
    this.healthWarned = false;
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

    // A presentation running right up to Stop covers the closing audio, so it is
    // exactly the interval worth keeping.
    if (this.presenter) this.emitPresenter(this.presenter);
    this.presenter = null;

    this.onTurn = null;

    // A one-line verdict on whether attribution worked. Zero turns after a real
    // meeting means the timeline was empty and every remote speaker will be
    // "Unknown" — worth flagging at the source rather than discovering it later
    // in the minutes.
    if (this.turnsEmitted === 0) {
      console.warn(
        '[meet] recording ended with 0 speaker turns — remote speakers will be ' +
          '"Unknown". SELECTORS.speaking likely no longer matches Meet; run ' +
          '__meetCalibrate() on a live call to find the current signal.',
      );
    } else {
      console.info('[meet] speaker timeline: %d turns emitted.', this.turnsEmitted);
    }
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

    if (speakingNow.size > 0) this.sawAnySpeaker = true;

    // Close turns that have been quiet long enough to mean it.
    for (const [id, turn] of this.open) {
      if (speakingNow.has(id)) continue;
      if (now - turn.lastSeenSpeakingAt < SILENCE_HOLD_MS) continue;

      this.emit(turn);
      this.open.delete(id);
    }

    this.samplePresenter(now);
    this.checkHealth(now);
  }

  /**
   * Track who is presenting, as one continuous interval per presentation.
   *
   * Unlike speaking, presenting does not flicker, so this is deliberately simple:
   * extend the open presentation while the same person shares, bridge a brief drop
   * (`PRESENTER_HOLD_MS`), and close it when the sharer changes or stops. The local
   * user is skipped — their own screen-share audio is not what the tab track
   * captures, and their speech is already the mic track.
   */
  private samplePresenter(now: number): void {
    const current = this.detectPresenter();

    if (current) {
      if (this.presenter && this.presenter.speakerName === current.name) {
        this.presenter.lastSeenAt = now;
      } else {
        if (this.presenter) this.emitPresenter(this.presenter);
        this.presenter = {
          speakerName: current.name,
          speakerExternalRef: current.ref,
          startedAt: now,
          lastSeenAt: now,
        };
      }
      return;
    }

    if (this.presenter && now - this.presenter.lastSeenAt >= PRESENTER_HOLD_MS) {
      this.emitPresenter(this.presenter);
      this.presenter = null;
    }
  }

  /**
   * Warn once if the meeting clearly has people in it but no speaking signal has
   * ever matched. This is the difference between "the room was silent" (fine) and
   * "our selectors are stale" (the transcript will be full of Unknown) — and only
   * the second is worth shouting about, so we require both a grace period and
   * visible participants before we do.
   */
  private checkHealth(now: number): void {
    if (this.healthWarned || this.sawAnySpeaker) return;
    if (now - this.observeStartedAt < ATTRIBUTION_HEALTH_GRACE_MS) return;

    const tileCount = this.tiles().length;
    if (tileCount === 0) return; // no meeting UI yet — nothing to conclude

    this.healthWarned = true;
    console.warn(
      '[meet] %d participant tile(s) on screen but no speaking signal has matched ' +
        'in %ds. Meet has almost certainly changed its markup, so every remote ' +
        'speaker will come out as "Unknown". Fix: run __meetCalibrate() in this ' +
        'console, have one person talk, and paste the reported selector into ' +
        'SELECTORS.speaking in adapters/meet.ts.',
      tileCount,
      ATTRIBUTION_HEALTH_GRACE_MS / 1000,
    );
  }

  private emit(turn: OpenTurn): void {
    // The turn ended when they stopped talking, not when the hold expired —
    // otherwise every turn is padded by SILENCE_HOLD_MS and starts swallowing
    // the first words of whoever spoke next.
    const endedAt = turn.lastSeenSpeakingAt;
    if (endedAt - turn.startedAt < MIN_TURN_MS) return;

    this.turnsEmitted += 1;
    this.onTurn?.({
      speakerName: turn.speakerName,
      speakerExternalRef: turn.speakerExternalRef,
      startedAt: turn.startedAt,
      endedAt,
    });
  }

  /**
   * Emit a presentation as a speaker turn tagged `presenter`.
   *
   * It counts towards attribution health the same way a speaking turn does: a
   * meeting whose only signal was a screen share still produced names, so it is
   * not the silent-selector failure the health check exists to catch.
   */
  private emitPresenter(p: OpenPresentation): void {
    const endedAt = p.lastSeenAt;
    if (endedAt - p.startedAt < MIN_TURN_MS) return;

    this.turnsEmitted += 1;
    this.onTurn?.({
      speakerName: p.speakerName,
      ...(p.speakerExternalRef ? { speakerExternalRef: p.speakerExternalRef } : {}),
      startedAt: p.startedAt,
      endedAt,
      source: 'presenter',
    });
  }

  /**
   * Who, if anyone, is presenting — by their real name.
   *
   * Returns null when nobody is sharing, or when the sharer is the local user
   * (their screen audio is not in the tab track we are trying to attribute).
   */
  private detectPresenter(): { name: string; ref: string | null } | null {
    for (const selector of SELECTORS.presenting) {
      for (const node of document.querySelectorAll<HTMLElement>(selector)) {
        // Require the indicator to sit inside a participant tile. Meet's toolbar
        // has its own "Present now" control whose label also mentions presenting,
        // and mislabelling a whole meeting to that is far worse than missing a
        // share — so we only trust an indicator anchored to a real person.
        const tile = node.closest<HTMLElement>(SELECTORS.tile);
        if (!tile) continue;

        const name = this.presenterName(node, tile);
        if (!name) continue;
        if (this.isSelfTile(tile, name)) return null;

        return { name, ref: tile.getAttribute('data-participant-id') };
      }
    }
    return null;
  }

  /** Pull the presenter's name out of a presentation label, else the tile. */
  private presenterName(node: HTMLElement, tile: HTMLElement): string | null {
    const label = node.getAttribute('aria-label') ?? tile.getAttribute('aria-label') ?? '';
    // "Priya is presenting" / "Priya's presentation" / "Priya is sharing …".
    const match = label.match(/^(.+?)(?:['’]s presentation| is presenting| is sharing)/i);
    if (match?.[1]) return this.displayName(match[1].trim());

    const name = this.nameOf(tile);
    return name ? this.displayName(name) : null;
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

    // Last resort: the tile's own accessible label. It is durable but can carry
    // trailing status ("Priya Sharma, muted" / "Priya Sharma is presenting"), so
    // we keep only the leading name-shaped part.
    return this.cleanLabel(tile.getAttribute('aria-label'));
  }

  /** Strip status noise from an aria-label so it is usable as a display name. */
  private cleanLabel(label: string | null): string | null {
    if (!label) return null;
    // Names do not contain commas; everything Meet appends (mute/presenting/pin
    // state) sits after one. Fall back to the whole string if there is no comma.
    const name = label.split(',')[0]?.trim();
    return name && !/\b(muted|presenting|pinned|unmute)\b/i.test(name) ? name : null;
  }

  private getSelfName(): string | null {
    return document.querySelector(SELECTORS.selfName)?.getAttribute('data-self-name') ?? null;
  }

  /**
   * Whether a tile represents the local user — the reason the transcript can say
   * "Priya Sharma (You)" instead of inventing a separate "You".
   *
   * Three signals, any of which is enough, most durable first:
   *   1. a `data-self-name` node inside the tile (Meet's explicit marker), or its
   *      value matching the visible name;
   *   2. the localized "(You)" suffix Meet appends to its own tile.
   *
   * `rawName` is passed in (rather than re-read) because the caller has already
   * done the work, and because it is the value the "(You)" test needs to run on.
   */
  private isSelfTile(tile: HTMLElement, rawName: string): boolean {
    if (tile.matches(SELECTORS.selfName) || tile.querySelector(SELECTORS.selfName)) {
      return true;
    }
    const selfName = this.getSelfName();
    if (selfName && this.displayName(rawName) === this.displayName(selfName)) return true;

    // The "(You)" badge is often a sibling of the name, not part of it, so scan
    // the tile's whole label/text rather than just the name we extracted.
    if (SELF_MARKER.test(rawName)) return true;
    const label = `${tile.getAttribute('aria-label') ?? ''} ${tile.textContent ?? ''}`;
    return SELF_MARKER.test(label);
  }

  /** A tile name as we want to store it: without Meet's trailing "(You)". */
  private displayName(raw: string): string {
    return raw.replace(SELF_SUFFIX, '').trim();
  }
}

interface CalibrateOptions {
  speakMs?: number;
  silentMs?: number;
}

/**
 * Calibration tool. Run from the DevTools console on a live Meet call, with the
 * console's context set to the extension's content script (the dropdown at the
 * top-left of the Console that says "top" — pick "Meeting Intelligence"):
 *
 *     __meetCalibrate()
 *
 * A single-pass "what changed" scan is useless here: Meet's DOM churns every
 * frame even in silence — tooltips, layout, per-frame measurement attributes,
 * stream ids — so everything looks like it toggled. This runs *two labelled
 * phases* and diffs them:
 *
 *   1. SPEAK  — talk continuously for a few seconds.
 *   2. SILENT — stop, and stay quiet.
 *
 * The speaking indicator is, by definition, the signal present on a tile while
 * you talk and absent while you do not. So we keep only what appeared in nearly
 * every SPEAK sample and almost no SILENT sample. That difference is what a
 * mic-level animation is and what layout chrome is not, and it collapses a
 * hundred candidates to the one or two worth pasting into `SELECTORS.speaking`.
 *
 * Keep the mouse still and away from the tiles while it runs — hovering a tile
 * summons its controls, which would otherwise look like a signal that toggled.
 */
function calibrate({ speakMs = 6_000, silentMs = 6_000 }: CalibrateOptions = {}): void {
  // Attributes whose values change on their own — measurement floats, stream ids,
  // tooltip ids, a mirror of the class list. Collecting them only feeds the diff
  // noise it then has to fight, so drop them at the source. (The diff would reject
  // most anyway; this keeps the console readable when it does not.)
  const VOLATILE_ATTR = new Set([
    'data-iml',
    'data-ssrc',
    'data-tooltip-id',
    'data-unique-tt-id',
    'data-idom-class',
    'data-tooltip-classes',
    'data-context',
    'data-resolution-cap',
    'data-scroll-target',
    'data-requested-participant-id',
    'data-tile-media-id',
    'data-tooltip-anchor-boundary-type',
    'data-tooltip-y-position',
  ]);
  // Material-framework classes: page chrome (buttons, menus), never a mic signal.
  const isFrameworkClass = (c: string): boolean =>
    c.startsWith('VfPpkd') || c.startsWith('VYBDae') || c === 'google-symbols';

  const signalsOf = (tile: HTMLElement): Set<string> => {
    const out = new Set<string>();
    for (const node of [tile, ...tile.querySelectorAll('*')]) {
      for (const cls of node.classList) if (!isFrameworkClass(cls)) out.add(`.${cls}`);
      for (const attr of node.attributes) {
        if ((attr.name === 'aria-label' || attr.name.startsWith('data-')) && !VOLATILE_ATTR.has(attr.name)) {
          out.add(`[${attr.name}="${attr.value}"]`);
        }
      }
    }
    return out;
  };

  /** Sample every tile's signals every 200ms for `durationMs`, counting presence. */
  const record = (
    durationMs: number,
    onDone: (counts: Map<string, number>, samples: number) => void,
  ): void => {
    const counts = new Map<string, number>();
    let samples = 0;
    const timer = window.setInterval(() => {
      samples += 1;
      const seen = new Set<string>();
      for (const tile of document.querySelectorAll<HTMLElement>(SELECTORS.tile)) {
        for (const sig of signalsOf(tile)) seen.add(sig);
      }
      for (const sig of seen) counts.set(sig, (counts.get(sig) ?? 0) + 1);
    }, 200);
    window.setTimeout(() => {
      window.clearInterval(timer);
      onDone(counts, samples);
    }, durationMs);
  };

  const banner = makeBanner();

  banner('🎙️ SPEAK NOW — keep talking…');
  record(speakMs, (speak, speakN) => {
    banner('🤫 STOP — stay silent…');
    record(silentMs, (silent, silentN) => {
      banner('✓ calibration done — see the console', 4_000);

      // Present while speaking, absent while silent. Thresholds are loose because
      // a mic animation pulses with the voice rather than sitting solidly on.
      const candidates: string[] = [];
      for (const [sig, c] of speak) {
        const speakRatio = c / Math.max(1, speakN);
        const silentRatio = (silent.get(sig) ?? 0) / Math.max(1, silentN);
        if (speakRatio >= 0.6 && silentRatio <= 0.25) candidates.push(sig);
      }

      if (candidates.length) {
        console.info(
          '[calibrate] SELECTORS.speaking candidates (present speaking, gone silent):\n%s',
          `[\n${candidates.map((s) => `  '${s}',`).join('\n')}\n]`,
        );
        console.info(
          '[calibrate] If several, prefer a semantic [data-*]/[aria-*] over a class, and ' +
            're-run once to confirm the pick is stable.',
        );
      } else {
        console.info(
          '[calibrate] Nothing cleanly separated speaking from silent. Re-run with a longer ' +
            'window and talk the whole time: __meetCalibrate({ speakMs: 10000, silentMs: 8000 }).',
        );
      }
    });
  });
}

/** A fixed on-screen banner, so you can watch the meeting instead of the console. */
function makeBanner(): (text: string, hideAfterMs?: number) => void {
  const el = document.createElement('div');
  el.style.cssText =
    'position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:2147483647;' +
    'padding:10px 16px;border-radius:8px;background:rgba(0,0,0,.85);color:#fff;' +
    'font:600 14px system-ui,sans-serif;pointer-events:none';
  document.body.appendChild(el);

  let hideTimer: number | null = null;
  return (text: string, hideAfterMs?: number): void => {
    el.textContent = text;
    if (hideTimer !== null) window.clearTimeout(hideTimer);
    if (hideAfterMs) hideTimer = window.setTimeout(() => el.remove(), hideAfterMs);
  };
}

// Exposed for debugging only; harmless in production and invaluable when Meet
// changes its markup.
(window as unknown as { __meetCalibrate: typeof calibrate }).__meetCalibrate = calibrate;
