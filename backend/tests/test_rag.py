"""RAG tests use fakes so they never download an embedding model or call an LLM."""

import uuid
from types import SimpleNamespace

from app.services.rag import indexer, retriever
from app.services.rag.schemas import QuestionAnswer


class FakeCollection:
    """Minimal vector-store fake that records indexing and serves one query."""

    def __init__(self) -> None:
        self.deleted: list[dict[str, object]] = []
        self.added: dict[str, object] | None = None
        self.query_args: dict[str, object] | None = None

    def delete(self, **kwargs: object) -> None:
        self.deleted.append(kwargs)

    def add(self, **kwargs: object) -> None:
        self.added = kwargs

    def query(self, **kwargs: object) -> dict[str, list[list[object]]]:
        self.query_args = kwargs
        return {
            "documents": [["[1200ms] Priya: We chose PostgreSQL."]],
            "metadatas": [[
                {
                    "meeting_id": str(uuid.UUID(int=1)),
                    "meeting_title": "Architecture",
                    "speakers": "Priya",
                    "start_ms": 1200,
                }
            ]],
        }


def test_index_meeting_replaces_existing_windows(monkeypatch: object) -> None:
    """A re-index deletes stale rows and uses English text for retrieval."""
    collection = FakeCollection()
    meeting = SimpleNamespace(id=uuid.UUID(int=1), title="Architecture", started_at=None)
    segments = [
        SimpleNamespace(
            id=uuid.uuid4(),
            start_ms=index * 1000,
            index=index,
            text="hola",
            text_en="hello",
            speaker=SimpleNamespace(display_name="Priya"),
        )
        for index in range(13)
    ]

    class Result:
        def scalars(self) -> list[object]:
            return segments

    class Db:
        def get(self, _model: object, _id: object) -> object:
            return meeting

        def execute(self, _statement: object) -> Result:
            return Result()

    monkeypatch.setattr(indexer, "get_collection", lambda: collection)
    assert indexer.index_meeting(Db(), meeting.id) == 2  # type: ignore[arg-type]
    assert collection.deleted == [{"where": {"meeting_id": str(meeting.id)}}]
    assert collection.added is not None
    assert collection.added["documents"][0].endswith("Priya: hello")  # type: ignore[index]


def test_answer_returns_only_model_selected_citations(monkeypatch: object) -> None:
    """The model's evidence numbers, not arbitrary retrieved rows, make citations."""
    collection = FakeCollection()

    class Provider:
        def complete(self, **_kwargs: object) -> QuestionAnswer:
            return QuestionAnswer(answer="PostgreSQL was chosen.", citations=[1, 99])

    monkeypatch.setattr(retriever, "get_collection", lambda: collection)
    monkeypatch.setattr(retriever, "get_provider", lambda: Provider())
    answer = retriever.answer_question("What did we choose?")
    assert answer.text == "PostgreSQL was chosen."
    assert [(citation.meeting_title, citation.start_ms) for citation in answer.citations] == [
        ("Architecture", 1200)
    ]


def test_answer_can_be_limited_to_one_meeting(monkeypatch: object) -> None:
    """The meeting screen must not retrieve a similarly named past discussion."""
    collection = FakeCollection()

    class Provider:
        def complete(self, **_kwargs: object) -> QuestionAnswer:
            return QuestionAnswer(answer="PostgreSQL was chosen.", citations=[1])

    meeting_id = uuid.UUID(int=1)
    monkeypatch.setattr(retriever, "get_collection", lambda: collection)
    monkeypatch.setattr(retriever, "get_provider", lambda: Provider())
    retriever.answer_question("What did we choose?", meeting_id=meeting_id)
    assert collection.query_args is not None
    assert collection.query_args["where"] == {"meeting_id": str(meeting_id)}


def test_answer_with_no_citations_fails_closed(monkeypatch: object) -> None:
    """A model answer is withheld unless it identifies supporting evidence."""
    collection = FakeCollection()

    class Provider:
        def complete(self, **_kwargs: object) -> QuestionAnswer:
            return QuestionAnswer(answer="An unsupported claim", citations=[])

    monkeypatch.setattr(retriever, "get_collection", lambda: collection)
    monkeypatch.setattr(retriever, "get_provider", lambda: Provider())
    answer = retriever.answer_question("What happened?")
    assert answer.citations == []
    assert answer.text == "I could not answer that from the retrieved meeting evidence."
