/**
 * The meetings dashboard: everything you have recorded, in one place.
 *
 * This is the extension's home when it is opened without a specific meeting id.
 * It exists because a recorder that only ever shows you the *last* meeting is a
 * recorder you lose things in — the value compounds only if the back catalogue is
 * navigable, nameable, and prunable. So every row here is a small management
 * surface: open it, rename it (the auto-title is a meeting code nobody remembers),
 * or delete it (test runs and mis-starts pile up fast, as anyone who has used this
 * for a day knows).
 *
 * It talks to the same backend the meeting page does. When the backend is down —
 * a normal state for a browser extension whose server is a local dev process — it
 * says so plainly rather than showing an empty page that looks like "no meetings".
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  deleteMeeting,
  listMeetings,
  regenerateMinutes,
  renameMeeting,
} from '@/lib/api';
import type { Meeting } from '@/lib/types';

/** How often to re-check while any meeting is still being processed. */
const POLL_INTERVAL_MS = 4_000;

export function Dashboard(): JSX.Element {
  const [meetings, setMeetings] = useState<Meeting[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setMeetings(await listMeetings(200));
      setError(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // A meeting still in the pipeline changes state on its own, so poll until the
  // whole list is settled, then stop.
  const anyPending = useMemo(
    () =>
      meetings?.some((m) =>
        m.jobs.some((j) => j.status === 'pending' || j.status === 'running'),
      ) ?? false,
    [meetings],
  );

  useEffect(() => {
    if (!anyPending) return;
    const timer = window.setInterval(() => void load(), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [anyPending, load]);

  // Optimistic local updates: the row reflects the change immediately, and a
  // failure re-syncs from the server rather than leaving a lie on screen.
  const handleRename = useCallback(
    async (id: string, title: string) => {
      setMeetings((prev) =>
        prev?.map((m) => (m.id === id ? { ...m, title } : m)) ?? prev,
      );
      try {
        await renameMeeting(id, title);
      } catch {
        await load();
      }
    },
    [load],
  );

  const handleDelete = useCallback(
    async (id: string) => {
      const previous = meetings;
      setMeetings((prev) => prev?.filter((m) => m.id !== id) ?? prev);
      try {
        await deleteMeeting(id);
      } catch {
        setMeetings(previous); // put it back — the delete did not take
      }
    },
    [meetings],
  );

  const handleRegenerate = useCallback(
    async (id: string) => {
      try {
        await regenerateMinutes(id);
      } finally {
        // Refetch either way: on success the new job shows as pending (which turns
        // the poll back on and the badge to "minutes…"); on failure we resync.
        await load();
      }
    },
    [load],
  );

  return (
    <main>
      <header>
        <h1>Meetings</h1>
        <p className="muted">Everything you have recorded. Open, rename, or delete.</p>
      </header>

      {error && (
        <p className="error">
          Could not reach the backend ({error}). Start it and this list will fill in.
        </p>
      )}

      {meetings === null && !error && <p className="muted">Loading…</p>}

      {meetings !== null && meetings.length === 0 && !error && (
        <p className="muted">
          No meetings yet. Open a call, click the extension, and press Start recording.
        </p>
      )}

      {meetings && meetings.length > 0 && (
        <ul className="meeting-list">
          {meetings.map((m) => (
            <MeetingRow
              key={m.id}
              meeting={m}
              onRename={handleRename}
              onDelete={handleDelete}
              onRegenerate={handleRegenerate}
            />
          ))}
        </ul>
      )}
    </main>
  );
}

function MeetingRow({
  meeting,
  onRename,
  onDelete,
  onRegenerate,
}: {
  meeting: Meeting;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  onRegenerate: (id: string) => void;
}): JSX.Element {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(meeting.title ?? '');

  // Minutes are written from the transcript, so regenerating only makes sense
  // once there is one — and not while the pipeline is mid-run on this meeting.
  const transcribed = meeting.jobs.some(
    (j) => j.type === 'transcribe' && j.status === 'succeeded',
  );
  const busy = meeting.jobs.some(
    (j) => j.status === 'pending' || j.status === 'running',
  );
  const canRegenerate = transcribed && !busy;

  function saveRename(): void {
    const title = draft.trim();
    if (title && title !== meeting.title) onRename(meeting.id, title);
    setEditing(false);
  }

  function startRename(): void {
    setDraft(meeting.title ?? '');
    setEditing(true);
  }

  function confirmDelete(): void {
    const name = meeting.title ?? 'this meeting';
    // Deletion is permanent and takes the audio with it — worth one click to be
    // sure, since the whole reason this screen exists is to stop things vanishing.
    if (window.confirm(`Delete “${name}” and its transcript permanently?`)) {
      onDelete(meeting.id);
    }
  }

  return (
    <li className="meeting-row">
      {editing ? (
        <form
          className="rename-form"
          onSubmit={(e) => {
            e.preventDefault();
            saveRename();
          }}
        >
          <input
            className="rename-input"
            value={draft}
            autoFocus
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') setEditing(false);
            }}
            aria-label="Meeting name"
          />
          <button type="submit" className="link">
            Save
          </button>
          <button type="button" className="link" onClick={() => setEditing(false)}>
            Cancel
          </button>
        </form>
      ) : (
        <div className="meeting-main">
          {/* A plain link, not a chrome.tabs call: this page reloads itself with
              the id and renders the meeting. Bookmarkable and back-button-friendly. */}
          <a className="meeting-title" href={`?id=${meeting.id}`}>
            {meeting.title ?? 'Untitled meeting'}
          </a>
          <p className="meeting-meta">
            <span>{formatDate(meeting.startedAt)}</span>
            {meeting.speakers.length > 0 && (
              <span>{meeting.speakers.map((s) => s.displayName).join(', ')}</span>
            )}
            <StatusBadge meeting={meeting} />
          </p>
        </div>
      )}

      {!editing && (
        <div className="meeting-actions">
          {canRegenerate && (
            <button
              type="button"
              className="link"
              onClick={() => onRegenerate(meeting.id)}
              title="Re-run minutes generation over the transcript"
            >
              Regenerate
            </button>
          )}
          <button type="button" className="link" onClick={startRename}>
            Rename
          </button>
          <button type="button" className="link danger" onClick={confirmDelete}>
            Delete
          </button>
        </div>
      )}
    </li>
  );
}

/** A one-word verdict on where a meeting is in the pipeline. */
function StatusBadge({ meeting }: { meeting: Meeting }): JSX.Element {
  const running = meeting.jobs.find(
    (j) => j.status === 'running' || j.status === 'pending',
  );
  if (running) return <span className="badge working">{running.type}…</span>;

  const failed = meeting.jobs.find((j) => j.status === 'failed');
  if (failed) return <span className="badge failed">{failed.type} failed</span>;

  const ready = meeting.jobs.some((j) => j.type === 'ground' && j.status === 'succeeded');
  if (ready) return <span className="badge ready">Minutes ready</span>;

  const transcribed = meeting.jobs.some(
    (j) => j.type === 'transcribe' && j.status === 'succeeded',
  );
  return <span className="badge">{transcribed ? 'Transcribed' : 'No transcript'}</span>;
}

function formatDate(iso: string | null): string {
  if (!iso) return 'Date unknown';
  return new Date(iso).toLocaleString([], {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
}
