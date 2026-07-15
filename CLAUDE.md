# Project

Meeting Intelligence Browser Extension

## Features

- Record meeting audio
- Generate transcript
- Store transcript in PostgreSQL
- Translate transcript to English
- Speaker identification
- Meeting summaries
- RAG Q&A over previous meetings

## Tech Stack

Frontend:
- React
- TypeScript
- Vite

Backend:
- FastAPI
- PostgreSQL
- SQLAlchemy

AI:
- Whisper
- pyannote.audio
- ChromaDB

## Rules

- Use TypeScript strict mode
- Use FastAPI
- Modular architecture
- Add comments and docstrings
- Follow production-grade folder structure

## Current status

Keep this section current as features land — update it in the same change that
changes the behaviour, so it never drifts from the code.

Working end to end: audio → ffmpeg → Whisper `large-v3` → attribution
(DOM speaker timeline for Meet/Zoom/Teams, pyannote fallback otherwise) →
word-level alignment → attributed segments in Postgres → cited minutes
(`services/minutes/`, OpenAI + Anthropic providers) → grounding pass. The
extension records, uploads to the API, and returns minutes on a live meeting.

Not yet built:

- Translation — `workers/pipeline.run_translate` raises `NotImplementedError`.
- RAG — `services/rag/` and the `INDEX` job are stubbed; `api/v1/routes/qa.py`
  returns `501`. It is deliberately last, since Q&A inherits every upstream error.

## Running locally

- API: `backend/venv/Scripts/python -m uvicorn app.main:app --reload`
- Worker (separate process): `backend/venv/Scripts/python -m app.workers.run_worker`
- Extension: `cd extension && npm run build`, then load `extension/dist` unpacked
  at `chrome://extensions`.
- Requires PostgreSQL, `ffmpeg` on `PATH`, and an NVIDIA GPU. See `README.md`.