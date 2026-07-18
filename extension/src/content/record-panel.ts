/**
 * The in-page record panel.
 *
 * A small floating card injected into the meeting tab so recording can be started
 * and stopped without opening the toolbar popup. It is the content script's own
 * UI, drawn straight into the page — which is exactly why it lives behind a **Shadow
 * DOM**: Meet's stylesheet cannot reach in and restyle it, and its styles cannot
 * leak out onto Meet. Everything is inline; a content script has no bundled CSS file
 * to link.
 *
 * It renders one of a few states at a time (`joinPrompt`, `starting`, `recording`,
 * `error`) and never more. The panel does not *decide* anything — it reports clicks
 * through the callbacks it is given and lets the content script own the state
 * machine. In particular it never starts recording itself; the button only asks.
 *
 * The recording state deliberately keeps the same blunt consent notice the popup
 * shows. A person recorded without knowing is the failure this whole product is
 * careful about, and the panel is the most visible surface to say so on.
 */

const HOST_ID = 'meeting-intelligence-record-panel';

/** A red dot, so the card reads at a glance even before the text. */
const DOT = (color: string): string =>
  `<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${color};flex:0 0 auto"></span>`;

export class RecordPanel {
  private host: HTMLElement | null = null;
  private root: ShadowRoot | null = null;

  /** The card, ready to have its inner HTML swapped per state. Created lazily so
   *  merely importing this module touches no DOM. */
  private card(): HTMLElement {
    if (this.host && this.root) {
      return this.root.querySelector<HTMLElement>('.card') as HTMLElement;
    }

    const host = document.createElement('div');
    host.id = HOST_ID;
    // The host itself is inert layout: fixed, top of the stacking order, and
    // click-through except where the card sits.
    host.style.cssText =
      'position:fixed;right:16px;bottom:16px;z-index:2147483647;pointer-events:none';

    const root = host.attachShadow({ mode: 'open' });
    root.innerHTML = `
      <style>
        .card {
          pointer-events:auto;
          font:13px/1.4 system-ui,-apple-system,sans-serif;
          color:#0f172a;
          background:#fff;
          border:1px solid #e2e8f0;
          border-radius:10px;
          box-shadow:0 6px 24px rgba(0,0,0,.18);
          padding:12px 14px;
          width:250px;
        }
        .row { display:flex; align-items:center; gap:8px; }
        .title { font-weight:600; }
        .sub { color:#64748b; font-size:12px; margin:6px 0 10px; }
        .actions { display:flex; gap:8px; }
        button {
          font:inherit; font-weight:600; cursor:pointer;
          border-radius:7px; padding:7px 10px; border:1px solid transparent;
        }
        .primary { background:#d93025; color:#fff; flex:1; }
        .primary:hover { background:#b3271b; }
        .primary:disabled { opacity:.6; cursor:default; }
        .ghost { background:#f1f5f9; color:#334155; }
        .ghost:hover { background:#e2e8f0; }
        .x {
          pointer-events:auto; margin-left:auto; border:0; background:none;
          color:#94a3b8; font-size:16px; line-height:1; cursor:pointer; padding:0 2px;
        }
        .err { color:#b3271b; font-size:12px; margin:6px 0 10px; }
        .hint { color:#64748b; font-size:11px; margin-top:8px; text-align:center; }
        .hint b { color:#334155; }
        @media (prefers-color-scheme: dark) { .hint b { color:#cbd5e1; } }
        @media (prefers-color-scheme: dark) {
          .card { color:#e2e8f0; background:#1e293b; border-color:#334155; }
          .sub { color:#94a3b8; }
          .ghost { background:#334155; color:#e2e8f0; }
          .ghost:hover { background:#475569; }
        }
      </style>
      <div class="card"></div>
    `;

    document.body.appendChild(host);
    this.host = host;
    this.root = root;
    return root.querySelector<HTMLElement>('.card') as HTMLElement;
  }

  /** Wire a click handler onto an element inside the card by selector. */
  private on(selector: string, handler: () => void): void {
    this.root?.querySelector<HTMLElement>(selector)?.addEventListener('click', handler);
  }

  /**
   * "You're in a meeting — record it?" The card's resting state.
   *
   * `shortcut` is shown as the reliable start path. Chrome only lets recording
   * begin from something that "invokes the extension" — a toolbar click or this
   * keyboard shortcut — never from a click on this in-page button, so the button
   * is offered as a convenience that may be refused, and the shortcut as the one
   * that always works.
   */
  joinPrompt(onStart: () => void, onDismiss: () => void, shortcut: string): void {
    this.card().innerHTML = `
      <div class="row">
        ${DOT('#d93025')}
        <span class="title">Meeting detected</span>
        <button class="x" title="Dismiss" data-x>&times;</button>
      </div>
      <div class="sub">Record it for a transcript and minutes?</div>
      <div class="actions">
        <button class="primary" data-start>Start recording</button>
      </div>
      ${shortcutHint(shortcut)}
    `;
    this.on('[data-start]', onStart);
    this.on('[data-x]', onDismiss);
  }

  /** After the button is clicked, while the service worker spins the recorder up. */
  starting(): void {
    this.card().innerHTML = `
      <div class="row">
        ${DOT('#d93025')}
        <span class="title">Starting…</span>
      </div>
      <div class="sub">Requesting microphone and tab audio.</div>
    `;
  }

  /** Recording, with a stop control and the consent notice. */
  recording(onStop: () => void): void {
    this.card().innerHTML = `
      <div class="row">
        ${DOT('#d93025')}
        <span class="title">Recording</span>
      </div>
      <div class="sub">Everyone in the meeting should know.</div>
      <div class="actions">
        <button class="ghost" data-stop>Stop &amp; save</button>
      </div>
    `;
    this.on('[data-stop]', onStop);
  }

  /**
   * The start request came back a failure — most likely Chrome refusing tab
   * capture outside a toolbar-click gesture. Say what to do instead, and offer a
   * retry for the transient causes (backend down, recorder busy).
   */
  error(message: string, onRetry: () => void, onDismiss: () => void, shortcut: string): void {
    // With a shortcut bound, lead with it — it is the path that actually works when
    // the button was refused. Without one, fall back to pointing at the toolbar icon.
    const guidance = shortcut
      ? `Press <b>${escapeHtml(shortcut)}</b> to record — that always works. Or click the extension icon.`
      : 'Click the extension icon to record, or set a shortcut at chrome://extensions/shortcuts.';
    this.card().innerHTML = `
      <div class="row">
        ${DOT('#f59e0b')}
        <span class="title">Couldn't start</span>
        <button class="x" title="Dismiss" data-x>&times;</button>
      </div>
      <div class="err">${escapeHtml(message)}</div>
      <div class="sub">${guidance}</div>
      <div class="actions">
        <button class="primary" data-retry>Try again</button>
      </div>
    `;
    this.on('[data-retry]', onRetry);
    this.on('[data-x]', onDismiss);
  }

  /** Remove the panel entirely. Safe to call when nothing is shown. */
  hide(): void {
    this.host?.remove();
    this.host = null;
    this.root = null;
  }
}

/**
 * A one-line hint under the Start button telling the user the shortcut that always
 * works. Rendered only when a shortcut is actually bound — an empty line promising a
 * key that does nothing would be worse than silence.
 */
function shortcutHint(shortcut: string): string {
  if (!shortcut) return '';
  return `<div class="hint">or press <b>${escapeHtml(shortcut)}</b></div>`;
}

/** Minimal escaping for the one place we render a message string into HTML. */
function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
