from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# Re-export domain exceptions so consumers import from one place.
from src.event_store import OptimisticConcurrencyError  # noqa: F401
from src.aggregates.base import DomainError             # noqa: F401


class BaseEvent(BaseModel):
    """
    Minimum envelope every domain event must carry before being appended.
    event_id: client-supplied for idempotent retry safety.
    event_version: schema version; drives the upcaster chain on load.
    """
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_version: int = 1


class StoredEvent(BaseModel):
    """A domain event as it exists in the store after being persisted."""
    event_id: uuid.UUID
    stream_id: str
    stream_position: int
    global_position: int
    event_type: str
    event_version: int
    payload: dict[str, Any]
    metadata: dict[str, Any]
    recorded_at: datetime


class StreamMetadata(BaseModel):
    """Mirrors the event_streams table row."""
    stream_id: str
    aggregate_type: str
    current_version: int
    created_at: datetime
    archived_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
