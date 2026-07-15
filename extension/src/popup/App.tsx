/**
 * Popup: start and stop recording, and get to the minutes.
 *
 * The Record button is also the user gesture that chrome.tabCapture requires —
 * the stream id cannot be obtained without one, which is why recording can only
 * begin from a click here.
 *
 * The minutes themselves open in a full tab rather than in here. A 320px popup
 * that closes when it loses focus is the wrong place to read a set of minutes and
 * a hopeless place to check a citation against the transcript.
 */

import { useCallback, useEffect, useState } from 'react';
import { listMeetings } from '@/lib/api';
import type { ExtensionMessage, Meeting, RecordingState } from '@/lib/types';

export function App(): JSX.Element {
  const [state, setState] = useState<RecordingState>({ isRecording: false });
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [micGranted, setMicGranted] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setMeetings(await listMeetings(10));
    } catch {
      // The backend not running is a normal state for a browser extension, not
      // an error worth shouting about. Recording still works and queues locally.
      setMeetings([]);
    }
  }, []);

  useEffect(() => {
    void chrome.runtime
      .sendMessage({ type: 'GET_RECORDING_STATE' } satisfies ExtensionMessage)
      .then((s: RecordingState) => setState(s));
    void refresh();

    // Without this grant the offscreen recorder's getUserMedia fails and the mic
    // track is silently empty — you get a transcript of everyone except yourself,
    // which is a maddening thing to debug after the meeting is over. So we check
    // up front and ask before recording rather than failing during it.
    void navigator.permissions
      .query({ name: 'microphone' as PermissionName })
      .then((status) => setMicGranted(status.state === 'granted'))
      .catch(() => setMicGranted(true)); // can't tell — don't nag
  }, [refresh]);

  async function toggle(): Promise<void> {
    setError(null);
    try {
      if (state.isRecording) {
        await chrome.runtime.sendMessage({ type: 'STOP_RECORDING' } satisfies ExtensionMessage);
        setState({ isRecording: false });
        await refresh();
        return;
      }

      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab?.id) throw new Error('No active tab.');

      const res: { ok: boolean; error?: string } = await chrome.runtime.sendMessage({
        type: 'START_RECORDING',
        tabId: tab.id,
      } satisfies ExtensionMessage);

      if (!res.ok) throw new Error(res.error ?? 'Failed to start recording.');

      const updated: RecordingState = await chrome.runtime.sendMessage({
        type: 'GET_RECORDING_STATE',
      } satisfies ExtensionMessage);
      setState(updated);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  function openMeeting(meetingId: string): void {
    void chrome.tabs.create({
      url: chrome.runtime.getURL(`src/meeting/index.html?id=${meetingId}`),
    });
  }

  function openDashboard(): void {
    // No id → the meeting page renders the dashboard of every meeting.
    void chrome.tabs.create({ url: chrome.runtime.getURL('src/meeting/index.html') });
  }

  return (
    <main style={{ padding: 16 }}>
      <h1 style={{ fontSize: 16, margin: '0 0 12px' }}>Meeting Intelligence</h1>

      {!micGranted && !state.isRecording && (
        <p style={{ fontSize: 12, color: '#b45309', margin: '0 0 8px' }}>
          Your microphone is not allowed yet, so your own voice would be missing
          from the transcript.{' '}
          <button
            type="button"
            onClick={() =>
              void chrome.tabs.create({
                url: chrome.runtime.getURL('src/permission/index.html'),
              })
            }
            style={{
              padding: 0,
              border: 0,
              background: 'none',
              color: '#1a56db',
              font: 'inherit',
              cursor: 'pointer',
              textDecoration: 'underline',
            }}
          >
            Allow microphone
          </button>
        </p>
      )}

      <button type="button" onClick={() => void toggle()} style={{ width: '100%', padding: 8 }}>
        {state.isRecording ? 'Stop recording' : 'Start recording'}
      </button>

      {/* Recording someone without their knowledge is illegal in two-party-consent
          jurisdictions. The notice is a feature, not a disclaimer to be tucked away. */}
      {state.isRecording && (
        <p style={{ fontSize: 12, color: '#d93025' }}>
          Recording. Everyone in the meeting should know.
        </p>
      )}

      {error && <p style={{ fontSize: 12, color: '#d93025' }}>{error}</p>}

      <button
        type="button"
        onClick={openDashboard}
        style={{ width: '100%', padding: 8, marginTop: 8 }}
      >
        Manage all meetings
      </button>

      {meetings.length > 0 && (
        <>
          <h2 style={{ fontSize: 12, color: '#6b7280', margin: '16px 0 6px' }}>
            Recent meetings
          </h2>
          <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
            {meetings.map((m) => (
              <li key={m.id} style={{ marginBottom: 6 }}>
                <button
                  type="button"
                  onClick={() => openMeeting(m.id)}
                  style={{
                    width: '100%',
                    padding: 0,
                    border: 0,
                    background: 'none',
                    textAlign: 'left',
                    color: '#1a56db',
                    font: 'inherit',
                    fontSize: 13,
                    cursor: 'pointer',
                    textDecoration: 'underline',
                  }}
                >
                  {m.title ?? 'Untitled meeting'}
                </button>
                <div style={{ fontSize: 11, color: '#6b7280' }}>{describe(m)}</div>
              </li>
            ))}
          </ul>
        </>
      )}
    </main>
  );
}

/** What is happening to this meeting, in a few words. */
function describe(meeting: Meeting): string {
  const running = meeting.jobs.find((j) => j.status === 'running' || j.status === 'pending');
  if (running) return `${running.type}…`;

  const failed = meeting.jobs.find((j) => j.status === 'failed');
  if (failed) return `${failed.type} failed`;

  const done = meeting.jobs.some((j) => j.type === 'ground' && j.status === 'succeeded');
  return done ? 'Minutes ready' : 'Transcribed';
}
