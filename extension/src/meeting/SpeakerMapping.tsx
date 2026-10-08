/**
 * Naming the voices, so the minutes can name the people.
 *
 * This panel exists because of a trade made upstream. Attribution used to come
 * from scraping the meeting UI's active-speaker highlight, which was free and
 * already knew everyone's name — right up until the platform reskinned and it
 * silently attributed the entire call to "Unknown". Diarization replaced it: it
 * reads the audio, so nothing in a CSS bundle can break it, but it can only
 * separate voices, never name them. This screen is where that cost is paid.
 *
 * Two things follow from that, and they drive the whole design:
 *
 * **It has to be answerable.** "Who is SPEAKER_01?" is not a question anyone can
 * answer from a label, so every cluster leads with what that voice actually said.
 * A line like "I'll take the ChromaDB migration" identifies a colleague instantly.
 * That is why the samples are the body of each row and the dropdown is the small
 * part: the reading is the work, the click is the formality.
 *
 * **It blocks the minutes, so it must not feel like a wall.** The pipeline stops
 * here deliberately — minutes crediting SPEAKER_01 are not a rough draft, they
 * are a confident wrong answer. But a gate the user resents is a gate they click
 * through carelessly, which defeats it. So: longest-talking voices first (most
 * value, easiest to recognise), each answer saves on its own, and the last one
 * starts the minutes with no separate confirm step to hunt for.
 */

import { useState } from 'react';
import { resolveSpeaker } from '@/lib/api';
import type { SpeakerCluster, SpeakerMapping as Mapping } from '@/lib/types';

/** Sentinel values for the two options that are not a roster participant. */
const NAME_OTHER = '__other__';
const NOT_A_SPEAKER = '__ignore__';

interface Props {
  meetingId: string;
  mapping: Mapping;
  /** Called with the state the server returned, so the parent can drop the panel
   *  and start polling once the last cluster is resolved. */
  onResolved: (next: Mapping) => void;
}

export function SpeakerMapping({ meetingId, mapping, onResolved }: Props): JSX.Element {
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  async function resolve(
    cluster: SpeakerCluster,
    resolution: Parameters<typeof resolveSpeaker>[2],
  ): Promise<void> {
    setBusyId(cluster.id);
    setError(null);
    try {
      onResolved(await resolveSpeaker(meetingId, cluster.id, resolution));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  }

  const remaining = mapping.clusters.length;

  return (
    <section className="mapping">
      <h2>Who was speaking?</h2>
      <p className="muted">
        {remaining === 1
          ? 'One voice is still unidentified.'
          : `${remaining} voices are still unidentified.`}{' '}
        The minutes are on hold until they are named — a summary that credits
        “SPEAKER_01” is worse than one that waits.
      </p>

      {error && <p className="error">{error}</p>}

      <ul className="cluster-list">
        {mapping.clusters.map((cluster) => (
          <ClusterRow
            key={cluster.id}
            cluster={cluster}
            candidates={mapping.candidates}
            busy={busyId === cluster.id}
            onResolve={(resolution) => void resolve(cluster, resolution)}
          />
        ))}
      </ul>
    </section>
  );
}

function ClusterRow({
  cluster,
  candidates,
  busy,
  onResolve,
}: {
  cluster: SpeakerCluster;
  candidates: Mapping['candidates'];
  busy: boolean;
  onResolve: (resolution: Parameters<typeof resolveSpeaker>[2]) => void;
}): JSX.Element {
  const [naming, setNaming] = useState(false);
  const [draft, setDraft] = useState('');

  function handleSelect(value: string): void {
    if (value === '') return;
    if (value === NAME_OTHER) {
      setNaming(true);
      return;
    }
    if (value === NOT_A_SPEAKER) {
      onResolve({ ignore: true });
      return;
    }
    onResolve({ targetSpeakerId: value });
  }

  function saveName(): void {
    const displayName = draft.trim();
    if (!displayName) return;
    onResolve({ displayName });
  }

  return (
    <li className="cluster">
      <div className="cluster-head">
        <span className="cluster-label">{cluster.displayName}</span>
        <span className="cluster-stats">
          {formatDuration(cluster.totalMs)} across {cluster.segmentCount} line
          {cluster.segmentCount === 1 ? '' : 's'}
        </span>
      </div>

      {/* The samples come before the control on purpose: this is the evidence the
          decision is made from, and burying it under a dropdown would turn an
          answerable question back into a guess. */}
      {cluster.samples.length > 0 ? (
        <blockquote className="cluster-samples">
          {cluster.samples.map((sample, i) => (
            <p key={i}>“{sample}”</p>
          ))}
        </blockquote>
      ) : (
        <p className="muted">
          This voice was separated from the audio but no words were transcribed for
          it — most likely background noise or a stray sound.
        </p>
      )}

      {naming ? (
        <form
          className="cluster-name-form"
          onSubmit={(e) => {
            e.preventDefault();
            saveName();
          }}
        >
          <input
            className="rename-input"
            value={draft}
            autoFocus
            placeholder="Their name"
            disabled={busy}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') setNaming(false);
            }}
            aria-label={`Name for ${cluster.displayName}`}
          />
          <button type="submit" className="link" disabled={busy || !draft.trim()}>
            Save
          </button>
          <button type="button" className="link" onClick={() => setNaming(false)}>
            Cancel
          </button>
        </form>
      ) : (
        <label className="cluster-pick">
          <span className="muted">This is</span>{' '}
          <select
            defaultValue=""
            disabled={busy}
            onChange={(e) => handleSelect(e.target.value)}
            aria-label={`Identify ${cluster.displayName}`}
          >
            <option value="" disabled>
              Select…
            </option>
            {candidates.map((c) => (
              <option key={c.id} value={c.id}>
                {c.displayName}
              </option>
            ))}
            {/* Present even when the roster looks complete. People dial in by
                phone and join after the participant list was read, and a gate with
                no valid answer is a trap. */}
            <option value={NAME_OTHER}>Someone else — type a name…</option>
            <option value={NOT_A_SPEAKER}>Not a person (shared video, noise)</option>
          </select>
          {busy && <span className="muted"> saving…</span>}
        </label>
      )}
    </li>
  );
}

/** Speaking time, at the resolution a human cares about: minutes, or seconds if
 *  it barely spoke at all. */
function formatDuration(ms: number): string {
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.round(seconds / 60)} min`;
}
