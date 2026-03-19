"""
tests/test_aggregates.py
========================
Domain invariant tests for LoanApplicationAggregate and AgentSessionAggregate.

Week Standard assertions
------------------------
1. State Machine  — DomainError raised when transitioning Approved → UnderReview
2. Confidence Floor — DecisionGenerated with score=0.5 is forced to 'REFER'
3. Gas Town       — AgentSession raises DomainError if first event is not AgentContextLoaded
"""

import uuid
import pytest

from src.aggregates import (
    DomainError,
    LoanApplicationAggregate,
    AgentSessionAggregate,
)

pytestmark = pytest.mark.asyncio


# ===========================================================================
# LoanApplicationAggregate — State Machine
# ===========================================================================

async def test_state_machine_approved_to_under_review_raises_domain_error(store, stream_id):
    """
    Week Standard: transitioning from 'Approved' back to 'UnderReview'
    must raise DomainError. Approved is a forward-only state.
    """
    loan = await LoanApplicationAggregate.submit(
        stream_id, store, applicant_id="alice", amount=50_000.0
    )
    await loan.approve(store, approved_by="underwriter-1")
    assert loan.status == "Approved"

    with pytest.raises(DomainError, match="Approved"):
        loan._transition("UnderReview", "ApplicationSubmitted", {})


async def test_state_machine_happy_path(store, stream_id):
    """Full forward lifecycle: New → UnderReview → Approved → Disbursed."""
    loan = await LoanApplicationAggregate.submit(
        stream_id, store, applicant_id="bob", amount=20_000.0
    )
    assert loan.status == "UnderReview"

    await loan.approve(store, approved_by="underwriter-2")
    assert loan.status == "Approved"

    await loan.disburse(store, amount=20_000.0)
    assert loan.status == "Disbursed"


async def test_state_machine_rejected_is_terminal(store, stream_id):
    """Rejected is terminal — any further transition raises DomainError."""
    loan = await LoanApplicationAggregate.submit(
        stream_id, store, applicant_id="carol", amount=5_000.0
    )
    await loan.reject(store, reason="fraud risk")

    with pytest.raises(DomainError, match="Rejected"):
        await loan.approve(store, approved_by="underwriter-3")


async def test_state_machine_load_rebuilds_and_enforces_invariant(store, stream_id):
    """
    Load-Validate-Append: reload from the store; the rebuilt aggregate
    must still enforce the state machine invariant.
    """
    loan = await LoanApplicationAggregate.submit(
        stream_id, store, applicant_id="dave", amount=30_000.0
    )
    await loan.approve(store, approved_by="underwriter-4")

    reloaded = await LoanApplicationAggregate.load(stream_id, store)
    assert reloaded.status == "Approved"
    assert reloaded.version == 2

    with pytest.raises(DomainError):
        reloaded._transition("UnderReview", "ApplicationSubmitted", {})


async def test_state_machine_version_increments_per_event(store, stream_id):
    loan = await LoanApplicationAggregate.submit(
        stream_id, store, applicant_id="eve", amount=10_000.0
    )
    assert loan.version == 1
    await loan.approve(store, approved_by="underwriter-5")
    assert loan.version == 2
    await loan.disburse(store, amount=10_000.0)
    assert loan.version == 3


# ===========================================================================
# AgentSessionAggregate — Confidence Floor
# ===========================================================================

async def test_confidence_floor_score_0_5_forces_refer(store, stream_id):
    """
    Week Standard: a DecisionGenerated event with confidence_score=0.5
    must be automatically forced to recommendation='REFER'.
    """
    session = await AgentSessionAggregate.open(
        stream_id, store, agent_id="agent-1", context={"loan_id": "L001"}
    )
    await session.record_decision(
        store,
        recommendation="APPROVE",   # caller intent
        confidence_score=0.5,        # below floor → must become REFER
    )

    events = await store.load_stream(stream_id)
    decision = next(e for e in events if e.event_type == "DecisionGenerated")
    assert decision.payload["recommendation"] == "REFER"
    assert decision.payload["forced_refer"] is True


async def test_confidence_floor_score_above_threshold_preserved(store, stream_id):
    """Score >= 0.6 must not be overridden."""
    session = await AgentSessionAggregate.open(
        stream_id, store, agent_id="agent-2", context={"loan_id": "L002"}
    )
    await session.record_decision(
        store,
        recommendation="APPROVE",
        confidence_score=0.85,
    )

    events = await store.load_stream(stream_id)
    decision = next(e for e in events if e.event_type == "DecisionGenerated")
    assert decision.payload["recommendation"] == "APPROVE"
    assert decision.payload["forced_refer"] is False


async def test_confidence_floor_exactly_at_threshold_not_overridden(store, stream_id):
    """Score exactly at 0.6 (the floor) must NOT be overridden."""
    session = await AgentSessionAggregate.open(
        stream_id, store, agent_id="agent-3", context={"loan_id": "L003"}
    )
    await session.record_decision(
        store,
        recommendation="REJECT",
        confidence_score=0.6,
    )

    events = await store.load_stream(stream_id)
    decision = next(e for e in events if e.event_type == "DecisionGenerated")
    assert decision.payload["recommendation"] == "REJECT"
    assert decision.payload["forced_refer"] is False


# ===========================================================================
# AgentSessionAggregate — Gas Town
# ===========================================================================

async def test_gas_town_decision_without_context_raises_domain_error(stream_id):
    """
    Week Standard: AgentSession raises DomainError if a decision is attempted
    before AgentContextLoaded has been recorded as the first event.
    """
    session = AgentSessionAggregate(stream_id)  # raw — open() never called

    with pytest.raises(DomainError, match="AgentContextLoaded"):
        await session.record_decision(
            store=None,              # never reached
            recommendation="APPROVE",
            confidence_score=0.9,
        )


async def test_gas_town_close_without_context_raises_domain_error(stream_id):
    """Gas Town applies to close() as well — no context, no action."""
    session = AgentSessionAggregate(stream_id)

    with pytest.raises(DomainError):
        await session.close(store=None)


async def test_gas_town_happy_path_context_then_decision(store, stream_id):
    """After AgentContextLoaded, decisions are accepted normally."""
    session = await AgentSessionAggregate.open(
        stream_id, store, agent_id="agent-4", context={"loan_id": "L004"}
    )
    assert session.context_loaded is True

    await session.record_decision(
        store, recommendation="APPROVE", confidence_score=0.9
    )
    await session.close(store)
    assert session.closed is True


async def test_gas_town_closed_session_rejects_further_decisions(store, stream_id):
    """A closed session must raise DomainError on any further decision."""
    session = await AgentSessionAggregate.open(
        stream_id, store, agent_id="agent-5", context={"loan_id": "L005"}
    )
    await session.close(store)

    with pytest.raises(DomainError, match="closed"):
        await session.record_decision(
            store, recommendation="APPROVE", confidence_score=0.9
        )


async def test_gas_town_load_rebuilds_and_enforces_invariant(store, stream_id):
    """
    Load-Validate-Append: reload a closed session; Gas Town invariant
    must still be enforced on the rebuilt instance.
    """
    session = await AgentSessionAggregate.open(
        stream_id, store, agent_id="agent-6", context={"loan_id": "L006"}
    )
    await session.close(store)

    reloaded = await AgentSessionAggregate.load(stream_id, store)
    assert reloaded.context_loaded is True
    assert reloaded.closed is True
    assert reloaded.version == 2

    with pytest.raises(DomainError, match="closed"):
        await reloaded.record_decision(
            store, recommendation="APPROVE", confidence_score=0.9
        )
