from __future__ import annotations

import uuid
from typing import Any

from src.event_store import EventStore, NewEvent, RecordedEvent


class DomainError(Exception):
    """Single domain exception raised for all invariant violations."""


class Aggregate:
    """
    Base class implementing the Load-Validate-Append pattern.

    1. Load   — classmethod load() replays the event stream to rebuild state.
    2. Validate — command methods assert invariants; raise DomainError if violated.
    3. Append — save() writes staged events atomically at the observed version.
    """

    AGGREGATE_TYPE: str = "Aggregate"

    def __init__(self, stream_id: uuid.UUID) -> None:
        self.stream_id = stream_id
        self.version: int = 0
        self._pending: list[NewEvent] = []

    def _stage(self, event_type: str, payload: dict[str, Any]) -> None:
        self._pending.append(NewEvent(event_type=event_type, payload=payload))

    def _apply(self, event: RecordedEvent) -> None:  # pragma: no cover
        """Subclasses override to fold a recorded event into state."""

    def _rebuild(self, events: list[RecordedEvent]) -> None:
        for event in events:
            self._apply(event)
            self.version = event.stream_position

    @classmethod
    async def load(cls, stream_id: uuid.UUID, store: EventStore) -> "Aggregate":
        instance = cls(stream_id)
        instance._rebuild(await store.load_stream(stream_id))
        return instance

    async def save(self, store: EventStore) -> None:
        if not self._pending:
            return
        await store.append(
            self.stream_id,
            self.AGGREGATE_TYPE,
            self._pending,
            expected_version=self.version,
        )
        self.version += len(self._pending)
        self._pending.clear()
