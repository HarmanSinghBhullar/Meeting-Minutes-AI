import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { Dashboard } from './Dashboard';
import { Meeting } from './Meeting';
import './styles.css';

// One page, two views. The meeting id travels in the query string — this opens in
// a tab, and a tab that can be reloaded, bookmarked, or shared has to carry its
// own state. With an id we show that meeting; without one, the dashboard of all of
// them. Navigating between the two is a plain link, so the back button just works.
const meetingId = new URLSearchParams(window.location.search).get('id');

const container = document.getElementById('root');
if (!container) throw new Error('Root element is missing.');

createRoot(container).render(
  <StrictMode>
    {meetingId ? <Meeting meetingId={meetingId} /> : <Dashboard />}
  </StrictMode>,
);
