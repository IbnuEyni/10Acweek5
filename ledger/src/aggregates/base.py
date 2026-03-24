from __future__ import annotations

from typing import Any

from src.event_store import EventStore, NewEvent, RecordedEvent
from src.models.events import DomainError  # noqa: F401 — re-exported for aggregates


class Aggregate:
    """
    Base class implementing the Load → Validate → Append pattern.

    stream_id is a TEXT string (e.g. "loan-abc123") matching the schema.
    version tracks the last stream_position seen; passed as expected_version on save().
    """

    AGGREGATE_TYPE: str = "Aggregate"

    def __init__(self, stream_id: str) -> None:
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
    async def load(cls, stream_id: str, store: EventStore) -> "Aggregate":
        instance = cls(stream_id)
        instance._rebuild(await store.load_stream(stream_id))
        return instance

    async def save(
        self,
        store: EventStore,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> None:
        if not self._pending:
            return
        await store.append(
            stream_id=self.stream_id,
            events=self._pending,
            expected_version=self.version,
            aggregate_type=self.AGGREGATE_TYPE,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        # Apply pending events locally so _apply is the single source of truth.
        # Command methods only stage events; all state mutation lives in _apply.
        # global_position=0 is a sentinel — not meaningful for in-memory state.
        from src.event_store import RecordedEvent as _RecordedEvent
        from datetime import datetime as _dt, timezone as _tz
        _now = _dt.now(_tz.utc)
        for i, new_event in enumerate(self._pending):
            recorded = _RecordedEvent(
                event_id=new_event.event_id,
                stream_id=self.stream_id,
                stream_position=self.version + i + 1,
                global_position=0,
                event_type=new_event.event_type,
                event_version=new_event.event_version,
                payload=new_event.payload,
                metadata=new_event.metadata,
                recorded_at=_now,
            )
            self._apply(recorded)
        self.version += len(self._pending)
        self._pending.clear()
