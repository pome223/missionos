"""ADK memory service backed by curated promoted memories."""

from __future__ import annotations

from google.adk.memory.base_memory_service import (
    BaseMemoryService,
    MemoryEntry,
    SearchMemoryResponse,
)
from google.adk.sessions.session import Session
from google.genai import types

from src.memory_lifecycle.memory_schema import PromotedMemory
from src.memory_lifecycle.promoted_store import (
    PromotedMemoryStore,
    get_promoted_store,
)


class PromotedMemoryService(BaseMemoryService):
    """Expose promoted memories through the ADK memory service API."""

    def __init__(self, store: PromotedMemoryStore) -> None:
        self._store = store

    async def add_session_to_memory(self, session: Session):
        """Session ingestion is intentionally opt-in via promoted memories only."""
        return None

    async def store_promoted_memories(
        self,
        *,
        app_name: str,
        memories: list[PromotedMemory],
    ) -> list[str]:
        self._store.bulk_save(memories, app_name=app_name)
        return [memory.memory_id for memory in memories]

    async def search_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        query: str,
    ) -> SearchMemoryResponse:
        memories = self._store.search(
            app_name=app_name,
            user_id=user_id,
            query=query,
        )
        response = SearchMemoryResponse()
        for memory in memories:
            response.memories.append(_to_memory_entry(memory))
        return response


def _to_memory_entry(memory: PromotedMemory) -> MemoryEntry:
    return MemoryEntry(
        content=types.Content(
            role="model",
            parts=[types.Part(text=memory.content)],
        ),
        author=f"promoted:{memory.memory_type.value}",
        timestamp=memory.provenance.captured_at.isoformat(),
    )


_promoted_memory_service: PromotedMemoryService | None = None


def get_promoted_memory_service() -> PromotedMemoryService:
    global _promoted_memory_service
    if _promoted_memory_service is None:
        _promoted_memory_service = PromotedMemoryService(get_promoted_store())
    return _promoted_memory_service
