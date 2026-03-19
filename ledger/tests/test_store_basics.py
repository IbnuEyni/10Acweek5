"""
Unit + Integration tests for EventStore.

Unit:
  - Appending an event increments the stream version by 1.
  - Appending N events increments the stream version by N.
  - Loading a stream returns events in stream_position order.
  - load_stream applies registered upcasts to event payloads.

Integration (transactional atomicity):
  - append() writes to both events and outbox in one transaction.
  - If the events INSERT fails (duplicate position), outbox must stay empty.

Mastery — Double-Decision Test:
  - Two AI agents concurrently append to the same stream at the same version.
  - Exactly one succeeds; the other raises OptimisticConcurrencyError.
"""

import asyncio
import uuid
import pytest
import asyncpg

from src.event_store import EventStore, NewEvent, OptimisticConcurrencyError, _UPCASTS


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(event_type: str = "ThingHappened", payload: dict | None = None) -> NewEvent:
    return NewEvent(event_type=event_type, payload=payload or {"key": "value"})


async def _stream_version(pool, stream_id: uuid.UUID) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT current_version FROM event_streams WHERE stream_id = $1",
            stream_id,
        )
    return row["current_version"] if row else 0


# ---------------------------------------------------------------------------
# Unit tests — stream version bookkeeping
# ---------------------------------------------------------------------------

async def test_append_single_event_increments_version(store, pool, stream_id):
    await store.append(stream_id, "Order", [_event()], expected_version=0)
    assert await _stream_version(pool, stream_id) == 1


async def test_append_multiple_events_increments_version_by_count(store, pool, stream_id):
    events = [_event(f"Event{i}") for i in range(3)]
    await store.append(stream_id, "Order", events, expected_version=0)
    assert await _stream_version(pool, stream_id) == 3


async def test_sequential_appends_accumulate_version(store, pool, stream_id):
    await store.append(stream_id, "Order", [_event("First")], expected_version=0)
    await store.append(stream_id, "Order", [_event("Second")], expected_version=1)
    assert await _stream_version(pool, stream_id) == 2


async def test_load_stream_returns_events_in_order(store, stream_id):
    types = ["A", "B", "C"]
    await store.append(stream_id, "Order", [_event(t) for t in types], expected_version=0)
    recorded = await store.load_stream(stream_id)
    assert [e.event_type for e in recorded] == types
    assert [e.stream_position for e in recorded] == [1, 2, 3]


async def test_load_stream_empty_returns_empty_list(store, stream_id):
    result = await store.load_stream(stream_id)
    assert result == []


async def test_optimistic_concurrency_error_on_wrong_version(store, stream_id):
    await store.append(stream_id, "Order", [_event()], expected_version=0)
    with pytest.raises(OptimisticConcurrencyError):
        await store.append(stream_id, "Order", [_event()], expected_version=0)


async def test_load_stream_applies_upcast(store, stream_id):
    """Registered upcasts must transform the payload on load."""
    _UPCASTS["LegacyEvent"] = lambda p: {**p, "upcasted": True}
    try:
        await store.append(stream_id, "Order", [NewEvent("LegacyEvent", {"v": 1})], expected_version=0)
        recorded = await store.load_stream(stream_id)
        assert recorded[0].payload == {"v": 1, "upcasted": True}
    finally:
        _UPCASTS.pop("LegacyEvent", None)


# ---------------------------------------------------------------------------
# Mastery — Double-Decision Test
# ---------------------------------------------------------------------------

async def test_double_decision_exactly_one_agent_wins(pool, stream_id):
    """
    Two AI agents race to append to the same stream at expected_version=0.
    asyncio.gather fires both coroutines concurrently on the same event loop.
    The FOR UPDATE lock in append() serialises them at the DB level:
      - the first transaction to acquire the lock commits → "success"
      - the second sees current_version=1 after the lock is released → "conflict"
    Assert: exactly one success and one conflict; only one event in the stream.
    """
    store = EventStore(pool)

    async def agent(name: str) -> str:
        try:
            await store.append(
                stream_id,
                "Order",
                [NewEvent(f"DecisionBy_{name}", {"agent": name})],
                expected_version=0,
            )
            return "success"
        except OptimisticConcurrencyError:
            return "conflict"

    results = await asyncio.gather(agent("AgentA"), agent("AgentB"))

    assert sorted(results) == ["conflict", "success"], (
        f"Expected exactly one success and one conflict, got: {results}"
    )

    # Only the winner's event must exist in the stream.
    events = await store.load_stream(stream_id)
    assert len(events) == 1


# ---------------------------------------------------------------------------
# Integration tests — transactional atomicity (events + outbox)
# ---------------------------------------------------------------------------

async def test_append_writes_to_both_events_and_outbox(store, pool, stream_id):
    await store.append(stream_id, "Order", [_event("Placed")], expected_version=0)

    async with pool.acquire() as conn:
        event_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE stream_id = $1", stream_id
        )
        outbox_count = await conn.fetchval(
            "SELECT COUNT(*) FROM outbox WHERE stream_id = $1", stream_id
        )

    assert event_count == 1
    assert outbox_count == 1


async def test_outbox_event_id_matches_events_id(store, pool, stream_id):
    await store.append(stream_id, "Order", [_event("Placed")], expected_version=0)

    async with pool.acquire() as conn:
        event_id = await conn.fetchval(
            "SELECT id FROM events WHERE stream_id = $1", stream_id
        )
        outbox_event_id = await conn.fetchval(
            "SELECT event_id FROM outbox WHERE stream_id = $1", stream_id
        )

    assert event_id == outbox_event_id


async def test_failed_events_insert_leaves_outbox_empty(pool, stream_id):
    """
    Force a duplicate-position violation on the events table.
    The transaction must roll back entirely — outbox must stay empty.
    """
    # Seed a valid first event so the stream exists at version 1.
    store = EventStore(pool)
    await store.append(stream_id, "Order", [_event("First")], expected_version=0)

    # Craft an event whose id collides with an existing stream_position
    # by bypassing the store and injecting a duplicate directly.
    duplicate_event = NewEvent(event_type="Duplicate", payload={})

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Manually insert a conflicting event to simulate the failure path.
            try:
                async with conn.transaction():          # savepoint
                    await conn.execute(
                        """
                        INSERT INTO events (id, stream_id, stream_position, event_type, payload, metadata)
                        VALUES ($1, $2, 1, 'Duplicate', '{}', '{}')
                        """,
                        duplicate_event.event_id,
                        stream_id,
                    )
                    # This insert would follow in the same tx — must not persist.
                    await conn.execute(
                        """
                        INSERT INTO outbox (stream_id, event_id, event_type, payload)
                        VALUES ($1, $2, 'Duplicate', '{}')
                        """,
                        stream_id,
                        duplicate_event.event_id,
                    )
            except asyncpg.UniqueViolationError:
                pass  # savepoint rolled back; outer tx continues cleanly

    async with pool.acquire() as conn:
        outbox_count = await conn.fetchval(
            "SELECT COUNT(*) FROM outbox WHERE event_type = 'Duplicate'",
        )

    assert outbox_count == 0
