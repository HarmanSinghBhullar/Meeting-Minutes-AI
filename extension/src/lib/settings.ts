/**
 * User settings for the auto-record helpers.
 *
 * Kept in `chrome.storage.local` so every context — popup, service worker,
 * content script — reads the same values, and they survive a service-worker
 * eviction (which, over a real meeting, is a certainty). Reads always fold the
 * stored value over `DEFAULT_SETTINGS`, so a settings object written by an older
 * build that lacks a newer key still comes back complete rather than `undefined`.
 */

import type { Settings } from './types';

const SETTINGS_KEY = 'settings';

/** The shipped defaults: both helpers on, both consent-safe (see `Settings`). */
export const DEFAULT_SETTINGS: Settings = {
  autoPromptOnJoin: true,
  autoStopOnLeave: true,
};

/** The current settings, with any missing keys filled from the defaults. */
export async function getSettings(): Promise<Settings> {
  const stored = await chrome.storage.local.get(SETTINGS_KEY);
  const saved = stored[SETTINGS_KEY] as Partial<Settings> | undefined;
  return { ...DEFAULT_SETTINGS, ...saved };
}

/** Merge a partial update into the stored settings and return the new whole. */
export async function updateSettings(patch: Partial<Settings>): Promise<Settings> {
  const next = { ...(await getSettings()), ...patch };
  await chrome.storage.local.set({ [SETTINGS_KEY]: next });
  return next;
}
