"""
Phase 6 — What-If Projector Tests

Covers:
- The specific brief scenario: risk_tier MEDIUM → HIGH produces a materially
  different ApplicationSummary outcome (different decision cascade).
- Causal dependency: semantically dependent events are skipped in the
  counterfactual timeline.
- Independent events are preserved in the counterfactual timeline.
- Counterfactual events are never written to the real store.
- Branch point not found: counterfactual appended at end.
- WhatIfResult fields: divergence_events, skipped_real_events,
  counterfactual_events_injected.
"""
import uuid
import pytest

from src.event_store import EventStore, NewEvent
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.whatif.projector import run_what_if, WhatIfResult

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _build_full_lifecycle(
    store: EventStore,
    app_id: str,
    agent_id: str,
    session_id: str,
    risk_tier: str = "MEDIUM",
    confidence_score: float = 0.82,
) -> None:
    """
    Build a complete loan lifecycle on the loan stream.
    risk_tier and confidence_score are parameterised so we can vary them
    for the counterfactual scenario.
    """
    stream_id = f"loan-{app_id}"

    await store.append(stream_id, [NewEvent("ApplicationSubmitted", {
        "application_id": app_id,
        "applicant_id": "applicant-001",
        "requested_amount_usd": 100000.0,
        "loan_purpose": "business_expansion",
    })], expected_version=0, aggregate_type="LoanApplication")

    await store.append(stream_id, [NewEvent("CreditAnalysisRequested", {
        "application_id": app_id,
        "assigned_agent_id": agent_id,
    })], expected_version=1, aggregate_type="LoanApplication")

    await store.append(stream_id, [NewEvent("CreditAnalysisCompleted", {
        "application_id": app_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "model_version": "v2.3",
        "confidence_score": confidence_score,
        "risk_tier": risk_tier,
        "recommended_limit_usd": 90000.0,
        "analysis_duration_ms": 950,
        "input_data_hash": "hash-001",
    })], expected_version=2, aggregate_type="LoanApplication")

    await store.append(stream_id, [NewEvent("ComplianceCheckRequested", {
        "application_id": app_id,
        "regulation_set_version": "v2024",
        "checks_required": ["KYC", "AML"],
    })], expected_version=3, aggregate_type="LoanApplication")

    await store.append(stream_id, [NewEvent("ComplianceRulePassed", {
        "application_id": app_id,
        "rule_id": "KYC",
        "rule_version": "v2024.1",
        "evidence_hash": "kyc-hash",
    })], expected_version=4, aggregate_type="LoanApplication")

    await store.append(stream_id, [NewEvent("ComplianceRulePassed", {
        "application_id": app_id,
        "rule_id": "AML",
        "rule_version": "v2024.1",
        "evidence_hash": "aml-hash",
    })], expected_version=5, aggregate_type="LoanApplication")

    session_stream = f"agent-{agent_id}-{session_id}"
    await store.append(stream_id, [NewEvent("DecisionGenerated", {
        "application_id": app_id,
        "orchestrator_agent_id": agent_id,
        "recommendation": "APPROVE",
        "confidence_score": confidence_score,
        "contributing_agent_sessions": [session_stream],
        "decision_basis_summary": "All checks passed",
        "model_versions": {session_stream: "v2.3"},
        "forced_refer": False,
    })], expected_version=6, aggregate_type="LoanApplication")

    await store.append(stream_id, [NewEvent("HumanReviewCompleted", {
        "application_id": app_id,
        "reviewer_id": "reviewer-001",
        "final_decision": "APPROVE",
        "override": False,
        "override_reason": "",
    })], expected_version=7, aggregate_type="LoanApplication")

    await store.append(stream_id, [NewEvent("ApplicationApproved", {
        "application_id": app_id,
        "approved_amount_usd": 90000.0,
        "interest_rate": 0.045,
        "conditions": [],
        "approved_by": "reviewer-001",
        "effective_date": "2026-01-01",
    })], expected_version=8, aggregate_type="LoanApplication")


# ---------------------------------------------------------------------------
# The specific brief scenario: MEDIUM → HIGH risk tier
# ---------------------------------------------------------------------------

async def test_what_if_high_risk_produces_different_outcome(store):
    """
    Brief scenario: 'What would the final decision have been if the credit
    analysis had returned risk_tier=HIGH instead of MEDIUM?'

    Real timeline:    risk_tier=MEDIUM → APPROVE
    Counterfactual:   risk_tier=HIGH   → DecisionGenerated and downstream
                      events are skipped (causally dependent on the analysis)

    The counterfactual ApplicationSummary must show a materially different
    state — specifically, it must NOT reach FinalApproved because the
    downstream DecisionGenerated/HumanReviewCompleted/ApplicationApproved
    events are causally dependent on the CreditAnalysisCompleted and are
    skipped in the counterfactual timeline.
    """
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    await _build_full_lifecycle(store, app_id, agent_id, session_id, risk_tier="MEDIUM")

    # Counterfactual: same event but risk_tier=HIGH
    counterfactual = [NewEvent("CreditAnalysisCompleted", {
        "application_id": app_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "model_version": "v2.3",
        "confidence_score": 0.82,
        "risk_tier": "HIGH",           # ← the counterfactual change
        "recommended_limit_usd": 0.0,  # HIGH risk → zero limit
        "analysis_duration_ms": 950,
        "input_data_hash": "hash-001",
    })]

    projections = [ApplicationSummaryProjection()]
    result = await run_what_if(
        store,
        application_id=app_id,
        branch_at_event_type="CreditAnalysisCompleted",
        counterfactual_events=counterfactual,
        projections=projections,
    )

    assert isinstance(result, WhatIfResult)
    assert result.branch_point == "CreditAnalysisCompleted"

    # Real outcome: FinalApproved
    real_state = result.real_outcome.get("ApplicationSummary", {})
    assert real_state is not None
    assert real_state.get("state") == "FinalApproved", (
        f"Real timeline should reach FinalApproved, got: {real_state.get('state')}"
    )

    # Counterfactual outcome: must NOT be FinalApproved
    # The downstream events (DecisionGenerated, HumanReviewCompleted, ApplicationApproved)
    # are semantically dependent on CreditAnalysisCompleted and are skipped.
    cf_state = result.counterfactual_outcome.get("ApplicationSummary", {})
    assert cf_state is not None
    assert cf_state.get("state") != "FinalApproved", (
        f"Counterfactual with HIGH risk should NOT reach FinalApproved, "
        f"got: {cf_state.get('state')}"
    )

    # The risk_tier in the counterfactual must be HIGH
    assert cf_state.get("risk_tier") == "HIGH", (
        f"Counterfactual risk_tier should be HIGH, got: {cf_state.get('risk_tier')}"
    )

    # Divergence must be non-empty — the timelines materially differ
    assert len(result.divergence_events) > 0, "Timelines must diverge"

    # Downstream events must be listed as skipped
    assert len(result.skipped_real_events) > 0
    assert "DecisionGenerated" in result.skipped_real_events
    assert "ApplicationApproved" in result.skipped_real_events

    # Counterfactual event must be listed as injected
    assert "CreditAnalysisCompleted" in result.counterfactual_events_injected


async def test_what_if_never_writes_to_real_store(store):
    """
    Counterfactual events must NEVER appear in the real event store.
    After run_what_if(), the loan stream must be identical to before.
    """
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    await _build_full_lifecycle(store, app_id, agent_id, session_id)

    events_before = await store.load_stream(f"loan-{app_id}")
    version_before = len(events_before)

    counterfactual = [NewEvent("CreditAnalysisCompleted", {
        "application_id": app_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "model_version": "v2.3",
        "confidence_score": 0.82,
        "risk_tier": "HIGH",
        "recommended_limit_usd": 0.0,
        "analysis_duration_ms": 950,
        "input_data_hash": "hash-counterfactual",
    })]

    await run_what_if(
        store,
        application_id=app_id,
        branch_at_event_type="CreditAnalysisCompleted",
        counterfactual_events=counterfactual,
        projections=[ApplicationSummaryProjection()],
    )

    events_after = await store.load_stream(f"loan-{app_id}")
    assert len(events_after) == version_before, (
        "run_what_if must not write any events to the real store"
    )
    # Verify no counterfactual payload leaked into the store
    for ev in events_after:
        if ev.event_type == "CreditAnalysisCompleted":
            assert ev.payload.get("input_data_hash") != "hash-counterfactual", (
                "Counterfactual event payload must not appear in the real store"
            )


async def test_what_if_skips_semantically_dependent_events(store):
    """
    Events semantically downstream of the branch point are excluded from
    the counterfactual timeline and listed in skipped_real_events.
    """
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    await _build_full_lifecycle(store, app_id, agent_id, session_id)

    result = await run_what_if(
        store,
        application_id=app_id,
        branch_at_event_type="CreditAnalysisCompleted",
        counterfactual_events=[NewEvent("CreditAnalysisCompleted", {
            "application_id": app_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "model_version": "v2.3",
            "confidence_score": 0.55,  # below confidence floor → REFER
            "risk_tier": "HIGH",
            "recommended_limit_usd": 0.0,
            "analysis_duration_ms": 950,
            "input_data_hash": "hash-cf",
        })],
        projections=[ApplicationSummaryProjection()],
    )

    # All semantically dependent event types must be skipped
    for dependent_type in ["DecisionGenerated", "HumanReviewCompleted", "ApplicationApproved"]:
        assert dependent_type in result.skipped_real_events, (
            f"{dependent_type} should be in skipped_real_events"
        )


async def test_what_if_preserves_independent_events(store):
    """
    Events that are causally independent of the branch point must be
    preserved in the counterfactual timeline.
    ComplianceRulePassed events are independent of CreditAnalysisCompleted.
    """
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    await _build_full_lifecycle(store, app_id, agent_id, session_id)

    result = await run_what_if(
        store,
        application_id=app_id,
        branch_at_event_type="CreditAnalysisCompleted",
        counterfactual_events=[NewEvent("CreditAnalysisCompleted", {
            "application_id": app_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "model_version": "v2.3",
            "confidence_score": 0.82,
            "risk_tier": "HIGH",
            "recommended_limit_usd": 0.0,
            "analysis_duration_ms": 950,
            "input_data_hash": "hash-cf",
        })],
        projections=[ApplicationSummaryProjection()],
    )

    # ComplianceRulePassed is independent — must NOT be in skipped_real_events
    assert "ComplianceRulePassed" not in result.skipped_real_events, (
        "ComplianceRulePassed is causally independent and must not be skipped"
    )
    assert "ComplianceCheckRequested" not in result.skipped_real_events


async def test_what_if_branch_not_found_uses_full_real_sequence(store):
    """
    If branch_at_event_type is not in the stream, the real sequence is used
    as-is and counterfactual events are appended at the end.
    """
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    await _build_full_lifecycle(store, app_id, agent_id, session_id)

    result = await run_what_if(
        store,
        application_id=app_id,
        branch_at_event_type="NonExistentEventType",
        counterfactual_events=[NewEvent("HypotheticalEvent", {
            "application_id": app_id,
            "note": "this event type does not exist in the real stream",
        })],
        projections=[ApplicationSummaryProjection()],
    )

    # No events should be skipped — branch was not found
    assert result.skipped_real_events == []
    # The injected event should be listed
    assert "HypotheticalEvent" in result.counterfactual_events_injected


async def test_what_if_result_fields_complete(store):
    """WhatIfResult must have all required fields populated."""
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    await _build_full_lifecycle(store, app_id, agent_id, session_id)

    result = await run_what_if(
        store,
        application_id=app_id,
        branch_at_event_type="CreditAnalysisCompleted",
        counterfactual_events=[NewEvent("CreditAnalysisCompleted", {
            "application_id": app_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "model_version": "v2.3",
            "confidence_score": 0.82,
            "risk_tier": "HIGH",
            "recommended_limit_usd": 0.0,
            "analysis_duration_ms": 950,
            "input_data_hash": "hash-cf",
        })],
        projections=[ApplicationSummaryProjection()],
    )

    assert result.branch_point == "CreditAnalysisCompleted"
    assert isinstance(result.real_outcome, dict)
    assert isinstance(result.counterfactual_outcome, dict)
    assert isinstance(result.divergence_events, list)
    assert isinstance(result.skipped_real_events, list)
    assert isinstance(result.counterfactual_events_injected, list)
    assert "ApplicationSummary" in result.real_outcome
    assert "ApplicationSummary" in result.counterfactual_outcome
