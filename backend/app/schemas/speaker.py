"""DTOs for mapping diarization clusters onto real people.

pyannote answers "how many voices, and when did each speak". It cannot answer
"whose voice", and no amount of audio will let it — the name is simply not in the
signal. The roster is, though: the content script read the participant list out
of the meeting UI at the start of the call. So the missing step is a join, and the
only thing that can perform it is a human who was in the meeting.

This module is the contract for that step.
"""

import uuid

from pydantic import BaseModel, model_validator

from app.schemas.meeting import SpeakerOut


class SpeakerClusterOut(BaseModel):
    """An unnamed voice, described well enough for a human to recognise it."""

    id: uuid.UUID
    #: The diarizer's label — "SPEAKER_00". Meaningless except as a handle.
    display_name: str
    segment_count: int
    #: Total time this voice was speaking. The cheapest signal for telling the
    #: chair from someone who said "yep" twice, and it lets the UI put the
    #: clusters that matter most in front of the user first.
    total_ms: int
    #: A few things this voice actually said. This is the entire basis on which
    #: the mapping can be done: nobody can identify "SPEAKER_01" from its label,
    #: but "so I'll take the ChromaDB migration" identifies a person instantly to
    #: anyone who was there. Without these the UI would be asking users to guess.
    samples: list[str] = []


class SpeakerMappingOut(BaseModel):
    """Everything the mapping UI needs, in one request."""

    #: Voices still waiting to be identified, longest-talking first.
    clusters: list[SpeakerClusterOut] = []
    #: Who was in the meeting, per the roster — the names a cluster can be mapped
    #: onto. Excludes the local user: their audio came from the mic track and was
    #: never diarized, so they cannot be one of these clusters.
    candidates: list[SpeakerOut] = []
    #: True when the last cluster has just been named and the minutes job has been
    #: queued as a result. Lets the UI say what happened rather than leaving the
    #: user wondering whether their last click did anything.
    minutes_queued: bool = False


class SpeakerResolution(BaseModel):
    """Who a cluster turned out to be. Exactly one of the three must be given.

    The three cases are genuinely different operations, not one field with a
    default:

    ``target_speaker_id`` — the usual case. This voice is someone on the roster.
    Merges the cluster into that participant.

    ``display_name`` — the roster missed them. Phone dial-ins and people who
    joined after the snapshot are real and common, and a mapping UI that cannot
    express them would strand the meeting behind a gate with no valid answer.

    ``ignore`` — not a person at all: a shared video, hold music, a speakerphone
    carrying another room. Needed for the same reason as free text; without it a
    user would have to invent a name for a YouTube clip to get their minutes.
    """

    target_speaker_id: uuid.UUID | None = None
    display_name: str | None = None
    ignore: bool = False

    @model_validator(mode="after")
    def exactly_one(self) -> "SpeakerResolution":
        """Reject ambiguous resolutions rather than picking one and hoping.

        Each of these writes a different thing to the database and two of them are
        irreversible-ish (a merge deletes the cluster row). Silently preferring one
        field over another would make a client bug look like a user's mistake.
        """
        chosen = [
            self.target_speaker_id is not None,
            bool(self.display_name and self.display_name.strip()),
            self.ignore,
        ]
        if sum(chosen) != 1:
            raise ValueError(
                "Provide exactly one of: target_speaker_id (this voice is a known "
                "participant), display_name (name them), or ignore (not a person)."
            )
        return self
