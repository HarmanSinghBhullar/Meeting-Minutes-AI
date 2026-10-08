"""Rebuild the derived Chroma index from every transcript in PostgreSQL.

Run from ``backend`` after installing the RAG extra:

    venv\\Scripts\\python scripts\\reindex_rag.py
"""

import logging

from app.db.session import SessionLocal
from app.services.rag.indexer import reindex_all


def main() -> None:
    """Replace all RAG windows with the current PostgreSQL transcript data."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with SessionLocal() as db:
        count = reindex_all(db)
    logging.info("Rebuilt %d RAG transcript window(s).", count)


if __name__ == "__main__":
    main()
