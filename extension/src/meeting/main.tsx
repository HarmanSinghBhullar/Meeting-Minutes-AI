import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { Meeting } from './Meeting';
import './styles.css';

// The meeting id travels in the query string: this page is opened in a tab, and
// a tab that can be reloaded, bookmarked, or shared has to carry its own state.
const meetingId = new URLSearchParams(window.location.search).get('id');

const container = document.getElementById('root');
if (!container) throw new Error('Root element is missing.');

createRoot(container).render(
  <StrictMode>
    {meetingId ? (
      <Meeting meetingId={meetingId} />
    ) : (
      <p className="error">No meeting id in the URL.</p>
    )}
  </StrictMode>,
);
