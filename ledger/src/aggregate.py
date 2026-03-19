from __future__ import annotations

import uuid
from typing import Any
from src.event_store import EventStore, NewEvent, RecordedEvent


class Aggregate:
    """
    Base class for all aggregates.

    Load-Validate-Append pattern
    ----------------------------
    1. Load   — replay all recorded events from the store to rebuild state.
    2. Validate — command methods check invariants against current state and
                  raise if violated; otherwise they stage NewEvent(s).
    3. Append — save() flushes staged events to the store atomically with
                the expected_version observed at load time.

    Subclasses implement:
      - AGGREGATE_TYPE: str class attribute
      - _apply(event): mutate self._state for each event type
    """

    AGGREGATE_TYPE: str = "Aggregate"

    def __init__(self, stream_id: uuid.UUID) -> None:
        self.stream_id = stream_id
        self.version: int = 0
        self._pending: list[NewEvent] = []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _stage(self, event_type: str, payload: dict[str, Any]) -> None:
        """Stage an event to be written on the next save()."""
        self._pending.append(NewEvent(event_type=event_type, payload=payload))

    def _apply(self, event: RecordedEvent) -> None:  # pragma: no cover
        """Subclasses override to mutate state from a recorded event."""

    def _rebuild(self, events: list[RecordedEvent]) -> None:
        """Replay all recorded events to reconstruct current state."""
        for event in events:
            self._apply(event)
            self.version = event.stream_position

    # ------------------------------------------------------------------
    # Load-Validate-Append entry points
    # ------------------------------------------------------------------

    @classmethod
    async def load(cls, stream_id: uuid.UUID, store: EventStore) -> "Aggregate":
        """Reconstruct aggregate state by replaying its event stream."""
        instance = cls(stream_id)
        events = await store.load_stream(stream_id)
        instance._rebuild(events)
        return instance

    async def save(self, store: EventStore) -> None:
        """Flush all staged events to the store, then clear the pending list."""
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
