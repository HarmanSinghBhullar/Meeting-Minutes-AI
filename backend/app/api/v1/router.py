"""Aggregates the v1 routes."""

from fastapi import APIRouter

from app.api.v1.routes import meetings, minutes, qa, recordings, speakers

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(recordings.router)
api_router.include_router(meetings.router)
api_router.include_router(speakers.router)
api_router.include_router(minutes.router)
api_router.include_router(qa.router)
