from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

import asyncpg

# ---------------------------------------------------------------------------
# Upcast registry
# Registered with: EventStore.register_upcast("OldType", from_version=1, fn)
# Applied transparently on every load_stream() / load_all() call.
# ---------------------------------------------------------------------------
_UPCASTS: dict[tuple[str, int], Callable[[dict], dict]] = {}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class OptimisticConcurrencyError(Exception):
    """
    Raised when expected_version does not match the stream's current version.
    Carries enough context for the caller to log and retry intelligently.
    """
    def __init__(
        self,
        stream_id: str,
        expected: int,
        actual: int,
    ) -> None:
        self.stream_id = stream_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Stream '{stream_id}': expected version {expected}, got {actual}. "
            f"Reload the stream and retry."
        )


class StreamNotFoundError(Exception):
    """Raised by get_stream_metadata when the stream does not exist."""


# ---------------------------------------------------------------------------
# Wire types — used internally and by aggregates
# ---------------------------------------------------------------------------

@dataclass
class NewEvent:
    """
    An event staged for appending. event_id is client-supplied for idempotency:
    if the same event_id is appended twice, the second INSERT hits the PK
    constraint and the transaction rolls back — no silent duplicate.
    """
    event_type: str
    payload: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    event_id: uuid.UUID = field(default_factory=uuid.uuid4)
    event_version: int = 1


@dataclass
class RecordedEvent:
    """An event as returned from the store, with upcasting already applied."""
    event_id: uuid.UUID
    stream_id: str
    stream_position: int
    global_position: int
    event_type: str
    event_version: int
    payload: dict[str, Any]
    metadata: dict[str, Any]
    recorded_at: Any  # datetime — kept as Any to avoid import cycle with models


# ---------------------------------------------------------------------------
# StreamMetadata — returned by get_stream_metadata
# ---------------------------------------------------------------------------

@dataclass
class StreamMetadata:
    stream_id: str
    aggregate_type: str
    current_version: int
    created_at: Any
    archived_at: Any | None
    metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode(row_value: Any) -> Any:
    """asyncpg returns JSONB as dict already; handle the str case defensively."""
    if isinstance(row_value, str):
        return json.loads(row_value)
    return row_value


def _apply_upcasts(event_type: str, version: int, payload: dict) -> tuple[int, dict]:
    """Walk the upcast chain for (event_type, version) until no more upcasters exist."""
    v = version
    p = payload
    while (event_type, v) in _UPCASTS:
        p = _UPCASTS[(event_type, v)](p)
        v += 1
    return v, p


def _to_recorded(row: asyncpg.Record) -> RecordedEvent:
    payload = _decode(row["payload"])
    version = row["event_version"]
    final_version, upcasted_payload = _apply_upcasts(row["event_type"], version, payload)
    return RecordedEvent(
        event_id=row["event_id"],
        stream_id=row["stream_id"],
        stream_position=row["stream_position"],
        global_position=row["global_position"],
        event_type=row["event_type"],
        event_version=final_version,
        payload=upcasted_payload,
        metadata=_decode(row["metadata"]),
        recorded_at=row["recorded_at"],
    )


# ---------------------------------------------------------------------------
# EventStore
# ---------------------------------------------------------------------------

class EventStore:
    """
    PostgreSQL-backed event store.

    Concurrency strategy (append):
      1. INSERT event_streams ON CONFLICT DO NOTHING — ensures the stream row exists.
      2. SELECT ... FOR UPDATE — acquires a row-level lock, serialising all concurrent
         appenders on the same stream. Only one transaction holds the lock at a time.
      3. Version check inside the lock — no two agents can observe the same version
         and both succeed.

    expected_version semantics:
      -1  → assert the stream does NOT yet exist (new stream guard)
       0  → stream exists but has no events (created but empty)
       N  → stream is at exactly version N

    Outbox:
      Written in the same transaction as the domain events. If the events INSERT
      fails, the outbox INSERT is also rolled back — guaranteed at-least-once
      delivery without two-phase commit.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # Upcast registration
    # ------------------------------------------------------------------

    @staticmethod
    def register_upcast(
        event_type: str,
        from_version: int,
        fn: Callable[[dict], dict],
    ) -> None:
        """Register a payload migration from (event_type, from_version) → from_version+1."""
        _UPCASTS[(event_type, from_version)] = fn

    # ------------------------------------------------------------------
    # append
    # ------------------------------------------------------------------

    async def append(
        self,
        stream_id: str,
        events: list[NewEvent],
        expected_version: int,
        aggregate_type: str = "Unknown",
        correlation_id: str | None = None,
        causation_id: str | None = None,
        destination: str = "default",
    ) -> int:
        """
        Atomically append events to stream_id and write outbox rows.

        Returns the new stream version.
        Raises OptimisticConcurrencyError if current version != expected_version.

        correlation_id / causation_id are injected into every event's metadata
        so the full causal chain is queryable without touching the payload.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():

                if expected_version == -1:
                    # New stream: INSERT must succeed; conflict means it already exists.
                    result = await conn.execute(
                        """
                        INSERT INTO event_streams (stream_id, aggregate_type, current_version)
                        VALUES ($1, $2, 0)
                        ON CONFLICT (stream_id) DO NOTHING
                        """,
                        stream_id,
                        aggregate_type,
                    )
                    # "INSERT 0 0" means the row already existed.
                    if result == "INSERT 0 0":
                        raise OptimisticConcurrencyError(stream_id, -1, 0)
                else:
                    # Existing stream: ensure the row exists (no-op if already present).
                    await conn.execute(
                        """
                        INSERT INTO event_streams (stream_id, aggregate_type, current_version)
                        VALUES ($1, $2, 0)
                        ON CONFLICT (stream_id) DO NOTHING
                        """,
                        stream_id,
                        aggregate_type,
                    )

                # Acquire row-level lock — serialises all concurrent appenders.
                row = await conn.fetchrow(
                    """
                    SELECT current_version FROM event_streams
                    WHERE stream_id = $1
                    FOR UPDATE
                    """,
                    stream_id,
                )
                current_version: int = row["current_version"]

                if expected_version != -1 and current_version != expected_version:
                    raise OptimisticConcurrencyError(stream_id, expected_version, current_version)

                for i, event in enumerate(events):
                    next_position = current_version + i + 1

                    # Merge correlation/causation into metadata without mutating the caller's dict.
                    enriched_metadata = {
                        **event.metadata,
                        **({"correlation_id": correlation_id} if correlation_id else {}),
                        **({"causation_id": causation_id} if causation_id else {}),
                    }

                    await conn.execute(
                        """
                        INSERT INTO events
                            (event_id, stream_id, stream_position, event_type,
                             event_version, payload, metadata)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        event.event_id,
                        stream_id,
                        next_position,
                        event.event_type,
                        event.event_version,
                        json.dumps(event.payload),
                        json.dumps(enriched_metadata),
                    )

                    await conn.execute(
                        """
                        INSERT INTO outbox (event_id, destination, payload)
                        VALUES ($1, $2, $3)
                        """,
                        event.event_id,
                        destination,
                        json.dumps(event.payload),
                    )

                new_version = current_version + len(events)
                await conn.execute(
                    """
                    UPDATE event_streams
                    SET current_version = $1
                    WHERE stream_id = $2
                    """,
                    new_version,
                    stream_id,
                )

        return new_version

    # ------------------------------------------------------------------
    # load_stream
    # ------------------------------------------------------------------

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: int | None = None,
    ) -> list[RecordedEvent]:
        """
        Return events for stream_id in stream_position order, with upcasting applied.

        from_position: inclusive lower bound (0 = all events).
        to_position:   inclusive upper bound (None = no upper bound).

        Supports time-travel: load_stream(id, to_position=N) replays the aggregate
        as it existed after event N — without touching the stored payloads.
        """
        async with self._pool.acquire() as conn:
            if to_position is None:
                rows = await conn.fetch(
                    """
                    SELECT event_id, stream_id, stream_position, global_position,
                           event_type, event_version, payload, metadata, recorded_at
                    FROM events
                    WHERE stream_id = $1
                      AND stream_position > $2
                    ORDER BY stream_position
                    """,
                    stream_id,
                    from_position,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT event_id, stream_id, stream_position, global_position,
                           event_type, event_version, payload, metadata, recorded_at
                    FROM events
                    WHERE stream_id = $1
                      AND stream_position > $2
                      AND stream_position <= $3
                    ORDER BY stream_position
                    """,
                    stream_id,
                    from_position,
                    to_position,
                )
        return [_to_recorded(row) for row in rows]

    # ------------------------------------------------------------------
    # load_all  — async generator for memory-efficient projection replay
    # ------------------------------------------------------------------

    async def load_all(
        self,
        from_global_position: int = 0,
        event_types: list[str] | None = None,
        batch_size: int = 500,
    ) -> AsyncIterator[RecordedEvent]:
        """
        Async generator yielding all events in global_position order.

        Fetches in batches of batch_size to avoid loading the entire event log
        into memory — critical for projection rebuild on large stores.

        event_types: optional server-side filter; avoids transferring irrelevant
        events over the wire (equivalent to EventStoreDB $all stream filtering).
        """
        last_position = from_global_position

        async with self._pool.acquire() as conn:
            while True:
                if event_types:
                    rows = await conn.fetch(
                        """
                        SELECT event_id, stream_id, stream_position, global_position,
                               event_type, event_version, payload, metadata, recorded_at
                        FROM events
                        WHERE global_position > $1
                          AND event_type = ANY($2::text[])
                        ORDER BY global_position
                        LIMIT $3
                        """,
                        last_position,
                        event_types,
                        batch_size,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT event_id, stream_id, stream_position, global_position,
                               event_type, event_version, payload, metadata, recorded_at
                        FROM events
                        WHERE global_position > $1
                        ORDER BY global_position
                        LIMIT $2
                        """,
                        last_position,
                        batch_size,
                    )

                if not rows:
                    return

                for row in rows:
                    yield _to_recorded(row)

                last_position = rows[-1]["global_position"]

                # If we got fewer rows than batch_size, we've reached the end.
                if len(rows) < batch_size:
                    return

    # ------------------------------------------------------------------
    # stream_version
    # ------------------------------------------------------------------

    async def stream_version(self, stream_id: str) -> int:
        """Return the current version of a stream, or 0 if it does not exist."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT current_version FROM event_streams WHERE stream_id = $1",
                stream_id,
            )
        return row["current_version"] if row else 0

    # ------------------------------------------------------------------
    # archive_stream
    # ------------------------------------------------------------------

    async def archive_stream(self, stream_id: str) -> None:
        """
        Soft-archive a stream by setting archived_at = now().
        The stream and all its events remain fully queryable — nothing is deleted.
        Idempotent: archiving an already-archived stream is a no-op.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE event_streams
                SET archived_at = now()
                WHERE stream_id = $1
                  AND archived_at IS NULL
                """,
                stream_id,
            )

    # ------------------------------------------------------------------
    # get_stream_metadata
    # ------------------------------------------------------------------

    async def get_stream_metadata(self, stream_id: str) -> StreamMetadata:
        """
        Return metadata for a stream.
        Raises StreamNotFoundError if the stream does not exist.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT stream_id, aggregate_type, current_version,
                       created_at, archived_at, metadata
                FROM event_streams
                WHERE stream_id = $1
                """,
                stream_id,
            )
        if row is None:
            raise StreamNotFoundError(f"Stream '{stream_id}' does not exist.")
        return StreamMetadata(
            stream_id=row["stream_id"],
            aggregate_type=row["aggregate_type"],
            current_version=row["current_version"],
            created_at=row["created_at"],
            archived_at=row["archived_at"],
            metadata=_decode(row["metadata"]),
        )
