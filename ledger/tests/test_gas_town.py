"""
Phase 4C — Gas Town Agent Memory Pattern Tests

Simulated crash recovery: start an agent session, append 5 events, then call
reconstruct_agent_context() without the in-memory agent object. Verify that
the reconstructed context contains enough information for the agent to continue.
"""
import uuid
import pytest

from src.event_store import EventStore, NewEvent
from src.integrity.gas_town import reconstruct_agent_context

pytestmark = pytest.mark.asyncio


async def _setup_session(store: EventStore, agent_id: str, session_id: str) -> str:
    """Create a full agent session with 5 events and return the stream_id."""
    stream_id = f"agent-{agent_id}-{session_id}"

    # Event 1: AgentContextLoaded (Gas Town — must be first)
    await store.append(
        stream_id,
        [NewEvent("AgentContextLoaded", {
            "agent_id": agent_id,
            "session_id": session_id,
            "context_source": "event_replay",
            "event_replay_from_position": 0,
            "context_token_count": 1500,
            "model_version": "v2.3",
            "context": {"loaded_applications": ["app-001"]},
        })],
        expected_version=0,
        aggregate_type="AgentSession",
    )

    # Event 2: CreditAnalysisCompleted for app-001
    await store.append(
        stream_id,
        [NewEvent("CreditAnalysisCompleted", {
            "application_id": "app-001",
            "agent_id": agent_id,
            "session_id": session_id,
            "model_version": "v2.3",
            "confidence_score": 0.82,
            "risk_tier": "MEDIUM",
            "recommended_limit_usd": 75000.0,
            "analysis_duration_ms": 950,
            "input_data_hash": "hash-001",
        })],
        expected_version=1,
        aggregate_type="AgentSession",
    )

    # Event 3: FraudScreeningCompleted for app-001
    await store.append(
        stream_id,
        [NewEvent("FraudScreeningCompleted", {
            "application_id": "app-001",
            "agent_id": agent_id,
            "fraud_score": 0.12,
            "anomaly_flags": [],
            "screening_model_version": "fraud-v1.1",
            "input_data_hash": "hash-002",
        })],
        expected_version=2,
        aggregate_type="AgentSession",
    )

    # Event 4: CreditAnalysisCompleted for app-002
    await store.append(
        stream_id,
        [NewEvent("CreditAnalysisCompleted", {
            "application_id": "app-002",
            "agent_id": agent_id,
            "session_id": session_id,
            "model_version": "v2.3",
            "confidence_score": 0.65,
            "risk_tier": "LOW",
            "recommended_limit_usd": 120000.0,
            "analysis_duration_ms": 1100,
            "input_data_hash": "hash-003",
        })],
        expected_version=3,
        aggregate_type="AgentSession",
    )

    # Event 5: FraudScreeningCompleted for app-002
    await store.append(
        stream_id,
        [NewEvent("FraudScreeningCompleted", {
            "application_id": "app-002",
            "agent_id": agent_id,
            "fraud_score": 0.05,
            "anomaly_flags": [],
            "screening_model_version": "fraud-v1.1",
            "input_data_hash": "hash-004",
        })],
        expected_version=4,
        aggregate_type="AgentSession",
    )

    return stream_id


async def test_reconstruct_context_after_simulated_crash(store):
    """
    Simulated crash: 5 events appended, then reconstruct_agent_context() called
    without the in-memory agent object. Verify reconstructed context is sufficient.
    """
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    # Simulate the agent running and appending 5 events
    stream_id = await _setup_session(store, agent_id, session_id)

    # Simulate crash: discard the in-memory agent object
    # Now reconstruct from the event store alone
    context = await reconstruct_agent_context(store, agent_id, session_id)

    # Verify the reconstructed context is sufficient to continue
    assert context.agent_id == agent_id
    assert context.session_id == session_id
    assert context.last_event_position == 5
    assert context.model_version == "v2.3"
    # Both app-001 and app-002 have matching CreditAnalysis + FraudScreening pairs
    assert context.session_health_status == "OK"
    assert context.pending_work == []  # no unresolved partial decisions
    assert len(context.last_3_events) == 3
    assert context.context_text  # non-empty prose summary


async def test_reconstruct_context_contains_recent_events_verbatim(store):
    """Last 3 events must be preserved verbatim in the reconstructed context."""
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]
    await _setup_session(store, agent_id, session_id)

    context = await reconstruct_agent_context(store, agent_id, session_id)

    # Last 3 events: positions 3, 4, 5
    positions = [e["stream_position"] for e in context.last_3_events]
    assert positions == [3, 4, 5]
    types = [e["event_type"] for e in context.last_3_events]
    assert "FraudScreeningCompleted" in types


async def test_reconstruct_context_needs_reconciliation_on_partial_decision(store):
    """
    If the last event is a CreditAnalysisCompleted with no corresponding
    FraudScreeningCompleted for the same app, flag NEEDS_RECONCILIATION.
    Explicitly validates pending_work content and NEEDS_RECONCILIATION status.
    """
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]
    stream_id = f"agent-{agent_id}-{session_id}"

    # AgentContextLoaded
    await store.append(
        stream_id,
        [NewEvent("AgentContextLoaded", {
            "agent_id": agent_id,
            "session_id": session_id,
            "context_source": "cold_start",
            "event_replay_from_position": 0,
            "context_token_count": 500,
            "model_version": "v2.3",
            "context": {},
        })],
        expected_version=0,
        aggregate_type="AgentSession",
    )

    # CreditAnalysisCompleted for app-999 — no completion event follows (simulated crash)
    await store.append(
        stream_id,
        [NewEvent("CreditAnalysisCompleted", {
            "application_id": "app-999",
            "agent_id": agent_id,
            "session_id": session_id,
            "model_version": "v2.3",
            "confidence_score": 0.75,
            "risk_tier": "MEDIUM",
            "recommended_limit_usd": 50000.0,
            "analysis_duration_ms": 800,
            "input_data_hash": "hash-partial",
        })],
        expected_version=1,
        aggregate_type="AgentSession",
    )

    context = await reconstruct_agent_context(store, agent_id, session_id)

    # Health status must be NEEDS_RECONCILIATION
    assert context.session_health_status == "NEEDS_RECONCILIATION"

    # pending_work must contain exactly one item referencing app-999
    assert len(context.pending_work) == 1
    assert "app-999" in context.pending_work[0]
    assert "CreditAnalysisCompleted" in context.pending_work[0]
    assert "stream_position=2" in context.pending_work[0]

    # context_text must surface the reconciliation warning
    assert "NEEDS_RECONCILIATION" in context.context_text or "reconciliation" in context.context_text.lower()
    assert "app-999" in context.context_text


async def test_reconstruct_context_empty_session(store):
    """Reconstructing a non-existent session returns a safe empty context."""
    context = await reconstruct_agent_context(store, "ghost-agent", "ghost-session")
    assert context.last_event_position == 0
    assert context.session_health_status == "OK"


async def test_reconstruct_context_closed_session(store):
    """A closed session is flagged as CLOSED."""
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]
    stream_id = f"agent-{agent_id}-{session_id}"

    await store.append(
        stream_id,
        [NewEvent("AgentContextLoaded", {
            "agent_id": agent_id,
            "session_id": session_id,
            "context_source": "cold_start",
            "event_replay_from_position": 0,
            "context_token_count": 0,
            "model_version": "v1.0",
            "context": {},
        })],
        expected_version=0,
        aggregate_type="AgentSession",
    )
    await store.append(
        stream_id,
        [NewEvent("SessionClosed", {"agent_id": agent_id, "session_id": session_id})],
        expected_version=1,
        aggregate_type="AgentSession",
    )

    context = await reconstruct_agent_context(store, agent_id, session_id)
    assert context.session_health_status == "CLOSED"


async def test_reconstruct_context_token_budget_truncation(store):
    """Context text is truncated to the token budget."""
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]
    stream_id = f"agent-{agent_id}-{session_id}"

    await store.append(
        stream_id,
        [NewEvent("AgentContextLoaded", {
            "agent_id": agent_id,
            "session_id": session_id,
            "context_source": "cold_start",
            "event_replay_from_position": 0,
            "context_token_count": 0,
            "model_version": "v1.0",
            "context": {"data": "x" * 10000},  # large context
        })],
        expected_version=0,
        aggregate_type="AgentSession",
    )

    context = await reconstruct_agent_context(store, agent_id, session_id, token_budget=100)
    # 100 tokens * 4 chars = 400 chars max
    assert len(context.context_text) <= 400 + len("\n[...truncated to token budget]") + 10


async def test_reconstruct_context_source_field(store):
    """AgentContext.context_source reflects the value from AgentContextLoaded."""
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]
    stream_id = f"agent-{agent_id}-{session_id}"

    await store.append(
        stream_id,
        [NewEvent("AgentContextLoaded", {
            "agent_id": agent_id,
            "session_id": session_id,
            "context_source": "event_replay",
            "event_replay_from_position": 10,
            "context_token_count": 2048,
            "model_version": "v3.0",
            "context": {},
        })],
        expected_version=0,
        aggregate_type="AgentSession",
    )

    context = await reconstruct_agent_context(store, agent_id, session_id)

    assert context.context_source == "event_replay"
    assert context.model_version == "v3.0"
