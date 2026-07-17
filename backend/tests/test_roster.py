"""The roster endpoint: reconciling a participant list that changes mid-meeting.

``open_meeting`` reads the roster once, inside the call stack of the click that
starts recording. That read is wrong in two ordinary cases — the tile grid may not
have rendered yet, and anybody who joins later is simply absent — so the adapter
re-posts what it sees and this endpoint merges it.

The whole risk in re-posting is that a repeated call is not a repeated *effect*.
These tests pin that down: the endpoint adds people, and does nothing else. In
particular it must never overwrite what a human decided at the mapping panel,
because the roster arrives on a timer and a human's answer does not.
"""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.enums import SpeakerSource
from app.db.models.meeting import Meeting
from app.db.models.speaker import Speaker

from conftest import SpeakerFactory


def _post(client: TestClient, meeting_id: uuid.UUID, participants: list[dict]) -> dict:
    response = client.post(
        f"/api/v1/recordings/meetings/{meeting_id}/participants",
        json={"participants": participants},
    )
    assert response.status_code == 202, response.text
    return response.json()


def _names(db: Session, meeting_id: uuid.UUID) -> set[str]:
    return {
        s.display_name
        for s in db.execute(
            select(Speaker).where(Speaker.meeting_id == meeting_id)
        ).scalars()
    }


def test_late_joiner_is_created(
    client: TestClient, db: Session, meeting: Meeting, speakers: SpeakerFactory
) -> None:
    """The case this endpoint exists for: someone arrives after recording began."""
    speakers.roster("Harman Singh Bhullar", is_local_user=True)

    body = _post(
        client,
        meeting.id,
        [
            {"display_name": "Harman Singh Bhullar", "is_local_user": True},
            {"display_name": "Suyash Pandey", "is_local_user": False},
        ],
    )

    assert body == {"created": 1}
    assert _names(db, meeting.id) == {"Harman Singh Bhullar", "Suyash Pandey"}

    joiner = db.execute(
        select(Speaker).where(Speaker.display_name == "Suyash Pandey")
    ).scalar_one()
    # DOM, not UNKNOWN: arriving late makes it no less the platform's own name,
    # and only a DOM row is offered as a candidate for their own voice.
    assert joiner.source == SpeakerSource.DOM
    assert joiner.is_local_user is False


def test_reposting_the_same_roster_creates_nobody(
    client: TestClient, db: Session, meeting: Meeting
) -> None:
    """It is posted on a timer, so 'again' is the common case, not the rare one."""
    roster = [{"display_name": "Priya Nair", "external_ref": "p-1", "is_local_user": False}]

    assert _post(client, meeting.id, roster) == {"created": 1}
    assert _post(client, meeting.id, roster) == {"created": 0}
    assert _post(client, meeting.id, roster) == {"created": 0}

    rows = db.execute(select(Speaker).where(Speaker.meeting_id == meeting.id)).scalars()
    assert len(list(rows)) == 1


def test_a_participant_who_renames_is_not_duplicated(
    client: TestClient, db: Session, meeting: Meeting
) -> None:
    """The platform id is the identity; the label on the tile is not.

    Meet lets someone change their display name mid-call. Keyed by name we would
    invent a second person and split their attendance in half.
    """
    _post(client, meeting.id, [{"display_name": "Priya", "external_ref": "p-1"}])
    body = _post(client, meeting.id, [{"display_name": "Priya Nair", "external_ref": "p-1"}])

    assert body == {"created": 0}
    # Still the name we first saw: this endpoint adds people, it does not rename
    # them. A rename would race the mapping panel for no benefit.
    assert _names(db, meeting.id) == {"Priya"}


def test_external_ref_is_backfilled_onto_a_name_only_row(
    client: TestClient, db: Session, meeting: Meeting, speakers: SpeakerFactory
) -> None:
    """A speaker event can create a row from a name alone, before the roster catches up.

    Backfilling the id is what keeps them one row across a reconnect, when the
    name is otherwise all we have to match on.
    """
    existing = speakers.roster("Priya Nair")
    assert existing.external_ref is None

    body = _post(
        client, meeting.id, [{"display_name": "Priya Nair", "external_ref": "p-9"}]
    )

    assert body == {"created": 0}
    db.refresh(existing)
    assert existing.external_ref == "p-9"


def test_a_named_cluster_is_not_duplicated_by_a_later_roster_post(
    client: TestClient, db: Session, meeting: Meeting, speakers: SpeakerFactory
) -> None:
    """A human named this voice. The roster must not answer back.

    Naming a cluster in place leaves a MANUAL row called "Priya Nair". If a roster
    post then created a *second* "Priya Nair", the meeting would show her twice —
    exactly what `_merge_into` deletes the cluster row to avoid.
    """
    cluster = speakers.cluster("SPEAKER_01")
    cluster.display_name = "Priya Nair"
    cluster.source = SpeakerSource.MANUAL
    db.flush()

    body = _post(client, meeting.id, [{"display_name": "Priya Nair"}])

    assert body == {"created": 0}
    db.refresh(cluster)
    # Untouched: still the human's row, still their verdict.
    assert cluster.source == SpeakerSource.MANUAL


def test_an_excluded_cluster_is_left_excluded(
    client: TestClient, db: Session, meeting: Meeting, speakers: SpeakerFactory
) -> None:
    """"Ignore" is a decision. A timer must not undo it.

    An ignored cluster keeps its `SPEAKER_02` name forever, so a roster post can
    never match it by name — but if one ever did, resurrecting it as a participant
    would put hold music back in the minutes.
    """
    ignored = speakers.ignored_cluster("SPEAKER_02")

    _post(client, meeting.id, [{"display_name": "SPEAKER_02"}])

    db.refresh(ignored)
    assert ignored.is_excluded is True
    assert ignored.source == SpeakerSource.MANUAL


def test_unknown_meeting_is_404(client: TestClient) -> None:
    """A roster for a meeting that does not exist is a bug, not a row to create."""
    response = client.post(
        f"/api/v1/recordings/meetings/{uuid.uuid4()}/participants",
        json={"participants": [{"display_name": "Nobody"}]},
    )
    assert response.status_code == 404
