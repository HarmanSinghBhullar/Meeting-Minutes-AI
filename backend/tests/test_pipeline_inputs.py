"""What the transcribe stage feeds Whisper and pyannote.

Both inputs under test here are read from the ``speakers`` table *before* the same
job clears the previous run's rows out of it, so both are really tests about one
thing: a meeting being processed for the second time must be handed the same
inputs as a meeting being processed for the first.

The rows that break that are the ones whose ``display_name`` is a cluster label —
``SPEAKER_00`` — rather than a person. There are two kinds, and the durable one is
easy to miss: a cluster a human *ignored* is moved off DIARIZATION to MANUAL
without ever being renamed, and nothing deletes it, so it is still sitting there
called ``SPEAKER_02`` on every subsequent run.
"""

from sqlalchemy.orm import Session

from app.db.models.meeting import Meeting
from app.workers.pipeline import _remote_speaker_bound, build_vocabulary_prompt

from conftest import SpeakerFactory


def test_vocabulary_prompt_carries_the_meeting_and_its_people(
    db: Session, meeting: Meeting, speakers: SpeakerFactory
) -> None:
    """The baseline: title and real names are what the prompt is for."""
    speakers.roster("Priya Nair")
    speakers.roster("Harman", is_local_user=True)

    prompt = build_vocabulary_prompt(db, meeting)

    assert prompt is not None
    assert "Weekly sync" in prompt
    assert "Priya Nair" in prompt
    # The local user's name is worth priming too — other people say it.
    assert "Harman" in prompt


def test_vocabulary_prompt_excludes_cluster_labels(
    db: Session, meeting: Meeting, speakers: SpeakerFactory
) -> None:
    """Whisper must never be primed with "SPEAKER_00".

    This is not a weaker prompt, it is an actively harmful one: the prompt biases
    the decoder toward tokens it expects to hear, and nobody says "speaker zero
    zero" out loud. Both kinds of cluster row are here — the unnamed one left over
    mid-run, and the ignored one that survives forever.
    """
    speakers.roster("Priya Nair")
    speakers.cluster("SPEAKER_00")
    speakers.ignored_cluster("SPEAKER_02")

    prompt = build_vocabulary_prompt(db, meeting)

    assert prompt is not None
    assert "Priya Nair" in prompt
    assert "SPEAKER" not in prompt


def test_speaker_bound_counts_only_real_remote_people(
    db: Session, meeting: Meeting, speakers: SpeakerFactory
) -> None:
    """Two remote participants, plus one spare seat for a late joiner."""
    speakers.roster("Priya Nair")
    speakers.roster("Deepak")
    speakers.roster("Harman", is_local_user=True)  # mic track; never diarized

    assert _remote_speaker_bound(db, meeting) == 3


def test_speaker_bound_is_unchanged_by_a_previous_runs_clusters(
    db: Session, meeting: Meeting, speakers: SpeakerFactory
) -> None:
    """Reprocessing must pass pyannote the bound the first run passed.

    Both leftovers are non-people: the unnamed cluster is about to be deleted by
    this very job, and the ignored one is hold music. Counting either would loosen
    the one parameter that governs over-splitting a little further on every
    re-run — and over-splitting surfaces as the same person appearing twice in the
    mapping UI, which is the error users actually notice.
    """
    speakers.roster("Priya Nair")
    speakers.roster("Deepak")
    speakers.cluster("SPEAKER_00")
    speakers.ignored_cluster("SPEAKER_02")

    assert _remote_speaker_bound(db, meeting) == 3


def test_speaker_bound_is_none_when_the_roster_is_empty(
    db: Session, meeting: Meeting, speakers: SpeakerFactory
) -> None:
    """Knowing nothing is not the same as knowing zero.

    An empty roster leaves pyannote unconstrained rather than bounded at 1, which
    would force every voice on the call into a single cluster.
    """
    speakers.ignored_cluster("SPEAKER_02")

    assert _remote_speaker_bound(db, meeting) is None
