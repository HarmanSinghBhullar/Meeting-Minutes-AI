/**
 * The meeting page: minutes, and the evidence behind them.
 *
 * The interface exists to make one promise good — that every claim in the minutes
 * is one click from the transcript line that supports it. So the citation is not
 * a footnote tucked away in a detail view; it is the primary affordance on every
 * item. Minutes you cannot check are minutes you eventually stop trusting, and a
 * product whose whole pitch is accuracy cannot ask to be taken on faith.
 *
 * The second decision is that **rejected items are shown, not hidden**. The
 * grounding pass throws out claims its citations do not support, and it would be
 * easy to quietly drop them and present a tidier page. That would be the wrong
 * trade: a reader who can see what the verifier caught has a reason to believe
 * what it let through. Hiding the rejects buys a cleaner screen at the cost of
 * the only thing that makes the clean screen worth anything.
 */

import { useCallback, useEffect, useState } from 'react';
import { getMeeting, getMinutes, getTranscript } from '@/lib/api';
import type {
  Meeting as MeetingModel,
  Minutes as MinutesModel,
  MinutesItem,
  MinutesItemType,
  Speaker,
  TranscriptSegment,
} from '@/lib/types';

/** How often we re-check while the pipeline is still running. */
const POLL_INTERVAL_MS = 3_000;

const SECTIONS: { type: MinutesItemType; title: string }[] = [
  // Action items first: they are the part somebody has to do something about.
  { type: 'action_item', title: 'Action items' },
  { type: 'decision', title: 'Decisions' },
  { type: 'open_question', title: 'Open questions' },
  { type: 'risk', title: 'Risks' },
  { type: 'topic', title: 'Topics' },
];

interface Props {
  meetingId: string;
}

export function Meeting({ meetingId }: Props): JSX.Element {
  const [meeting, setMeeting] = useState<MeetingModel | null>(null);
  const [minutes, setMinutes] = useState<MinutesModel | null>(null);
  const [transcript, setTranscript] = useState<TranscriptSegment[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [m, mins, segs] = await Promise.all([
        getMeeting(meetingId),
        getMinutes(meetingId),
        getTranscript(meetingId),
      ]);
      setMeeting(m);
      setMinutes(mins);
      setTranscript(segs);
      setError(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [meetingId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Transcription takes minutes, so the page is routinely opened before there is
  // anything to show. Poll until the work is done, then stop — a page that keeps
  // hammering a finished meeting is just a background CPU leak.
  const pending = meeting?.jobs.some(
    (j) => j.status === 'pending' || j.status === 'running',
  );

  useEffect(() => {
    if (!pending) return;
    const timer = window.setInterval(() => void load(), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [pending, load]);

  if (error) return <p className="error">{error}</p>;
  if (!meeting) return <p className="muted">Loading…</p>;

  const speakers = new Map(meeting.speakers.map((s) => [s.id, s]));
  const segments = new Map(transcript.map((s) => [s.id, s]));
  const failed = meeting.jobs.find((j) => j.status === 'failed');

  return (
    <main>
      <p className="back">
        <a href="index.html">← All meetings</a>
      </p>
      <header>
        <h1>{meeting.title ?? 'Untitled meeting'}</h1>
        <p className="muted">
          {meeting.startedAt ? new Date(meeting.startedAt).toLocaleString() : 'Date unknown'}
        </p>
      </header>

      {meeting.speakers.length > 0 && <Attendance speakers={meeting.speakers} />}

      {/* The error text is on the job row for a reason: "ffmpeg is not installed"
          is something the user can act on, and "failed" is not. */}
      {failed && (
        <p className="error">
          {failed.type} failed: {failed.error ?? 'unknown error'}
        </p>
      )}

      {pending && !failed && (
        <p className="muted">
          Processing {meeting.jobs.find((j) => j.status === 'running')?.type ?? ''}… this
          page will update itself.
        </p>
      )}

      {minutes ? (
        <MinutesView minutes={minutes} speakers={speakers} segments={segments} />
      ) : (
        !pending && <p className="muted">No minutes yet.</p>
      )}

      {transcript.length > 0 && (
        <Transcript segments={transcript} speakers={speakers} />
      )}
    </main>
  );
}

function MinutesView({
  minutes,
  speakers,
  segments,
}: {
  minutes: MinutesModel;
  speakers: Map<string, Speaker>;
  segments: Map<string, TranscriptSegment>;
}): JSX.Element {
  const rejected = minutes.items.filter((i) => i.isGrounded === false).length;

  return (
    <section>
      {minutes.summary && (
        <div>
          <h2>Minutes</h2>
          <ul className="minutes-list">
            {summaryPoints(minutes.summary).map((point, i) => (
              <li key={i}>{point}</li>
            ))}
          </ul>
        </div>
      )}

      {rejected > 0 && (
        // Surfaced deliberately. The number is evidence the verifier is awake,
        // and a reader who knows what was caught has grounds to trust the rest.
        <p className="muted">
          {rejected} item{rejected === 1 ? '' : 's'} could not be supported by the
          transcript and {rejected === 1 ? 'is' : 'are'} flagged below.
        </p>
      )}

      {SECTIONS.map(({ type, title }) => {
        const items = minutes.items.filter((i) => i.type === type);
        if (items.length === 0) return null;

        return (
          <div key={type}>
            <h2>{title}</h2>
            <ul className="items">
              {items.map((item) => (
                <Item
                  key={item.id}
                  item={item}
                  speakers={speakers}
                  segments={segments}
                />
              ))}
            </ul>
          </div>
        );
      })}
    </section>
  );
}

function Item({
  item,
  speakers,
  segments,
}: {
  item: MinutesItem;
  speakers: Map<string, Speaker>;
  segments: Map<string, TranscriptSegment>;
}): JSX.Element {
  const [showEvidence, setShowEvidence] = useState(false);

  const owner = item.ownerSpeakerId ? speakers.get(item.ownerSpeakerId) : undefined;
  const cited = item.segmentIds
    .map((id) => segments.get(id))
    .filter((s): s is TranscriptSegment => s !== undefined);

  const ungrounded = item.isGrounded === false;

  return (
    <li className={ungrounded ? 'item ungrounded' : 'item'}>
      <p className="item-text">{item.text}</p>

      <p className="meta">
        {owner && <span className="owner">{owner.displayName}</span>}
        {item.dueDate && <span className="due">due {item.dueDate}</span>}

        <button type="button" className="link" onClick={() => setShowEvidence((v) => !v)}>
          {showEvidence ? 'Hide' : 'Show'} the {cited.length} line
          {cited.length === 1 ? '' : 's'} this came from
        </button>
      </p>

      {ungrounded && (
        <p className="warning">
          Not supported by its own citation
          {item.groundingNote ? `: ${item.groundingNote}` : '.'}
        </p>
      )}

      {showEvidence && (
        <blockquote>
          {cited.length === 0 ? (
            <p className="muted">The cited lines are no longer in the transcript.</p>
          ) : (
            cited.map((seg) => (
              <p key={seg.id}>
                <span className="ts">{formatTime(seg.startMs)}</span>{' '}
                <span className="speaker">
                  {speakerName(seg, speakers)}:
                </span>{' '}
                {seg.textEn ?? seg.text}
              </p>
            ))
          )}
        </blockquote>
      )}
    </li>
  );
}

function Transcript({
  segments,
  speakers,
}: {
  segments: TranscriptSegment[];
  speakers: Map<string, Speaker>;
}): JSX.Element {
  return (
    <section>
      <h2>Transcript</h2>
      <div className="transcript">
        {segments.map((seg) => (
          <p key={seg.id}>
            <span className="ts">{formatTime(seg.startMs)}</span>{' '}
            <span
              className={seg.speakerSource === 'unknown' ? 'speaker unknown' : 'speaker'}
              // A guessed name and a known one must not look the same. The user
              // is the only one who can correct an attribution, and they can only
              // do that if they can see which ones are uncertain.
              title={`Speaker source: ${seg.speakerSource}`}
            >
              {speakerName(seg, speakers)}:
            </span>{' '}
            {seg.textEn ?? seg.text}
          </p>
        ))}
      </div>
    </section>
  );
}

function speakerName(seg: TranscriptSegment, speakers: Map<string, Speaker>): string {
  if (!seg.speakerId) return 'Unknown speaker';
  const speaker = speakers.get(seg.speakerId);
  return speaker ? speakerLabel(speaker) : 'Unknown speaker';
}

/**
 * A speaker's name as shown to the reader, tagging the local user with "(You)".
 *
 * The one exception is the backend's "You" placeholder — the name it falls back
 * to when the meeting UI never told it who the local user was. Marking that as
 * "You (You)" would be nonsense, so the tag is added only when there is a real
 * name to attach it to.
 */
function speakerLabel(speaker: Speaker): string {
  const isPlaceholder = speaker.displayName.trim().toLowerCase() === 'you';
  return speaker.isLocalUser && !isPlaceholder
    ? `${speaker.displayName} (You)`
    : speaker.displayName;
}

/** Who was in the meeting, the local user first, each tagged as needed. */
function Attendance({ speakers }: { speakers: Speaker[] }): JSX.Element {
  const ordered = [...speakers].sort(
    (a, b) => Number(b.isLocalUser) - Number(a.isLocalUser),
  );

  return (
    <section>
      <h2>Attendance</h2>
      <ul className="attendance">
        {ordered.map((s) => (
          <li key={s.id}>{speakerLabel(s)}</li>
        ))}
      </ul>
    </section>
  );
}

/**
 * Split the stored minutes into display points.
 *
 * The backend writes one point per line; older meetings may hold a single prose
 * paragraph, which simply renders as one point. Any leading bullet glyph the
 * model slipped in is trimmed so it does not double up with the list marker.
 */
function summaryPoints(summary: string): string[] {
  return summary
    .split('\n')
    .map((line) => line.replace(/^\s*[-•*]\s*/, '').trim())
    .filter(Boolean);
}

function formatTime(ms: number): string {
  const total = Math.floor(ms / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}
