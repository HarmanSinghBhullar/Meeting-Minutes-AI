"""End-to-end smoke test of the *production* path.

``smoke_transcribe.py`` calls the pipeline directly, which is convenient for
debugging but bypasses everything the extension actually relies on. This script
drives the real thing over HTTP:

    open meeting -> upload chunks -> finalize -> job queued -> worker claims it
    -> transcribes -> poll status -> fetch transcript

Nothing here imports the pipeline. If it passes, the API, the job queue, and the
worker are genuinely wired together.

Requires the API and the worker to both be running:

    python -m uvicorn app.main:app
    python -m app.workers.run_worker

Usage:

    python scripts/smoke_queue.py <mic.wav> <tab.wav>
"""

import sys
import time
import uuid
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000/api/v1"
POLL_TIMEOUT_SECONDS = 600


def main(mic: Path, tab: Path) -> int:
    meeting_id = str(uuid.uuid4())

    with httpx.Client(timeout=60) as client:
        print(f"Meeting {meeting_id}")

        # 1. Open the meeting, registering the participants the content script
        #    would have read off the meeting UI. These names prime Whisper.
        r = client.post(
            f"{BASE}/recordings/meetings",
            json={
                "id": meeting_id,
                "title": "Weekly sync",
                "platform": "meet",
                "agenda": "ChromaDB migration, Q3 roadmap",
                "speakers": [
                    {"display_name": "Harman", "is_local_user": True},
                    {"display_name": "Priya", "is_local_user": False},
                ],
            },
        )
        r.raise_for_status()
        print("  opened; tracks:", [rec["track"] for rec in r.json()["recordings"]])

        # 2. Upload the audio as chunks, exactly as the offscreen recorder does.
        for track, path in (("mic", mic), ("tab", tab)):
            r = client.post(
                f"{BASE}/recordings/meetings/{meeting_id}/chunks",
                data={"track": track},
                files={"chunk": (path.name, path.read_bytes(), "audio/webm")},
            )
            r.raise_for_status()
            print(f"  uploaded {track}: {r.json()}")

        # 3. Finalize. This must return immediately — it enqueues a job rather
        #    than transcribing, which is the whole point of the design.
        started = time.monotonic()
        r = client.post(
            f"{BASE}/recordings/meetings/{meeting_id}/finalize",
            json={"ended_at": "2026-07-14T12:00:00Z"},
        )
        r.raise_for_status()
        elapsed = time.monotonic() - started
        jobs = r.json()["jobs"]
        print(f"  finalized in {elapsed:.3f}s (must be fast); jobs: {jobs}")

        # 4. Poll, as the extension's progress bar would.
        print("\nWaiting for the worker to pick it up...")
        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        last = None

        while time.monotonic() < deadline:
            meeting = client.get(f"{BASE}/meetings/{meeting_id}").raise_for_status().json()
            transcribe = next(j for j in meeting["jobs"] if j["type"] == "transcribe")

            if transcribe["status"] != last:
                print(f"  job status: {transcribe['status']}")
                last = transcribe["status"]

            if transcribe["status"] == "succeeded":
                break
            if transcribe["status"] == "failed":
                print(f"\nFAILED: {transcribe['error']}")
                return 1

            time.sleep(1)
        else:
            print("\nTimed out. Is the worker running?")
            return 1

        # 5. Read the transcript the worker wrote.
        segments = client.get(f"{BASE}/meetings/{meeting_id}/transcript").raise_for_status().json()
        speakers = {s["id"]: s["display_name"] for s in meeting["speakers"]}

        print(f"\n--- transcript ({len(segments)} segments) ---")
        for seg in segments:
            name = speakers.get(seg["speaker_id"], "UNKNOWN")
            print(f"[{seg['start_ms'] / 1000:6.2f}s] {name:>8} ({seg['speaker_source']}): {seg['text']}")

        return 0 if segments else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python scripts/smoke_queue.py <mic.wav> <tab.wav>")
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
