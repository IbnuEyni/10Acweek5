from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from src.event_store import RecordedEvent

logger = logging.getLogger(__name__)


@dataclass
class ProjectionError:
    """Recorded when a projection handler fails on a specific event."""
    projection_name: str
    global_position: int
    event_type: str
    error: str
    attempts: int


class Projection(ABC):
    """
    Base class for all read-model projections.

    Each projection:
    - Declares which event_types it handles (empty = all events).
    - Implements handle(event, conn) to update its read model.
    - Exposes get_lag(conn) → milliseconds behind the latest stored event.
    - Exposes rebuild(conn) → truncate + replay from position 0.

    The daemon calls handle() inside a transaction; if it raises, the daemon
    logs the error, increments the retry counter, and skips after max_retries.
    """

    #: Override in subclasses to subscribe to specific event types only.
    #: Empty list = subscribe to ALL events.
    event_types: list[str] = []

    #: Maximum retries before an event is skipped with an error log.
    max_retries: int = 3

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique projection name — used as the checkpoint key."""

    @abstractmethod
    async def handle(self, event: RecordedEvent, conn: asyncpg.Connection) -> None:
        """
        Process a single event and update the read model.
        Called inside a transaction by the daemon.
        Must be idempotent — the daemon may replay events after a restart.
        """

    async def get_lag(self, conn: asyncpg.Connection) -> float:
        """
        Return lag in milliseconds: time between the latest event in the store
        and the latest event this projection has processed.

        Returns 0.0 if the projection is fully caught up.
        """
        row = await conn.fetchrow(
            """
            SELECT
                EXTRACT(EPOCH FROM (
                    (SELECT recorded_at FROM events ORDER BY global_position DESC LIMIT 1)
                    -
                    (SELECT e.recorded_at FROM events e
                     JOIN projection_checkpoints pc ON e.global_position = pc.last_position
                     WHERE pc.projection_name = $1)
                )) * 1000 AS lag_ms
            """,
            self.name,
        )
        if row is None or row["lag_ms"] is None:
            return 0.0
        return float(row["lag_ms"])

    @abstractmethod
    async def rebuild(self, conn: asyncpg.Connection) -> None:
        """
        Truncate the projection table and reset the checkpoint to 0.
        The daemon will replay all events from position 0 on the next poll.
        Must not block live reads — use TRUNCATE ... RESTART IDENTITY.
        """
