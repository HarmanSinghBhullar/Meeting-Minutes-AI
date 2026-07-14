"""FastAPI application entry point.

    uvicorn app.main:app --reload

The worker runs separately (``python -m app.workers.run_worker``) so that a
transcription job pinning the GPU for several minutes cannot starve the API the
extension is still uploading chunks to.
"""

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Prepare local storage on startup."""
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Storage directory: %s", settings.storage_dir.resolve())
    yield


app = FastAPI(
    title="Meeting Intelligence API",
    version="0.1.0",
    lifespan=lifespan,
)

# The extension calls this API from a chrome-extension:// origin, which is not
# same-origin with localhost and so must be allowed explicitly. The id is assigned
# by Chrome per-install, hence the regex — see Settings.cors_origin_regex.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_origin_regex=settings.cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}
