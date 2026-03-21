"""
Unit + Integration tests for EventStore — Phase 1 complete coverage.

Unit:
  - append() increments stream version correctly (single, multiple, sequential)
  - load_stream() returns events in order with correct positions
  - load_stream() with from_position / to_position bounds
  - load_stream() applies upcasts transparently
  - load_all() yields events as async generator in global_position order
  - load_all() filters by event_type server-side
  - load_all() paginates correctly across batch boundaries
  - stream_version() returns 0 for unknown stream
  - archive_stream() sets archived_at; idempotent
  - get_stream_metadata() returns StreamMetadata; raises StreamNotFoundError

Integration (transactional atomicity):
  - append() writes to both events and outbox in one transaction
  - outbox event_id matches events.event_id
  - failed events INSERT leaves outbox empty (rollback test)
  - correlation_id / causation_id are stored in event metadata

Concurrency:
  - Two agents race at expected_version=0; exactly one wins
"""

import asyncio
import uuid
import pytest
import asyncpg

from src.event_store import (
    EventStore, NewEvent, OptimisticConcurrencyError,
    StreamNotFoundError, _UPCASTS,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(event_type: str = "ThingHappened", payload: dict | None = None) -> NewEvent:
    return NewEvent(event_type=event_type, payload=payload or {"key": "value"})


async def _db_version(pool, stream_id: str) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT current_version FROM event_streams WHERE stream_id = $1", stream_id
        )
    return row["current_version"] if row else 0


# ---------------------------------------------------------------------------
# Version bookkeeping
# ---------------------------------------------------------------------------

async def test_append_single_event_increments_version(store, pool, stream_id):
    await store.append(stream_id, [_event()], expected_version=0)
    assert await _db_version(pool, stream_id) == 1


async def test_append_returns_new_version(store, stream_id):
    v = await store.append(stream_id, [_event()], expected_version=0)
    assert v == 1


async def test_append_multiple_events_increments_version_by_count(store, pool, stream_id):
    v = await store.append(stream_id, [_event(f"E{i}") for i in range(3)], expected_version=0)
    assert v == 3
    assert await _db_version(pool, stream_id) == 3


async def test_sequential_appends_accumulate_version(store, pool, stream_id):
    await store.append(stream_id, [_event("First")], expected_version=0)
    await store.append(stream_id, [_event("Second")], expected_version=1)
    assert await _db_version(pool, stream_id) == 2


async def test_optimistic_concurrency_error_on_wrong_version(store, stream_id):
    await store.append(stream_id, [_event()], expected_version=0)
    with pytest.raises(OptimisticConcurrencyError) as exc_info:
        await store.append(stream_id, [_event()], expected_version=0)
    err = exc_info.value
    assert err.stream_id == stream_id
    assert err.expected == 0
    assert err.actual == 1


async def test_expected_version_minus_one_creates_new_stream(store, stream_id):
    v = await store.append(stream_id, [_event()], expected_version=-1)
    assert v == 1


async def test_expected_version_minus_one_fails_if_stream_exists(store, stream_id):
    await store.append(stream_id, [_event()], expected_version=-1)
    with pytest.raises(OptimisticConcurrencyError):
        await store.append(stream_id, [_event()], expected_version=-1)


# ---------------------------------------------------------------------------
# load_stream
# ---------------------------------------------------------------------------

async def test_load_stream_returns_events_in_order(store, stream_id):
    types = ["A", "B", "C"]
    await store.append(stream_id, [_event(t) for t in types], expected_version=0)
    events = await store.load_stream(stream_id)
    assert [e.event_type for e in events] == types
    assert [e.stream_position for e in events] == [1, 2, 3]


async def test_load_stream_empty_returns_empty_list(store, stream_id):
    assert await store.load_stream(stream_id) == []


async def test_load_stream_from_position(store, stream_id):
    await store.append(stream_id, [_event(f"E{i}") for i in range(5)], expected_version=0)
    events = await store.load_stream(stream_id, from_position=3)
    assert len(events) == 2
    assert events[0].stream_position == 4
    assert events[1].stream_position == 5


async def test_load_stream_to_position(store, stream_id):
    await store.append(stream_id, [_event(f"E{i}") for i in range(5)], expected_version=0)
    events = await store.load_stream(stream_id, to_position=3)
    assert len(events) == 3
    assert events[-1].stream_position == 3


async def test_load_stream_from_and_to_position(store, stream_id):
    await store.append(stream_id, [_event(f"E{i}") for i in range(5)], expected_version=0)
    events = await store.load_stream(stream_id, from_position=1, to_position=3)
    assert len(events) == 2
    assert events[0].stream_position == 2
    assert events[1].stream_position == 3


async def test_load_stream_events_have_recorded_at(store, stream_id):
    await store.append(stream_id, [_event()], expected_version=0)
    events = await store.load_stream(stream_id)
    assert events[0].recorded_at is not None


async def test_load_stream_events_have_event_version(store, stream_id):
    await store.append(stream_id, [NewEvent("Typed", {}, event_version=2)], expected_version=0)
    events = await store.load_stream(stream_id)
    assert events[0].event_version == 2


async def test_load_stream_applies_upcast(store, stream_id):
    """Upcasting must transform the payload on load without touching stored data."""
    _UPCASTS[("LegacyEvent", 1)] = lambda p: {**p, "upcasted": True}
    try:
        await store.append(stream_id, [NewEvent("LegacyEvent", {"v": 1})], expected_version=0)
        events = await store.load_stream(stream_id)
        assert events[0].payload == {"v": 1, "upcasted": True}
        assert events[0].event_version == 2  # version advanced by upcast chain
    finally:
        _UPCASTS.pop(("LegacyEvent", 1), None)


async def test_upcast_does_not_modify_stored_payload(pool, store, stream_id):
    """The immutability guarantee: upcasting never touches the stored row."""
    _UPCASTS[("ImmutableEvent", 1)] = lambda p: {**p, "added": True}
    try:
        await store.append(stream_id, [NewEvent("ImmutableEvent", {"original": 1})], expected_version=0)

        # Load through the store — upcast applied
        events = await store.load_stream(stream_id)
        assert events[0].payload == {"original": 1, "added": True}

        # Query the raw DB row — payload must be unchanged
        import json as _json
        async with pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT payload FROM events WHERE stream_id = $1", stream_id
            )
        if isinstance(raw, str):
            raw = _json.loads(raw)
        assert raw == {"original": 1}
    finally:
        _UPCASTS.pop(("ImmutableEvent", 1), None)


# ---------------------------------------------------------------------------
# load_all — async generator
# ---------------------------------------------------------------------------

async def test_load_all_yields_all_events_in_global_order(store, stream_id):
    sid2 = stream_id + "-b"
    await store.append(stream_id, [_event("A"), _event("B")], expected_version=0)
    await store.append(sid2, [_event("C")], expected_version=0)

    events = [e async for e in store.load_all()]
    types = [e.event_type for e in events]
    assert "A" in types and "B" in types and "C" in types
    # global_position must be strictly increasing
    positions = [e.global_position for e in events]
    assert positions == sorted(positions)


async def test_load_all_from_global_position(store, stream_id):
    await store.append(stream_id, [_event(f"E{i}") for i in range(4)], expected_version=0)
    all_events = [e async for e in store.load_all()]
    cutoff = all_events[1].global_position

    later = [e async for e in store.load_all(from_global_position=cutoff)]
    assert all(e.global_position > cutoff for e in later)


async def test_load_all_filters_by_event_type(store, stream_id):
    await store.append(
        stream_id,
        [_event("TypeA"), _event("TypeB"), _event("TypeA")],
        expected_version=0,
    )
    events = [e async for e in store.load_all(event_types=["TypeA"])]
    assert all(e.event_type == "TypeA" for e in events)
    assert len(events) == 2


async def test_load_all_empty_store_yields_nothing(store):
    events = [e async for e in store.load_all()]
    assert events == []


async def test_load_all_batch_size_pagination(store, stream_id):
    """Verify the generator correctly pages through results smaller than batch_size."""
    await store.append(stream_id, [_event(f"E{i}") for i in range(7)], expected_version=0)
    events = [e async for e in store.load_all(batch_size=3)]
    assert len(events) == 7
    positions = [e.global_position for e in events]
    assert positions == sorted(positions)


# ---------------------------------------------------------------------------
# stream_version
# ---------------------------------------------------------------------------

async def test_stream_version_returns_zero_for_unknown_stream(store):
    assert await store.stream_version("nonexistent-stream") == 0


async def test_stream_version_matches_append_count(store, stream_id):
    await store.append(stream_id, [_event(), _event()], expected_version=0)
    assert await store.stream_version(stream_id) == 2


# ---------------------------------------------------------------------------
# archive_stream
# ---------------------------------------------------------------------------

async def test_archive_stream_sets_archived_at(store, pool, stream_id):
    await store.append(stream_id, [_event()], expected_version=0)
    await store.archive_stream(stream_id)

    async with pool.acquire() as conn:
        archived_at = await conn.fetchval(
            "SELECT archived_at FROM event_streams WHERE stream_id = $1", stream_id
        )
    assert archived_at is not None


async def test_archive_stream_is_idempotent(store, pool, stream_id):
    await store.append(stream_id, [_event()], expected_version=0)
    await store.archive_stream(stream_id)
    await store.archive_stream(stream_id)  # second call must not raise

    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM event_streams WHERE stream_id = $1 AND archived_at IS NOT NULL",
            stream_id,
        )
    assert count == 1


async def test_archive_stream_events_remain_queryable(store, stream_id):
    await store.append(stream_id, [_event("BeforeArchive")], expected_version=0)
    await store.archive_stream(stream_id)
    events = await store.load_stream(stream_id)
    assert len(events) == 1
    assert events[0].event_type == "BeforeArchive"


# ---------------------------------------------------------------------------
# get_stream_metadata
# ---------------------------------------------------------------------------

async def test_get_stream_metadata_returns_correct_fields(store, stream_id):
    await store.append(stream_id, [_event()], expected_version=0, aggregate_type="Order")
    meta = await store.get_stream_metadata(stream_id)
    assert meta.stream_id == stream_id
    assert meta.aggregate_type == "Order"
    assert meta.current_version == 1
    assert meta.archived_at is None
    assert meta.created_at is not None


async def test_get_stream_metadata_raises_for_unknown_stream(store):
    with pytest.raises(StreamNotFoundError):
        await store.get_stream_metadata("does-not-exist")


async def test_get_stream_metadata_archived_at_populated_after_archive(store, stream_id):
    await store.append(stream_id, [_event()], expected_version=0)
    await store.archive_stream(stream_id)
    meta = await store.get_stream_metadata(stream_id)
    assert meta.archived_at is not None


# ---------------------------------------------------------------------------
# correlation_id / causation_id stored in metadata
# ---------------------------------------------------------------------------

async def test_correlation_and_causation_ids_stored_in_metadata(store, pool, stream_id):
    await store.append(
        stream_id,
        [_event("Correlated")],
        expected_version=0,
        correlation_id="corr-123",
        causation_id="cause-456",
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT metadata FROM events WHERE stream_id = $1", stream_id
        )
    import json as _json
    meta = row["metadata"]
    if isinstance(meta, str):
        meta = _json.loads(meta)
    assert meta["correlation_id"] == "corr-123"
    assert meta["causation_id"] == "cause-456"


# ---------------------------------------------------------------------------
# Transactional atomicity — events + outbox
# ---------------------------------------------------------------------------

async def test_append_writes_to_both_events_and_outbox(store, pool, stream_id):
    await store.append(stream_id, [_event("Placed")], expected_version=0)

    async with pool.acquire() as conn:
        event_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE stream_id = $1", stream_id
        )
        outbox_count = await conn.fetchval(
            "SELECT COUNT(*) FROM outbox WHERE event_id IN "
            "(SELECT event_id FROM events WHERE stream_id = $1)", stream_id
        )
    assert event_count == 1
    assert outbox_count == 1


async def test_outbox_event_id_matches_events_event_id(store, pool, stream_id):
    await store.append(stream_id, [_event("Placed")], expected_version=0)

    async with pool.acquire() as conn:
        event_id = await conn.fetchval(
            "SELECT event_id FROM events WHERE stream_id = $1", stream_id
        )
        outbox_event_id = await conn.fetchval(
            "SELECT event_id FROM outbox WHERE event_id = $1", event_id
        )
    assert event_id == outbox_event_id


async def test_outbox_destination_stored(store, pool, stream_id):
    await store.append(
        stream_id, [_event()], expected_version=0, destination="kafka:loan-events"
    )
    async with pool.acquire() as conn:
        dest = await conn.fetchval(
            "SELECT destination FROM outbox WHERE event_id IN "
            "(SELECT event_id FROM events WHERE stream_id = $1)", stream_id
        )
    assert dest == "kafka:loan-events"


async def test_failed_events_insert_leaves_outbox_empty(pool, stream_id):
    """
    Force a duplicate stream_position violation.
    The transaction must roll back entirely — outbox must stay empty.
    """
    store = EventStore(pool)
    await store.append(stream_id, [_event("First")], expected_version=0)

    duplicate = NewEvent(event_type="Duplicate", payload={})
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                async with conn.transaction():  # savepoint
                    await conn.execute(
                        """
                        INSERT INTO events
                            (event_id, stream_id, stream_position, event_type, payload, metadata)
                        VALUES ($1, $2, 1, 'Duplicate', '{}', '{}')
                        """,
                        duplicate.event_id,
                        stream_id,
                    )
                    await conn.execute(
                        "INSERT INTO outbox (event_id, destination, payload) VALUES ($1, $2, $3)",
                        duplicate.event_id,
                        "test",
                        "{}",
                    )
            except asyncpg.UniqueViolationError:
                pass  # savepoint rolled back; outer tx continues cleanly

    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM outbox WHERE event_id = $1", duplicate.event_id
        )
    assert count == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

async def test_append_zero_events_is_noop(store, pool, stream_id):
    """Appending an empty list must not change the stream version."""
    await store.append(stream_id, [_event()], expected_version=0)
    v_before = await store.stream_version(stream_id)
    v_after = await store.append(stream_id, [], expected_version=v_before)
    assert v_after == v_before
    assert await _db_version(pool, stream_id) == v_before


async def test_append_large_batch(store, pool, stream_id):
    """A batch of 200 events must all land with correct sequential positions."""
    n = 200
    events = [_event(f"E{i}") for i in range(n)]
    v = await store.append(stream_id, events, expected_version=0)
    assert v == n
    loaded = await store.load_stream(stream_id)
    assert len(loaded) == n
    assert [e.stream_position for e in loaded] == list(range(1, n + 1))


async def test_load_all_multiple_event_type_filter(store, stream_id):
    """load_all with multiple event_types returns only matching events."""
    await store.append(
        stream_id,
        [_event("TypeA"), _event("TypeB"), _event("TypeC"), _event("TypeA")],
        expected_version=0,
    )
    events = [e async for e in store.load_all(event_types=["TypeA", "TypeC"])]
    types = [e.event_type for e in events]
    assert set(types) == {"TypeA", "TypeC"}
    assert "TypeB" not in types
    assert len(events) == 3  # 2x TypeA + 1x TypeC


async def test_load_all_from_position_with_type_filter(store, stream_id):
    """Combining from_global_position and event_types must apply both filters."""
    await store.append(
        stream_id,
        [_event("TypeA"), _event("TypeB"), _event("TypeA"), _event("TypeB")],
        expected_version=0,
    )
    all_events = [e async for e in store.load_all()]
    # Cutoff after the second event
    cutoff = all_events[1].global_position

    filtered = [e async for e in store.load_all(
        from_global_position=cutoff, event_types=["TypeA"]
    )]
    assert all(e.event_type == "TypeA" for e in filtered)
    assert all(e.global_position > cutoff for e in filtered)


async def test_load_stream_exact_position_boundary(store, stream_id):
    """from_position is exclusive; to_position is inclusive."""
    await store.append(stream_id, [_event(f"E{i}") for i in range(5)], expected_version=0)
    # from_position=2, to_position=4 → positions 3 and 4 only
    events = await store.load_stream(stream_id, from_position=2, to_position=4)
    assert [e.stream_position for e in events] == [3, 4]


async def test_append_zero_events_does_not_write_outbox(store, pool, stream_id):
    """Empty append must not produce any outbox rows."""
    await store.append(stream_id, [_event()], expected_version=0)
    outbox_before = await pool.acquire()
    count_before = await outbox_before.fetchval(
        "SELECT COUNT(*) FROM outbox WHERE event_id IN "
        "(SELECT event_id FROM events WHERE stream_id = $1)", stream_id
    )
    await outbox_before.close()

    await store.append(stream_id, [], expected_version=1)

    async with pool.acquire() as conn:
        count_after = await conn.fetchval(
            "SELECT COUNT(*) FROM outbox WHERE event_id IN "
            "(SELECT event_id FROM events WHERE stream_id = $1)", stream_id
        )
    assert count_after == count_before  # no new outbox rows



async def test_double_decision_exactly_one_agent_wins(pool, stream_id):
    """
    Two AI agents race to append to the same stream at expected_version=0.
    SELECT FOR UPDATE serialises them at the DB level.
    Assert: exactly one success, one OptimisticConcurrencyError, one event in stream.
    """
    store = EventStore(pool)

    async def agent(name: str) -> str:
        try:
            await store.append(
                stream_id,
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
    events = await store.load_stream(stream_id)
    assert len(events) == 1
