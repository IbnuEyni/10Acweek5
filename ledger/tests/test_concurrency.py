"""
Week Standard — Double-Decision Concurrency Test
=================================================
Scenario: A loan application stream has 3 prior events (version 3).
Two AI agents simultaneously attempt to append a CreditAnalysisCompleted
event at expected_version=3.

Guarantee under test:
  - SELECT ... FOR UPDATE in append() serialises the two transactions.
  - Exactly one commits  → stream length becomes 4.
  - The other reads current_version=4 after the lock is released,
    fails the version check, and raises OptimisticConcurrencyError.
  - Final stream length is 4, never 5.
"""

import asyncio
import uuid
import pytest

from src.event_store import EventStore, NewEvent, OptimisticConcurrencyError

pytestmark = pytest.mark.asyncio

AGGREGATE_TYPE = "LoanApplication"
STREAM_VERSION_BEFORE_RACE = 3  # three events already in the stream


async def _seed_stream(store: EventStore, stream_id: uuid.UUID) -> None:
    """Append 3 prior events so the stream sits at version 3 before the race."""
    prior_events = [
        NewEvent("ApplicationSubmitted", {"applicant": "Alice"}),
        NewEvent("DocumentsVerified",    {"verified_by": "system"}),
        NewEvent("RiskScoreCalculated",  {"score": 720}),
    ]
    await store.append(
        stream_id,
        AGGREGATE_TYPE,
        prior_events,
        expected_version=0,
    )


async def test_double_decision_credit_analysis_at_version_3(pool, stream_id):
    """
    Two AI agents race to append CreditAnalysisCompleted at expected_version=3.

    Assertions
    ----------
    1. Exactly one agent succeeds ("success") and one gets a conflict ("conflict").
    2. Final stream contains exactly 4 events — the 3 seeded + 1 winner's event.
    3. The winning event is CreditAnalysisCompleted.
    4. The losing agent's event is NOT in the stream.
    """
    store = EventStore(pool)
    await _seed_stream(store, stream_id)

    async def agent(name: str) -> str:
        try:
            await store.append(
                stream_id,
                AGGREGATE_TYPE,
                [NewEvent("CreditAnalysisCompleted", {"decision": "approved", "agent": name})],
                expected_version=STREAM_VERSION_BEFORE_RACE,
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

    # --- assertion 3: the 4th event is CreditAnalysisCompleted ---
    assert events[3].event_type == "CreditAnalysisCompleted"
    assert events[3].stream_position == 4

    # --- assertion 4: only one agent's decision is recorded ---
    decisions = [e.payload["agent"] for e in events if e.event_type == "CreditAnalysisCompleted"]
    assert len(decisions) == 1, "Exactly one CreditAnalysisCompleted must be in the stream"
