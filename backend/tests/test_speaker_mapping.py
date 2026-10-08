"""Tests for the speaker-mapping gate.

The gate makes one promise: **no minutes are written while a voice is unnamed.**
Everything here either checks that promise directly or checks something that would
quietly break it — a merge that leaves the roster with two Priyas, an ignore that
does not actually withhold the video's words, a resolution that clears the gate
without queuing the work it was blocking.

These are the tests that would have caught the bug in the design this replaced:
attribution that failed silently and produced confident, wrong minutes.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.enums import JobStatus, JobType, SpeakerSource
from app.db.models.job import Job
from app.db.models.meeting import Meeting
from app.db.models.segment import Segment
from app.db.models.speaker import Speaker
from app.schemas.speaker import SpeakerResolution
from app.services.attribution.mapping import unmapped_cluster_count
from app.workers.pipeline import _minutable

# `from conftest import`, not `from tests.conftest import`. The latter cannot
# work: pyannote.pipeline ships its own test suite as a top-level `tests` package
# (site-packages/tests/__init__.py), and a real package always wins over this
# directory's implicit namespace package — so `tests.conftest` resolves into
# pyannote's tests and raises ModuleNotFoundError. pytest puts this directory on
# sys.path itself, which makes the bare name both correct and immune to whatever
# else decides to squat on `tests`.
from conftest import SpeakerFactory


class TestUnmappedClusterCount:
    """The gate itself: one query that everything else keys off."""

    def test_counts_only_diarization_clusters(
        self, db: Session, meeting: Meeting, speakers: SpeakerFactory
    ) -> None:
        speakers.roster("Priya Nair")
        speakers.roster("You", is_local_user=True)
        speakers.cluster("SPEAKER_00")
        speakers.cluster("SPEAKER_01")

        assert unmapped_cluster_count(db, meeting.id) == 2

    def test_zero_when_nothing_was_diarized(
        self, db: Session, meeting: Meeting, speakers: SpeakerFactory
    ) -> None:
        speakers.roster("Priya Nair")
        assert unmapped_cluster_count(db, meeting.id) == 0

    def test_is_scoped_to_one_meeting(
        self, db: Session, meeting: Meeting, speakers: SpeakerFactory
    ) -> None:
        """A neighbouring meeting's unnamed voices must not gate this one."""
        other = Meeting(id=uuid.uuid4(), title="Someone else's call")
        db.add(other)
        db.flush()
        db.add(
            Speaker(
                meeting_id=other.id,
                display_name="SPEAKER_00",
                source=SpeakerSource.DIARIZATION,
            )
        )
        db.flush()

        assert unmapped_cluster_count(db, meeting.id) == 0
        assert unmapped_cluster_count(db, other.id) == 1


class TestResolutionSchema:
    """Exactly one answer per cluster — the rest are client bugs."""

    def test_accepts_each_of_the_three_forms(self) -> None:
        assert SpeakerResolution(target_speaker_id=uuid.uuid4()).ignore is False
        assert SpeakerResolution(display_name="Priya Nair").display_name == "Priya Nair"
        assert SpeakerResolution(ignore=True).ignore is True

    def test_rejects_an_empty_resolution(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            SpeakerResolution()

    def test_rejects_two_answers_at_once(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            SpeakerResolution(target_speaker_id=uuid.uuid4(), ignore=True)

    def test_whitespace_is_not_a_name(self) -> None:
        """'   ' would otherwise pass as an answer and name someone nothing."""
        with pytest.raises(ValueError, match="exactly one"):
            SpeakerResolution(display_name="   ")


class TestGetSpeakerMapping:
    """The panel's one request."""

    def test_returns_clusters_with_samples_and_candidates(
        self, client: TestClient, meeting: Meeting, speakers: SpeakerFactory
    ) -> None:
        speakers.roster("Priya Nair")
        speakers.roster("Sam Okafor")
        cluster = speakers.cluster("SPEAKER_00")
        speakers.says(cluster, "I'll take the ChromaDB migration.")

        res = client.get(f"/api/v1/meetings/{meeting.id}/speaker-mapping")
        assert res.status_code == 200
        body = res.json()

        assert [c["display_name"] for c in body["clusters"]] == ["SPEAKER_00"]
        assert body["clusters"][0]["samples"] == ["I'll take the ChromaDB migration."]
        assert body["clusters"][0]["segment_count"] == 1
        assert {c["display_name"] for c in body["candidates"]} == {
            "Priya Nair",
            "Sam Okafor",
        }

    def test_local_user_is_not_a_candidate(
        self, client: TestClient, meeting: Meeting, speakers: SpeakerFactory
    ) -> None:
        """Their audio is the mic track and was never diarized, so no cluster can
        be them. Offering them would invite an answer that cannot be right."""
        speakers.roster("Priya Nair")
        speakers.roster("Harman Bhullar", is_local_user=True)
        speakers.cluster()

        body = client.get(f"/api/v1/meetings/{meeting.id}/speaker-mapping").json()
        assert [c["display_name"] for c in body["candidates"]] == ["Priya Nair"]

    def test_loudest_cluster_comes_first(
        self, client: TestClient, meeting: Meeting, speakers: SpeakerFactory
    ) -> None:
        quiet = speakers.cluster("SPEAKER_00")
        loud = speakers.cluster("SPEAKER_01")
        speakers.says(quiet, "Yep.", duration_ms=1_000)
        speakers.says(loud, "So the plan is to cut the release on Thursday.", duration_ms=30_000)

        body = client.get(f"/api/v1/meetings/{meeting.id}/speaker-mapping").json()
        assert [c["display_name"] for c in body["clusters"]] == ["SPEAKER_01", "SPEAKER_00"]

    def test_samples_prefer_the_longest_lines(
        self, client: TestClient, meeting: Meeting, speakers: SpeakerFactory
    ) -> None:
        """A call opens with "hi" and "can you hear me" — the least identifying
        speech anyone produces. The longest lines are the ones that name a person."""
        cluster = speakers.cluster()
        speakers.says(cluster, "Hi.", duration_ms=500)
        speakers.says(cluster, "I'll own the ChromaDB migration this sprint.", duration_ms=9_000)

        body = client.get(f"/api/v1/meetings/{meeting.id}/speaker-mapping").json()
        assert body["clusters"][0]["samples"][0].startswith("I'll own the ChromaDB")

    def test_unknown_meeting_is_404(self, client: TestClient) -> None:
        res = client.get(f"/api/v1/meetings/{uuid.uuid4()}/speaker-mapping")
        assert res.status_code == 404


class TestResolveByMerge:
    """The common case: this voice is someone on the roster."""

    def test_merges_segments_and_removes_the_cluster(
        self,
        client: TestClient,
        db: Session,
        meeting: Meeting,
        speakers: SpeakerFactory,
    ) -> None:
        priya = speakers.roster("Priya Nair")
        cluster = speakers.cluster()
        segment = speakers.says(cluster, "I'll take the migration.")

        res = client.post(
            f"/api/v1/meetings/{meeting.id}/speakers/{cluster.id}/resolve",
            json={"target_speaker_id": str(priya.id)},
        )
        assert res.status_code == 200

        db.expire_all()
        # The words are Priya's now, and marked as a human's assertion.
        assert db.get(Segment, segment.id).speaker_id == priya.id
        assert db.get(Segment, segment.id).speaker_source == SpeakerSource.MANUAL
        # The placeholder is gone — a merge that renamed it would leave the meeting
        # with two Priya Nairs, and every speaker list would show her twice.
        assert db.get(Speaker, cluster.id) is None

    def test_rejects_a_target_from_another_meeting(
        self, client: TestClient, db: Session, meeting: Meeting, speakers: SpeakerFactory
    ) -> None:
        other = Meeting(id=uuid.uuid4(), title="Another call")
        db.add(other)
        db.flush()
        stranger = Speaker(
            meeting_id=other.id, display_name="Stranger", source=SpeakerSource.DOM
        )
        db.add(stranger)
        db.flush()
        cluster = speakers.cluster()

        res = client.post(
            f"/api/v1/meetings/{meeting.id}/speakers/{cluster.id}/resolve",
            json={"target_speaker_id": str(stranger.id)},
        )
        assert res.status_code == 400

    def test_rejects_merging_a_cluster_into_another_cluster(
        self, client: TestClient, meeting: Meeting, speakers: SpeakerFactory
    ) -> None:
        """Two clusters are two voices by construction; collapsing them here would
        just produce a bigger anonymous blob."""
        a = speakers.cluster("SPEAKER_00")
        b = speakers.cluster("SPEAKER_01")

        res = client.post(
            f"/api/v1/meetings/{meeting.id}/speakers/{a.id}/resolve",
            json={"target_speaker_id": str(b.id)},
        )
        assert res.status_code == 400


class TestResolveByName:
    """The roster missed someone — a phone dial-in, a late joiner."""

    def test_names_the_cluster_in_place(
        self,
        client: TestClient,
        db: Session,
        meeting: Meeting,
        speakers: SpeakerFactory,
    ) -> None:
        cluster = speakers.cluster()
        segment = speakers.says(cluster, "Sorry, dialling in from the car.")

        res = client.post(
            f"/api/v1/meetings/{meeting.id}/speakers/{cluster.id}/resolve",
            json={"display_name": "Dev Sharma"},
        )
        assert res.status_code == 200

        db.expire_all()
        named = db.get(Speaker, cluster.id)
        assert named.display_name == "Dev Sharma"
        assert named.source == SpeakerSource.MANUAL
        assert named.is_excluded is False
        assert db.get(Segment, segment.id).speaker_source == SpeakerSource.MANUAL


class TestResolveByIgnore:
    """Not a person at all — a shared video, hold music."""

    def test_excludes_without_deleting_the_words(
        self,
        client: TestClient,
        db: Session,
        meeting: Meeting,
        speakers: SpeakerFactory,
    ) -> None:
        cluster = speakers.cluster()
        segment = speakers.says(cluster, "Subscribe and hit the bell.")

        res = client.post(
            f"/api/v1/meetings/{meeting.id}/speakers/{cluster.id}/resolve",
            json={"ignore": True},
        )
        assert res.status_code == 200

        db.expire_all()
        excluded = db.get(Speaker, cluster.id)
        assert excluded.is_excluded is True
        assert excluded.source == SpeakerSource.MANUAL
        # The words were really in the recording; the transcript must still say so.
        assert db.get(Segment, segment.id) is not None

    def test_excluded_speaker_is_not_a_candidate(
        self, client: TestClient, meeting: Meeting, speakers: SpeakerFactory
    ) -> None:
        """A shared video must not be offerable as the answer for the next voice."""
        video = speakers.cluster("SPEAKER_00")
        speakers.cluster("SPEAKER_01")
        client.post(
            f"/api/v1/meetings/{meeting.id}/speakers/{video.id}/resolve",
            json={"ignore": True},
        )

        body = client.get(f"/api/v1/meetings/{meeting.id}/speaker-mapping").json()
        assert body["candidates"] == []


class TestGateReleasesTheMinutes:
    """The point of the whole feature."""

    def test_no_minutes_job_while_a_voice_is_unnamed(
        self,
        client: TestClient,
        db: Session,
        meeting: Meeting,
        speakers: SpeakerFactory,
    ) -> None:
        priya = speakers.roster("Priya Nair")
        first = speakers.cluster("SPEAKER_00")
        speakers.cluster("SPEAKER_01")

        body = client.post(
            f"/api/v1/meetings/{meeting.id}/speakers/{first.id}/resolve",
            json={"target_speaker_id": str(priya.id)},
        ).json()

        assert body["minutes_queued"] is False
        assert _minutes_jobs(db, meeting) == []

    def test_resolving_the_last_voice_queues_the_minutes(
        self,
        client: TestClient,
        db: Session,
        meeting: Meeting,
        speakers: SpeakerFactory,
    ) -> None:
        priya = speakers.roster("Priya Nair")
        only = speakers.cluster()

        body = client.post(
            f"/api/v1/meetings/{meeting.id}/speakers/{only.id}/resolve",
            json={"target_speaker_id": str(priya.id)},
        ).json()

        assert body["minutes_queued"] is True
        assert body["clusters"] == []
        assert len(_minutes_jobs(db, meeting)) == 1

    def test_ignoring_the_last_voice_also_releases_the_gate(
        self,
        client: TestClient,
        db: Session,
        meeting: Meeting,
        speakers: SpeakerFactory,
    ) -> None:
        """A meeting whose only remaining voice was a shared video is fully
        answered — the gate must not deadlock on it."""
        only = speakers.cluster()

        body = client.post(
            f"/api/v1/meetings/{meeting.id}/speakers/{only.id}/resolve",
            json={"ignore": True},
        ).json()

        assert body["minutes_queued"] is True
        assert len(_minutes_jobs(db, meeting)) == 1

    def test_resolving_the_same_cluster_twice_is_a_conflict(
        self, client: TestClient, meeting: Meeting, speakers: SpeakerFactory
    ) -> None:
        """Two tabs on one meeting: the second must not silently overwrite the
        first, nor queue the minutes a second time."""
        priya = speakers.roster("Priya Nair")
        cluster = speakers.cluster()
        path = f"/api/v1/meetings/{meeting.id}/speakers/{cluster.id}/resolve"

        assert client.post(path, json={"target_speaker_id": str(priya.id)}).status_code == 200
        assert client.post(path, json={"display_name": "Someone else"}).status_code in (404, 409)

    def test_mapping_during_translation_does_not_double_queue_the_minutes(
        self,
        client: TestClient,
        db: Session,
        meeting: Meeting,
        speakers: SpeakerFactory,
    ) -> None:
        """A non-English meeting translates while the user names its speakers.

        Both stages want to queue the minutes and neither can see the other's
        intent, so without a check the meeting gets summarised twice — two LLM
        bills, and two differing versions handed to readers. The resolve route
        must defer: TRANSLATE re-checks the gate when it finishes.
        """
        priya = speakers.roster("Priya Nair")
        cluster = speakers.cluster()
        db.add(Job(meeting_id=meeting.id, type=JobType.TRANSLATE, status=JobStatus.RUNNING))
        db.flush()

        body = client.post(
            f"/api/v1/meetings/{meeting.id}/speakers/{cluster.id}/resolve",
            json={"target_speaker_id": str(priya.id)},
        ).json()

        assert body["clusters"] == []  # the gate really is clear
        assert body["minutes_queued"] is False  # ...but translate owns the handoff
        assert _minutes_jobs(db, meeting) == []

    def test_translate_queues_the_minutes_once_mapping_is_done(
        self, db: Session, meeting: Meeting, speakers: SpeakerFactory
    ) -> None:
        """The other half of the deferral: translate must actually pick it up."""
        from app.workers.pipeline import _enqueue_minutes_unless_unmapped

        speakers.roster("Priya Nair")  # no clusters left — mapping finished
        _enqueue_minutes_unless_unmapped(db, meeting.id)
        db.flush()

        assert len(_minutes_jobs(db, meeting)) == 1

    def test_translate_still_holds_when_voices_remain_unnamed(
        self, db: Session, meeting: Meeting, speakers: SpeakerFactory
    ) -> None:
        from app.workers.pipeline import _enqueue_minutes_unless_unmapped

        speakers.cluster()
        _enqueue_minutes_unless_unmapped(db, meeting.id)
        db.flush()

        assert _minutes_jobs(db, meeting) == []

    def test_regenerate_is_refused_while_voices_are_unnamed(
        self, client: TestClient, db: Session, meeting: Meeting, speakers: SpeakerFactory
    ) -> None:
        """The pipeline's handoff is not the only door into the minutes job."""
        speakers.cluster()

        res = client.post(f"/api/v1/meetings/{meeting.id}/minutes/regenerate")
        assert res.status_code == 409
        assert "identifying" in res.json()["detail"]
        assert _minutes_jobs(db, meeting) == []

    def test_regenerate_works_once_everyone_is_named(
        self, client: TestClient, meeting: Meeting, speakers: SpeakerFactory
    ) -> None:
        speakers.roster("Priya Nair")

        res = client.post(f"/api/v1/meetings/{meeting.id}/minutes/regenerate")
        assert res.status_code == 202


class TestMinutable:
    """What the minutes are actually allowed to read."""

    def test_withholds_excluded_speakers(
        self, db: Session, meeting: Meeting, speakers: SpeakerFactory
    ) -> None:
        priya = speakers.roster("Priya Nair")
        video = speakers.roster("Shared video")
        video.is_excluded = True
        db.flush()

        real = speakers.says(priya, "Let's ship on Thursday.")
        speakers.says(video, "Smash that subscribe button.")

        assert [s.id for s in _minutable([real, *_segments(db, video)])] == [real.id]

    def test_keeps_everything_when_nothing_is_excluded(
        self, db: Session, meeting: Meeting, speakers: SpeakerFactory
    ) -> None:
        priya = speakers.roster("Priya Nair")
        a = speakers.says(priya, "One.")
        b = speakers.says(priya, "Two.")

        assert _minutable([a, b]) == [a, b]

    def test_tolerates_segments_with_no_speaker(
        self, db: Session, meeting: Meeting, speakers: SpeakerFactory
    ) -> None:
        """An unattributed line is still something that was said."""
        priya = speakers.roster("Priya Nair")
        orphan = speakers.says(priya, "Who said this?")
        orphan.speaker_id = None
        db.flush()
        db.expire(orphan)

        assert _minutable([orphan]) == [orphan]


def _minutes_jobs(db: Session, meeting: Meeting) -> list[Job]:
    return list(
        db.execute(
            select(Job).where(Job.meeting_id == meeting.id, Job.type == JobType.MINUTES)
        ).scalars()
    )


def _segments(db: Session, speaker: Speaker) -> list[Segment]:
    return list(
        db.execute(select(Segment).where(Segment.speaker_id == speaker.id)).scalars()
    )
