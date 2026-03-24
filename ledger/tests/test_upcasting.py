"""
Phase 4 — Upcasting & Immutability Tests

Required by the brief:
1. v1 event stored → loaded as v2 via upcaster → raw DB payload UNCHANGED.
2. Upcast chain walks multiple versions automatically.
3. UpcasterRegistry.upcast() returns a new RecordedEvent without mutating the original.
"""
import json
import pytest

from src.event_store import EventStore, NewEvent, _UPCASTS
from src.upcasting.registry import UpcasterRegistry

# Only async tests use asyncio mark; sync tests are plain pytest functions.


# ---------------------------------------------------------------------------
# Immutability test — the mandatory brief requirement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upcast_does_not_modify_stored_payload(pool, store, stream_id):
    """
    THE immutability test from the brief:
    1. Directly query events table — get raw stored payload of a v1 event.
    2. Load same event through EventStore.load_stream() — verify it is upcasted to v2.
    3. Directly query events table again — verify raw stored payload is UNCHANGED.
    Any system where upcasting touches the stored events has broken the core guarantee.
    """
    # Register a v1→v2 upcaster for this test
    _UPCASTS[("ImmutabilityTestEvent", 1)] = lambda p: {**p, "v2_field": "added_by_upcast"}
    try:
        # 1. Append a v1 event
        await store.append(
            stream_id,
            [NewEvent("ImmutabilityTestEvent", {"original_field": "original_value"}, event_version=1)],
            expected_version=0,
        )

        # 2. Query raw DB payload BEFORE loading through store
        async with pool.acquire() as conn:
            raw_before = await conn.fetchval(
                "SELECT payload FROM events WHERE stream_id = $1", stream_id
            )
        if isinstance(raw_before, str):
            raw_before = json.loads(raw_before)
        assert raw_before == {"original_field": "original_value"}, \
            "Raw stored payload should be the original v1 payload"

        # 3. Load through EventStore — upcast must be applied
        events = await store.load_stream(stream_id)
        assert len(events) == 1
        assert events[0].event_version == 2, "Loaded event must be upcasted to v2"
        assert events[0].payload == {"original_field": "original_value", "v2_field": "added_by_upcast"}, \
            "Loaded payload must include the upcasted field"

        # 4. Query raw DB payload AFTER loading — must be UNCHANGED
        async with pool.acquire() as conn:
            raw_after = await conn.fetchval(
                "SELECT payload FROM events WHERE stream_id = $1", stream_id
            )
        if isinstance(raw_after, str):
            raw_after = json.loads(raw_after)
        assert raw_after == {"original_field": "original_value"}, \
            "Stored payload must be UNCHANGED after upcasting — immutability guarantee violated"

    finally:
        _UPCASTS.pop(("ImmutabilityTestEvent", 1), None)


@pytest.mark.asyncio
async def test_upcast_chain_walks_multiple_versions(pool, store, stream_id):
    """v1 → v2 → v3 chain applied automatically on load."""
    _UPCASTS[("ChainEvent", 1)] = lambda p: {**p, "v2": True}
    _UPCASTS[("ChainEvent", 2)] = lambda p: {**p, "v3": True}
    try:
        await store.append(
            stream_id,
            [NewEvent("ChainEvent", {"original": True}, event_version=1)],
            expected_version=0,
        )
        events = await store.load_stream(stream_id)
        assert events[0].event_version == 3
        assert events[0].payload == {"original": True, "v2": True, "v3": True}

        # Raw DB still v1
        async with pool.acquire() as conn:
            raw = await conn.fetchval("SELECT payload FROM events WHERE stream_id = $1", stream_id)
        if isinstance(raw, str):
            raw = json.loads(raw)
        assert raw == {"original": True}
    finally:
        _UPCASTS.pop(("ChainEvent", 1), None)
        _UPCASTS.pop(("ChainEvent", 2), None)


# ---------------------------------------------------------------------------
# UpcasterRegistry class tests
# ---------------------------------------------------------------------------

def test_registry_upcast_returns_new_event_with_updated_payload():
    """UpcasterRegistry.upcast() must not mutate the original RecordedEvent."""
    from src.event_store import RecordedEvent
    import uuid
    from datetime import datetime, timezone

    registry = UpcasterRegistry()

    @registry.register("TestEvent", from_version=1)
    def v1_to_v2(payload: dict) -> dict:
        return {**payload, "added": True}

    original = RecordedEvent(
        event_id=uuid.uuid4(),
        stream_id="test-stream",
        stream_position=1,
        global_position=1,
        event_type="TestEvent",
        event_version=1,
        payload={"key": "value"},
        metadata={},
        recorded_at=datetime.now(timezone.utc),
    )
    upcasted = registry.upcast(original)

    # Original must be unchanged
    assert original.event_version == 1
    assert original.payload == {"key": "value"}

    # Upcasted must have new version and payload
    assert upcasted.event_version == 2
    assert upcasted.payload == {"key": "value", "added": True}


def test_registry_no_upcaster_returns_same_event():
    """If no upcaster is registered, upcast() returns the original event unchanged."""
    from src.event_store import RecordedEvent
    import uuid
    from datetime import datetime, timezone

    registry = UpcasterRegistry()
    event = RecordedEvent(
        event_id=uuid.uuid4(),
        stream_id="test",
        stream_position=1,
        global_position=1,
        event_type="NoUpcasterEvent",
        event_version=1,
        payload={"x": 1},
        metadata={},
        recorded_at=datetime.now(timezone.utc),
    )
    result = registry.upcast(event)
    assert result is event  # same object — no copy needed


# ---------------------------------------------------------------------------
# CreditAnalysisCompleted v1→v2 upcaster (registered in upcasters.py)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_credit_analysis_v1_upcasted_to_v2_on_load(pool, store, stream_id):
    """
    CreditAnalysisCompleted v1 (missing model_version/confidence_score) is
    transparently upcasted to v2 on load. Stored payload unchanged.
    """
    # Import to ensure upcasters are registered
    import src.upcasting.upcasters  # noqa: F401

    v1_payload = {
        "application_id": "app-001",
        "agent_id": "agent-001",
        "session_id": "sess-001",
        "risk_tier": "MEDIUM",
        "recommended_limit_usd": 50000.0,
        "analysis_duration_ms": 1200,
        "input_data_hash": "abc123",
        # Deliberately missing: model_version, confidence_score
    }
    await store.append(
        stream_id,
        [NewEvent("CreditAnalysisCompleted", v1_payload, event_version=1)],
        expected_version=0,
    )

    events = await store.load_stream(stream_id)
    assert events[0].event_version == 2
    assert events[0].payload["model_version"] == "legacy-pre-2026"
    assert events[0].payload["confidence_score"] is None  # genuinely unknown — not fabricated
    assert events[0].payload["regulatory_basis"] == "legacy-regulatory-framework"

    # Raw DB payload must be unchanged
    async with pool.acquire() as conn:
        raw = await conn.fetchval("SELECT payload FROM events WHERE stream_id = $1", stream_id)
    if isinstance(raw, str):
        raw = json.loads(raw)
    assert "model_version" not in raw or raw.get("model_version") is None or raw == v1_payload


@pytest.mark.asyncio
async def test_decision_generated_v1_upcasted_to_v2_on_load(pool, store, stream_id):
    """DecisionGenerated v1 (missing model_versions) is upcasted to v2 on load."""
    import src.upcasting.upcasters  # noqa: F401

    v1_payload = {
        "application_id": "app-002",
        "orchestrator_agent_id": "orch-001",
        "recommendation": "APPROVE",
        "confidence_score": 0.85,
        "contributing_agent_sessions": ["agent-a-s1", "agent-b-s2"],
        "decision_basis_summary": "All checks passed",
        # Deliberately missing: model_versions
    }
    await store.append(
        stream_id,
        [NewEvent("DecisionGenerated", v1_payload, event_version=1)],
        expected_version=0,
    )

    events = await store.load_stream(stream_id)
    assert events[0].event_version == 2
    assert "model_versions" in events[0].payload
    assert events[0].payload["model_versions"] == {
        "agent-a-s1": "unknown",
        "agent-b-s2": "unknown",
    }

    # Raw DB payload must be unchanged
    async with pool.acquire() as conn:
        raw = await conn.fetchval("SELECT payload FROM events WHERE stream_id = $1", stream_id)
    if isinstance(raw, str):
        raw = json.loads(raw)
    assert "model_versions" not in raw
