/** Grounded chat for a single meeting, with the evidence kept visible. */

import { FormEvent, useState } from 'react';
import { askMeetingQuestion } from '@/lib/api';
import type { AnswerCitation } from '@/lib/api';

interface ChatTurn {
  question: string;
  answer?: string;
  citations?: AnswerCitation[];
  error?: string;
}

interface Props {
  meetingId: string;
  indexed: boolean;
}

export function MeetingChat({ meetingId, indexed }: Props): JSX.Element {
  const [question, setQuestion] = useState('');
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [asking, setAsking] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const query = question.trim();
    if (!query || asking || !indexed) return;

    setQuestion('');
    setAsking(true);
    setTurns((previous) => [...previous, { question: query }]);
    try {
      const response = await askMeetingQuestion(meetingId, query);
      setTurns((previous) => [
        ...previous.slice(0, -1),
        { question: query, answer: response.answer, citations: response.citations },
      ]);
    } catch (err: unknown) {
      setTurns((previous) => [
        ...previous.slice(0, -1),
        { question: query, error: err instanceof Error ? err.message : String(err) },
      ]);
    } finally {
      setAsking(false);
    }
  }

  return (
    <section className="meeting-chat" aria-labelledby="meeting-chat-title">
      <h2 id="meeting-chat-title">Ask this meeting</h2>
      <p className="muted">
        Answers come only from this meeting&apos;s indexed transcript and include the lines
        they rely on.
      </p>

      {turns.map((turn, index) => (
        <div className="chat-turn" key={`${turn.question}-${index}`}>
          <p className="chat-question">{turn.question}</p>
          {turn.answer && <p className="chat-answer">{turn.answer}</p>}
          {turn.error && <p className="error">{turn.error}</p>}
          {turn.citations && <Citations citations={turn.citations} />}
          {!turn.answer && !turn.error && <p className="muted">Finding evidence…</p>}
        </div>
      ))}

      {!indexed && (
        <p className="muted">
          Chat will be ready once transcript indexing finishes.
        </p>
      )}
      <form className="chat-form" onSubmit={(event) => void submit(event)}>
        <input
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="What was decided? Who owns the next step?"
          aria-label="Ask a question about this meeting"
          disabled={!indexed || asking}
        />
        <button type="submit" disabled={!indexed || asking || !question.trim()}>
          {asking ? 'Asking…' : 'Ask'}
        </button>
      </form>
    </section>
  );
}

function Citations({ citations }: { citations: AnswerCitation[] }): JSX.Element {
  if (citations.length === 0) return <p className="muted">No supporting lines found.</p>;
  return (
    <details className="chat-citations">
      <summary>{citations.length} supporting passage{citations.length === 1 ? '' : 's'}</summary>
      {citations.map((citation, index) => (
        <blockquote key={`${citation.startMs}-${index}`}>
          <p>
            <a href="#transcript">{formatTime(citation.startMs)}</a>
            {citation.speaker ? ` · ${citation.speaker}` : ''}
          </p>
          <p>{citation.text}</p>
        </blockquote>
      ))}
    </details>
  );
}

function formatTime(milliseconds: number): string {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1_000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}
