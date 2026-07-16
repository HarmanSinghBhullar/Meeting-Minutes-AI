"""Test fixtures: a real Postgres, on a throwaway database.

Not SQLite. The models are Postgres-specific by design — UUID primary keys, JSONB
word timings — and the speaker-mapping code leans on ``ON DELETE SET NULL`` and
multi-row ``UPDATE``s to do the right thing. A SQLite stand-in would either fail
to load the models or, worse, pass while the real database did something else.
A test that agrees with a fiction is not worth its runtime.

The database is created once per session and dropped at the end. Each test runs
against a clean set of tables, so no test can be made to pass or fail by another
one's leftovers.
"""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.base import Base
from app.db.models.enums import Platform, SpeakerSource, Track
from app.db.models.meeting import Meeting
from app.db.models.recording import Recording
from app.db.models.segment import Segment
from app.db.models.speaker import Speaker
from app.db.session import get_db
from app.main import app

#: A separate database, so a bad test can never touch real recordings.
TEST_DB_NAME = "meeting_minutes_test"


@pytest.fixture(scope="session")
def engine() -> Iterator[object]:
    """A Postgres engine pointed at a freshly created test database."""
    url = make_url(settings.database_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")

    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    except Exception as exc:  # pragma: no cover - environment problem, not a bug
        pytest.skip(f"Postgres is not available for tests: {exc}")

    test_engine = create_engine(url.set(database=TEST_DB_NAME))

    # create_all rather than `alembic upgrade head`: this asserts the tests run
    # against the models as written. The migrations have their own job — getting an
    # existing database to this shape — and conflating the two would let a missing
    # migration hide behind a passing test suite.
    Base.metadata.create_all(test_engine)

    yield test_engine

    test_engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def db(engine: object) -> Iterator[Session]:
    """A session over empty tables."""
    with sessionmaker(bind=engine)() as session:  # type: ignore[arg-type]
        # Truncate rather than recreate: same isolation, a fraction of the cost.
        session.execute(
            text(
                "TRUNCATE meetings, speakers, speaker_events, segments, "
                "recordings, minutes, minutes_items, jobs RESTART IDENTITY CASCADE"
            )
        )
        session.commit()
        yield session


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    """An API client sharing the test's session, so it sees uncommitted setup."""
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def meeting(db: Session) -> Meeting:
    """A finalized meeting with a tab recording, but no transcript yet."""
    row = Meeting(
        id=uuid.uuid4(),
        title="Weekly sync",
        platform=Platform.MEET,
    )
    db.add(row)
    db.add(
        Recording(
            meeting_id=row.id,
            track=Track.TAB,
            chunk_count=3,
            is_finalized=True,
            source_path="/tmp/tab.webm",
            normalized_path="/tmp/tab.wav",
        )
    )
    db.flush()
    return row


class SpeakerFactory:
    """Builds the speaker rows a mapping test needs, without the ceremony."""

    def __init__(self, db: Session, meeting: Meeting, recording: Recording) -> None:
        self._db = db
        self._meeting = meeting
        self._recording = recording
        self._next_start = 0

    def roster(self, name: str, *, is_local_user: bool = False) -> Speaker:
        """A participant read from the meeting UI's list — a mapping candidate."""
        return self._add(name, SpeakerSource.DOM, is_local_user=is_local_user)

    def cluster(self, label: str = "SPEAKER_00") -> Speaker:
        """An unnamed voice from the diarizer — what the gate is waiting on."""
        return self._add(label, SpeakerSource.DIARIZATION)

    def _add(
        self, name: str, source: SpeakerSource, *, is_local_user: bool = False
    ) -> Speaker:
        speaker = Speaker(
            meeting_id=self._meeting.id,
            display_name=name,
            source=source,
            is_local_user=is_local_user,
        )
        self._db.add(speaker)
        self._db.flush()
        return speaker

    def says(self, speaker: Speaker, text_: str, *, duration_ms: int = 4_000) -> Segment:
        """Attach a spoken line to a speaker, on a non-overlapping timeline."""
        start = self._next_start
        self._next_start += duration_ms

        segment = Segment(
            meeting_id=self._meeting.id,
            recording_id=self._recording.id,
            speaker_id=speaker.id,
            index=start // 1_000,
            start_ms=start,
            end_ms=start + duration_ms,
            text=text_,
            language="en",
            words=[],
            speaker_source=speaker.source,
        )
        self._db.add(segment)
        self._db.flush()
        return segment


@pytest.fixture
def speakers(db: Session, meeting: Meeting) -> SpeakerFactory:
    """Factory for the speaker/segment shapes these tests care about."""
    recording = db.execute(
        select(Recording).where(Recording.meeting_id == meeting.id)
    ).scalar_one()
    return SpeakerFactory(db, meeting, recording)
