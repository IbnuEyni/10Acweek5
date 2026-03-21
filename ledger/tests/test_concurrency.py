"""
Week Standard — Double-Decision Concurrency Tests
==================================================
Test 1 — Store layer:
  A loan application stream has 3 prior events (version 3).
  Two AI agents simultaneously attempt to append a CreditAnalysisCompleted
  event at expected_version=3.

Test 2 — Handler layer:
  Two agents simultaneously call handle_credit_analysis_completed for the
  same application. Exactly one succeeds; the other raises
  OptimisticConcurrencyError. The loan stream ends up with exactly one
  CreditAnalysisCompleted event.
"""

import asyncio
import uuid
import pytest

from src.event_store import EventStore, NewEvent, OptimisticConcurrencyError
from src.aggregates.base import DomainError
from src.commands.handlers import (
    StartAgentSessionCommand,
    SubmitApplicationCommand,
    RequestCreditAnalysisCommand,
    CreditAnalysisCompletedCommand,
    handle_start_agent_session,
    handle_submit_application,
    handle_request_credit_analysis,
    handle_credit_analysis_completed,
)

pytestmark = pytest.mark.asyncio

AGGREGATE_TYPE = "LoanApplication"
STREAM_VERSION_BEFORE_RACE = 3


async def _seed_stream(store: EventStore, stream_id: str) -> None:
    """Append 3 prior events so the stream sits at version 3 before the race."""
    prior_events = [
        NewEvent("ApplicationSubmitted", {"applicant": "Alice"}),
        NewEvent("DocumentsVerified",    {"verified_by": "system"}),
        NewEvent("RiskScoreCalculated",  {"score": 720}),
    ]
    await store.append(
        stream_id,
        prior_events,
        expected_version=0,
        aggregate_type=AGGREGATE_TYPE,
    )


async def test_double_decision_credit_analysis_at_version_3(pool, stream_id):
    """
    Two AI agents race to append CreditAnalysisCompleted at expected_version=3.

    Assertions
    ----------
    1. Exactly one agent succeeds ("success") and one gets a conflict ("conflict").
    2. Final stream contains exactly 4 events — the 3 seeded + 1 winner's event.
    3. The winning event is CreditAnalysisCompleted at stream_position=4.
    4. The losing agent's event is NOT in the stream.
    """
    store = EventStore(pool)
    await _seed_stream(store, stream_id)

    async def agent(name: str) -> str:
        try:
            await store.append(
                stream_id,
                [NewEvent("CreditAnalysisCompleted", {"decision": "approved", "agent": name})],
                expected_version=STREAM_VERSION_BEFORE_RACE,
                aggregate_type=AGGREGATE_TYPE,
            )
            return "success"
        except OptimisticConcurrencyError:
            return "conflict"

    results = await asyncio.gather(agent("AgentA"), agent("AgentB"))

    # --- assertion 1: exactly one winner, one loser ---
    assert sorted(results) == ["conflict", "success"], (
        f"Expected one success and one conflict, got: {results}"
    )

    # --- assertion 2: stream length is 4, not 5 ---
    events = await store.load_stream(stream_id)
    assert len(events) == 4, (
        f"Stream must have exactly 4 events (3 seeded + 1 winner), got {len(events)}"
    )

    # --- assertion 3: the 4th event is CreditAnalysisCompleted at position 4 ---
    assert events[3].event_type == "CreditAnalysisCompleted"
    assert events[3].stream_position == 4

    # --- assertion 4: only one agent's decision is recorded ---
    decisions = [e.payload["agent"] for e in events if e.event_type == "CreditAnalysisCompleted"]
    assert len(decisions) == 1, "Exactly one CreditAnalysisCompleted must be in the stream"


async def test_concurrent_handler_credit_analysis_exactly_one_wins(pool):
    """
    Handler-layer concurrency test.

    Two agents simultaneously call handle_credit_analysis_completed for the
    same application. The SELECT FOR UPDATE in EventStore.append() serialises
    both transactions at the DB level.

    Assertions
    ----------
    1. Exactly one handler call succeeds; the other raises OptimisticConcurrencyError.
    2. The loan stream contains exactly one CreditAnalysisCompleted event.
    3. The agent session stream for the winning agent contains CreditAnalysisCompleted.
    4. The loan stream version is exactly 3 (Submitted + CreditAnalysisRequested + CreditAnalysisCompleted).
    """
    store = EventStore(pool)
    app_id = str(uuid.uuid4())
    agent_a_id = str(uuid.uuid4())
    agent_b_id = str(uuid.uuid4())
    session_a_id = str(uuid.uuid4())
    session_b_id = str(uuid.uuid4())

    # Seed: two independent agent sessions + one loan at AwaitingAnalysis
    await handle_start_agent_session(
        StartAgentSessionCommand(agent_id=agent_a_id, session_id=session_a_id, model_version="v2.0"),
        store,
    )
    await handle_start_agent_session(
        StartAgentSessionCommand(agent_id=agent_b_id, session_id=session_b_id, model_version="v2.0"),
        store,
    )
    await handle_submit_application(
        SubmitApplicationCommand(application_id=app_id, applicant_id="alice", requested_amount_usd=50_000.0),
        store,
    )
    await handle_request_credit_analysis(
        RequestCreditAnalysisCommand(application_id=app_id, assigned_agent_id=agent_a_id),
        store,
    )

    async def run_agent(agent_id: str, session_id: str) -> str:
        cmd = CreditAnalysisCompletedCommand(
            application_id=app_id,
            agent_id=agent_id,
            session_id=session_id,
            model_version="v2.0",
            confidence_score=0.85,
            risk_tier="LOW",
            recommended_limit_usd=50_000.0,
            duration_ms=100,
        )
        try:
            await handle_credit_analysis_completed(cmd, store)
            return "success"
        except OptimisticConcurrencyError:
            return "conflict"

    results = await asyncio.gather(
        run_agent(agent_a_id, session_a_id),
        run_agent(agent_b_id, session_b_id),
    )

    # 1. Exactly one winner, one loser
    assert sorted(results) == ["conflict", "success"], (
        f"Expected one success and one conflict, got: {results}"
    )

    # 2. Loan stream has exactly one CreditAnalysisCompleted
    loan_events = await store.load_stream(f"loan-{app_id}")
    credit_events = [e for e in loan_events if e.event_type == "CreditAnalysisCompleted"]
    assert len(credit_events) == 1, (
        f"Loan stream must have exactly one CreditAnalysisCompleted, got {len(credit_events)}"
    )

    # 3. Loan stream is at version 3 (Submitted + CreditAnalysisRequested + CreditAnalysisCompleted)
    assert len(loan_events) == 3

    # 4. The winning agent's session stream contains CreditAnalysisCompleted
    winning_agent_id = credit_events[0].payload["agent_id"]
    winning_session_id = credit_events[0].payload["session_id"]
    session_events = await store.load_stream(f"agent-{winning_agent_id}-{winning_session_id}")
    session_types = [e.event_type for e in session_events]
    assert "CreditAnalysisCompleted" in session_types
