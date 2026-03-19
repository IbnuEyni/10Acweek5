from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import asyncpg


@dataclass
class NewEvent:
    event_type: str
    payload: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    event_id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass
class RecordedEvent:
    id: uuid.UUID
    stream_id: uuid.UUID
    stream_position: int
    global_position: int
    event_type: str
    payload: dict[str, Any]
    metadata: dict[str, Any]


class OptimisticConcurrencyError(Exception):
    """Raised when the expected stream version does not match the current version."""


class EventStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def append(
        self,
        stream_id: uuid.UUID,
        aggregate_type: str,
        events: list[NewEvent],
        expected_version: int,
    ) -> None:
        """
        Append events to a stream and write outbox rows — atomically.

        expected_version: the current_version the caller observed.
            Pass 0 when creating a new stream.
        Raises OptimisticConcurrencyError on version mismatch.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Upsert the stream row and lock it for the duration of the tx.
                row = await conn.fetchrow(
                    """
                    INSERT INTO event_streams (stream_id, aggregate_type, current_version)
                    VALUES ($1, $2, 0)
                    ON CONFLICT (stream_id) DO UPDATE
                        SET updated_at = now()
                    RETURNING current_version
                    """,
                    stream_id,
                    aggregate_type,
                )
                current_version: int = row["current_version"]

                if current_version != expected_version:
                    raise OptimisticConcurrencyError(
                        f"Expected version {expected_version}, got {current_version} "
                        f"for stream {stream_id}"
                    )

                for i, event in enumerate(events):
                    next_position = current_version + i + 1

                    event_row = await conn.fetchrow(
                        """
                        INSERT INTO events
                            (id, stream_id, stream_position, event_type, payload, metadata)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        RETURNING id
                        """,
                        event.event_id,
                        stream_id,
                        next_position,
                        event.event_type,
                        json.dumps(event.payload),
                        json.dumps(event.metadata),
                    )

                    await conn.execute(
                        """
                        INSERT INTO outbox (stream_id, event_id, event_type, payload)
                        VALUES ($1, $2, $3, $4)
                        """,
                        stream_id,
                        event_row["id"],
                        event.event_type,
                        json.dumps(event.payload),
                    )

                new_version = current_version + len(events)
                await conn.execute(
                    """
                    UPDATE event_streams
                    SET current_version = $1, updated_at = now()
                    WHERE stream_id = $2
                    """,
                    new_version,
                    stream_id,
                )

    async def load_stream(self, stream_id: uuid.UUID) -> list[RecordedEvent]:
        """Return all events for a stream ordered by stream_position."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, stream_id, stream_position, global_position,
                       event_type, payload, metadata
                FROM events
                WHERE stream_id = $1
                ORDER BY stream_position
                """,
                stream_id,
            )
        return [
            RecordedEvent(
                id=row["id"],
                stream_id=row["stream_id"],
                stream_position=row["stream_position"],
                global_position=row["global_position"],
                event_type=row["event_type"],
                payload=row["payload"],
                metadata=row["metadata"],
            )
            for row in rows
        ]
