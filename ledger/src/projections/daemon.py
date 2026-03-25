from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from src.event_store import EventStore, RecordedEvent
from src.projections.base import Projection

logger = logging.getLogger(__name__)


@dataclass
class _RetryState:
    """Tracks consecutive failures for a single (projection, global_position) pair."""
    attempts: int = 0
    last_error: str = ""


class ProjectionDaemon:
    """
    Fault-tolerant background asyncio task that keeps all registered projections
    current by polling the events table from the last processed global_position.

    Design decisions:
    - Per-projection checkpoints: each projection advances independently.
      A slow projection does not block a fast one.
    - Fault isolation: if one projection's handler raises, the daemon logs the
      error, increments the retry counter for that (projection, position) pair,
      and continues processing other projections and events. After max_retries
      the event is skipped with an ERROR log — a daemon that crashes on a bad
      event is a production incident.
    - Lag metric: get_lag(name) returns milliseconds between the latest event
      in the store and the latest event that projection has processed.
    - get_all_lags() is the watchdog endpoint — p99 < 10ms.

    Mirrors Marten's Async Daemon pattern: each projection has its own
    checkpoint, processes events in order, and can be rebuilt independently.
    """

    def __init__(
        self,
        store: EventStore,
        projections: list[Projection],
        max_retries: int = 3,
        batch_size: int = 100,
    ) -> None:
        self._store = store
        self._projections: dict[str, Projection] = {p.name: p for p in projections}
        self._max_retries = max_retries
        self._batch_size = batch_size
        self._running = False

        # (projection_name, global_position) → retry state
        self._retry_state: dict[tuple[str, int], _RetryState] = {}

        # In-memory lag cache: projection_name → lag_ms (updated each batch)
        self._lag_cache: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run_forever(self, poll_interval_ms: int = 100) -> None:
        """
        Main loop. Runs until stop() is called.
        poll_interval_ms: sleep between batches when caught up.
        """
        self._running = True
        logger.info("ProjectionDaemon started with projections: %s",
                    list(self._projections))
        while self._running:
            try:
                processed = await self._process_batch()
                if processed == 0:
                    # Caught up — sleep before next poll
                    await asyncio.sleep(poll_interval_ms / 1000)
            except Exception:
                logger.exception("Unexpected error in ProjectionDaemon._process_batch")
                await asyncio.sleep(poll_interval_ms / 1000)

    def stop(self) -> None:
        self._running = False

    async def poll_until_caught_up(self) -> None:
        """Drive all projections to the current head of the event log. Blocks until caught up."""
        while await self._process_batch():
            pass

    # ------------------------------------------------------------------
    # Core batch processing
    # ------------------------------------------------------------------

    async def _process_batch(self) -> int:
        """
        Load events from the lowest checkpoint across all projections,
        route each event to subscribed projections, update checkpoints.

        Returns the number of events processed.
        """
        async with self._store._pool.acquire() as conn:
            # Load all checkpoints in one query
            checkpoints = await self._load_checkpoints(conn)

            # Find the lowest checkpoint — we must process from there
            min_position = min(checkpoints.values()) if checkpoints else 0

            # Fetch the next batch of events from min_position
            rows = await conn.fetch(
                """
                SELECT event_id, stream_id, stream_position, global_position,
                       event_type, event_version, payload, metadata, recorded_at
                FROM events
                WHERE global_position > $1
                ORDER BY global_position
                LIMIT $2
                """,
                min_position,
                self._batch_size,
            )

            if not rows:
                return 0

            from src.event_store import _to_recorded
            events = [_to_recorded(row) for row in rows]

            # Update lag cache before processing
            await self._update_lag_cache(conn, events[-1].global_position)

            # Route each event to subscribed projections
            for event in events:
                for name, projection in self._projections.items():
                    proj_checkpoint = checkpoints.get(name, 0)

                    # Skip events this projection has already processed
                    if event.global_position <= proj_checkpoint:
                        continue

                    # Skip events this projection doesn't subscribe to
                    if projection.event_types and event.event_type not in projection.event_types:
                        await self._advance_checkpoint(conn, name, event.global_position)
                        continue

                    await self._handle_with_retry(conn, projection, event)

            return len(events)

    async def _handle_with_retry(
        self,
        conn: asyncpg.Connection,
        projection: Projection,
        event: RecordedEvent,
    ) -> None:
        """
        Call projection.handle() inside a savepoint.
        On failure: log, increment retry counter, skip after max_retries.
        """
        key = (projection.name, event.global_position)
        state = self._retry_state.get(key, _RetryState())

        if state.attempts >= self._max_retries:
            logger.error(
                "Skipping event global_position=%d type=%s for projection=%s "
                "after %d failed attempts. Last error: %s",
                event.global_position, event.event_type,
                projection.name, state.attempts, state.last_error,
            )
            await self._advance_checkpoint(conn, projection.name, event.global_position)
            self._retry_state.pop(key, None)
            return

        try:
            async with conn.transaction():
                await projection.handle(event, conn)
                await self._advance_checkpoint(conn, projection.name, event.global_position)
            # Success — clear retry state
            self._retry_state.pop(key, None)

        except Exception as exc:
            state.attempts += 1
            state.last_error = str(exc)
            self._retry_state[key] = state
            logger.warning(
                "Projection %s failed on event global_position=%d type=%s "
                "(attempt %d/%d): %s",
                projection.name, event.global_position, event.event_type,
                state.attempts, self._max_retries, exc,
            )

    async def _advance_checkpoint(
        self,
        conn: asyncpg.Connection,
        projection_name: str,
        position: int,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO projection_checkpoints (projection_name, last_position)
            VALUES ($1, $2)
            ON CONFLICT (projection_name) DO UPDATE
            SET last_position = GREATEST(projection_checkpoints.last_position, $2),
                updated_at = NOW()
            """,
            projection_name,
            position,
        )

    async def _load_checkpoints(
        self, conn: asyncpg.Connection
    ) -> dict[str, int]:
        """Load all checkpoints, initialising missing ones to 0."""
        rows = await conn.fetch(
            "SELECT projection_name, last_position FROM projection_checkpoints"
        )
        checkpoints = {row["projection_name"]: row["last_position"] for row in rows}

        # Ensure every registered projection has a checkpoint row
        for name in self._projections:
            if name not in checkpoints:
                await conn.execute(
                    """
                    INSERT INTO projection_checkpoints (projection_name, last_position)
                    VALUES ($1, 0)
                    ON CONFLICT DO NOTHING
                    """,
                    name,
                )
                checkpoints[name] = 0

        return checkpoints

    async def _update_lag_cache(
        self, conn: asyncpg.Connection, latest_processed: int
    ) -> None:
        """Update in-memory lag cache from DB. Called once per batch."""
        rows = await conn.fetch(
            """
            SELECT
                pc.projection_name,
                EXTRACT(EPOCH FROM (
                    (SELECT recorded_at FROM events ORDER BY global_position DESC LIMIT 1)
                    -
                    COALESCE(
                        (SELECT e.recorded_at FROM events e
                         WHERE e.global_position = pc.last_position),
                        NOW()
                    )
                )) * 1000 AS lag_ms
            FROM projection_checkpoints pc
            """
        )
        for row in rows:
            lag = float(row["lag_ms"]) if row["lag_ms"] is not None else 0.0
            self._lag_cache[row["projection_name"]] = max(0.0, lag)

    # ------------------------------------------------------------------
    # Lag metrics
    # ------------------------------------------------------------------

    def get_lag(self, projection_name: str) -> float:
        """
        Return cached lag in milliseconds for a named projection.
        Updated after each batch — O(1), safe to call from the watchdog endpoint.
        Returns 0.0 if the projection is fully caught up or not yet seen.
        """
        return self._lag_cache.get(projection_name, 0.0)

    def get_all_lags(self) -> dict[str, float]:
        """
        Return lag for every registered projection.
        This is the watchdog endpoint — p99 < 10ms (pure dict lookup).
        """
        return {name: self._lag_cache.get(name, 0.0) for name in self._projections}

    # ------------------------------------------------------------------
    # Rebuild support
    # ------------------------------------------------------------------

    async def rebuild_projection(self, projection_name: str) -> None:
        """
        Truncate a single projection's read model and reset its checkpoint.
        The daemon will replay all events from position 0 on the next poll.
        Live reads continue against the (now empty) table — no downtime.
        """
        projection = self._projections.get(projection_name)
        if projection is None:
            raise ValueError(f"Unknown projection: {projection_name}")

        async with self._store._pool.acquire() as conn:
            async with conn.transaction():
                await projection.rebuild(conn)

        logger.info("Projection %s rebuilt — will replay from position 0", projection_name)
