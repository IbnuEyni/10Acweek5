"""
tests/test_aggregates.py
========================
Domain invariant tests — all 6 business rules from the brief.

Rule 1  State machine          — strict forward-only transitions
Rule 2  Gas Town               — AgentContextLoaded must be first event
Rule 3  Model version locking  — no duplicate credit analysis unless superseded
Rule 4  Confidence floor       — score < 0.6 forces REFER
Rule 5  Compliance dependency  — approval blocked until all checks pass
Rule 6  Causal chain           — contributing sessions must have processed the app
"""

import pytest

from src.aggregates import (
    ApplicationState,
    DomainError,
    LoanApplicationAggregate,
    AgentSessionAggregate,
    ComplianceRecordAggregate,
    AuditLedgerAggregate,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers — build aggregates to a known state without repeating boilerplate
# ---------------------------------------------------------------------------

async def _submitted_loan(store, stream_id, applicant_id="alice", amount=50_000.0):
    return await LoanApplicationAggregate.submit(
        stream_id, store, applicant_id=applicant_id, requested_amount_usd=amount
    )


async def _loan_awaiting_analysis(store, stream_id):
    loan = await _submitted_loan(store, stream_id)
    await loan.request_credit_analysis(store, assigned_agent_id="agent-1")
    return loan


def _parse_session_ids(session_stream_id: str) -> tuple[str, str]:
    """Extract agent_id and session_id from 'agent-{agent_id}-{session_id}'."""
    parts = session_stream_id.removeprefix("agent-").split("-", 1)
    return parts[0], parts[1] if len(parts) > 1 else "s1"


async def _open_session(store, session_stream_id, model_version="v2.0"):
    agent_id, session_id = _parse_session_ids(session_stream_id)
    return await AgentSessionAggregate.open(
        session_stream_id, store,
        agent_id=agent_id, model_version=model_version, session_id=session_id
    )


# ===========================================================================
# Rule 1 — State Machine
# ===========================================================================

async def test_rule1_submitted_state_on_creation(store, stream_id):
    loan = await _submitted_loan(store, stream_id)
    assert loan.state == ApplicationState.SUBMITTED


async def test_rule1_submitted_to_awaiting_analysis(store, stream_id):
    loan = await _submitted_loan(store, stream_id)
    await loan.request_credit_analysis(store, assigned_agent_id="agent-1")
    assert loan.state == ApplicationState.AWAITING_ANALYSIS


async def test_rule1_illegal_transition_raises_domain_error(store, stream_id):
    """Cannot skip AwaitingAnalysis — Submitted → AnalysisComplete is illegal."""
    loan = await _submitted_loan(store, stream_id)
    with pytest.raises(DomainError, match="Submitted"):
        loan._transition(ApplicationState.ANALYSIS_COMPLETE, "Bad", {})


async def test_rule1_terminal_states_reject_all_transitions(store, stream_id, session_stream_id):
    """FinalApproved and FinalDeclined are terminal — no further transitions."""
    agent_id, session_id = _parse_session_ids(session_stream_id)
    loan = await _loan_awaiting_analysis(store, stream_id)
    session = await _open_session(store, session_stream_id)

    # Record credit analysis through the session so it registers as a contributing session
    await session.record_credit_analysis(
        store, application_id=loan.application_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.record_credit_analysis_completed(
        store, agent_id=agent_id, session_id=session_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.request_compliance_review(store, "reg-v1", ["KYC"])
    await loan.record_compliance_passed(store, "KYC", "reg-v1", "hash1")
    await loan.generate_decision(
        store, "orch-1", "APPROVE", 0.9,
        [session_stream_id], "all good", {}
    )
    await loan.complete_human_review(store, "reviewer-1", "APPROVE")
    await loan.approve(store, 50_000.0, 0.05, [], "reviewer-1", "2026-01-01")

    assert loan.state == ApplicationState.FINAL_APPROVED
    with pytest.raises(DomainError):
        loan._transition(ApplicationState.SUBMITTED, "Bad", {})


async def test_rule1_load_rebuilds_full_state_machine(store, stream_id):
    """Reload from store — rebuilt aggregate must enforce state machine."""
    loan = await _submitted_loan(store, stream_id)
    await loan.request_credit_analysis(store, assigned_agent_id="a1")

    reloaded = await LoanApplicationAggregate.load(store, loan.application_id)
    assert reloaded.state == ApplicationState.AWAITING_ANALYSIS
    assert reloaded.version == 2

    with pytest.raises(DomainError):
        reloaded._transition(ApplicationState.SUBMITTED, "Bad", {})


async def test_rule1_version_increments_per_event(store, stream_id):
    loan = await _submitted_loan(store, stream_id)
    assert loan.version == 1
    await loan.request_credit_analysis(store, assigned_agent_id="a1")
    assert loan.version == 2


# ===========================================================================
# Rule 2 — Gas Town (AgentContextLoaded must be first event)
# ===========================================================================

async def test_rule2_decision_without_context_raises(session_stream_id):
    """Raw session — open() never called — must reject any decision."""
    session = AgentSessionAggregate(session_stream_id)
    with pytest.raises(DomainError, match="AgentContextLoaded"):
        await session.record_decision(store=None, recommendation="APPROVE", confidence_score=0.9)


async def test_rule2_fraud_screening_without_context_raises(session_stream_id):
    session = AgentSessionAggregate(session_stream_id)
    with pytest.raises(DomainError, match="AgentContextLoaded"):
        await session.record_fraud_screening(
            store=None, application_id="app-1", fraud_score=0.1,
            anomaly_flags=[], screening_model_version="v1", input_data_hash="h"
        )


async def test_rule2_close_without_context_raises(session_stream_id):
    session = AgentSessionAggregate(session_stream_id)
    with pytest.raises(DomainError):
        await session.close(store=None)


async def test_rule2_context_loaded_enables_decisions(store, session_stream_id):
    session = await _open_session(store, session_stream_id)
    assert session.context_loaded is True
    await session.record_decision(store, recommendation="APPROVE", confidence_score=0.9)
    await session.close(store)
    assert session.closed is True


async def test_rule2_closed_session_rejects_further_decisions(store, session_stream_id):
    session = await _open_session(store, session_stream_id)
    await session.close(store)
    with pytest.raises(DomainError, match="closed"):
        await session.record_decision(store, recommendation="APPROVE", confidence_score=0.9)


async def test_rule2_load_rebuilds_and_enforces_gas_town(store, session_stream_id):
    """Reload a closed session — Gas Town invariant must survive replay."""
    agent_id, session_id = _parse_session_ids(session_stream_id)
    session = await _open_session(store, session_stream_id)
    await session.close(store)

    reloaded = await AgentSessionAggregate.load(store, agent_id, session_id)
    assert reloaded.context_loaded is True
    assert reloaded.closed is True
    assert reloaded.version == 2

    with pytest.raises(DomainError, match="closed"):
        await reloaded.record_decision(store, recommendation="APPROVE", confidence_score=0.9)


async def test_rule2_context_source_and_model_version_stored(store, session_stream_id):
    """AgentContextLoaded payload must carry context_source and model_version."""
    agent_id, session_id = _parse_session_ids(session_stream_id)
    session = await AgentSessionAggregate.open(
        session_stream_id, store,
        agent_id=agent_id, model_version="v3.1",
        context_source="event_replay", event_replay_from_position=5,
        session_id=session_id
    )
    events = await store.load_stream(session_stream_id)
    ctx = events[0]
    assert ctx.event_type == "AgentContextLoaded"
    assert ctx.payload["model_version"] == "v3.1"
    assert ctx.payload["context_source"] == "event_replay"
    assert ctx.payload["event_replay_from_position"] == 5


# ===========================================================================
# Rule 3 — Model Version Locking
# ===========================================================================

async def test_rule3_duplicate_credit_analysis_raises(store, stream_id, session_stream_id):
    """Second credit analysis for same application on same session must raise."""
    loan = await _loan_awaiting_analysis(store, stream_id)
    session = await _open_session(store, session_stream_id)

    await session.record_credit_analysis(
        store, application_id=loan.application_id,
        model_version="v2.0", confidence_score=0.8, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h1"
    )
    # Reload to get the updated state with the lock in place
    agent_id, session_id = _parse_session_ids(session_stream_id)
    session = await AgentSessionAggregate.load(store, agent_id, session_id)

    with pytest.raises(DomainError, match="already has a credit analysis"):
        await session.record_credit_analysis(
            store, application_id=loan.application_id,
            model_version="v2.0", confidence_score=0.7, risk_tier="MEDIUM",
            recommended_limit_usd=40_000.0, analysis_duration_ms=120, input_data_hash="h2"
        )


async def test_rule3_model_version_mismatch_raises(store, session_stream_id):
    """Session opened with v2.0 must reject a command claiming v3.0."""
    session = await _open_session(store, session_stream_id)
    with pytest.raises(DomainError, match="model_version"):
        session.assert_model_version_current("v3.0")


async def test_rule3_model_version_match_passes(store, session_stream_id):
    session = await _open_session(store, session_stream_id)
    session.assert_model_version_current("v2.0")  # must not raise


async def test_rule3_supersede_unlocks_reanalysis(store, session_stream_id):
    """After HumanReviewOverride is replayed, re-analysis is allowed."""
    agent_id, session_id = _parse_session_ids(session_stream_id)
    session = await _open_session(store, session_stream_id)
    await session.record_credit_analysis(
        store, application_id="app-X",
        model_version="v2.0", confidence_score=0.8, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h1"
    )
    # Simulate override by directly appending the event to the store
    from src.event_store import NewEvent
    await store.append(
        stream_id=session_stream_id,
        events=[NewEvent("HumanReviewOverride", {"application_id": "app-X"})],
        expected_version=session.version,
    )
    # Reload — override must clear the lock
    reloaded = await AgentSessionAggregate.load(store, agent_id, session_id)
    assert "app-X" not in reloaded._credit_analyses


# ===========================================================================
# Rule 4 — Confidence Floor
# ===========================================================================

async def test_rule4_score_below_floor_forces_refer(store, session_stream_id):
    session = await _open_session(store, session_stream_id)
    await session.record_decision(store, recommendation="APPROVE", confidence_score=0.59)

    events = await store.load_stream(session_stream_id)
    decision = next(e for e in events if e.event_type == "DecisionGenerated")
    assert decision.payload["recommendation"] == "REFER"
    assert decision.payload["forced_refer"] is True


async def test_rule4_score_exactly_at_floor_not_overridden(store, session_stream_id):
    session = await _open_session(store, session_stream_id)
    await session.record_decision(store, recommendation="REJECT", confidence_score=0.6)

    events = await store.load_stream(session_stream_id)
    decision = next(e for e in events if e.event_type == "DecisionGenerated")
    assert decision.payload["recommendation"] == "REJECT"
    assert decision.payload["forced_refer"] is False


async def test_rule4_score_above_floor_preserved(store, session_stream_id):
    session = await _open_session(store, session_stream_id)
    await session.record_decision(store, recommendation="APPROVE", confidence_score=0.85)

    events = await store.load_stream(session_stream_id)
    decision = next(e for e in events if e.event_type == "DecisionGenerated")
    assert decision.payload["recommendation"] == "APPROVE"
    assert decision.payload["forced_refer"] is False


async def test_rule4_loan_generate_decision_enforces_floor(store, stream_id, session_stream_id):
    """Confidence floor also enforced in LoanApplicationAggregate.generate_decision."""
    agent_id, session_id = _parse_session_ids(session_stream_id)
    loan = await _loan_awaiting_analysis(store, stream_id)
    session = await _open_session(store, session_stream_id)

    await session.record_credit_analysis(
        store, application_id=loan.application_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.record_credit_analysis_completed(
        store, agent_id=agent_id, session_id=session_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.request_compliance_review(store, "reg-v1", ["KYC"])
    await loan.record_compliance_passed(store, "KYC", "reg-v1", "hash1")

    # confidence_score=0.4 → must be forced to REFER
    await loan.generate_decision(
        store, "orch-1", "APPROVE", 0.4,
        [session_stream_id], "low confidence", {}
    )

    events = await store.load_stream(loan.stream_id)
    decision = next(e for e in events if e.event_type == "DecisionGenerated")
    assert decision.payload["recommendation"] == "REFER"
    assert decision.payload["forced_refer"] is True


# ===========================================================================
# Rule 5 — Compliance Dependency
# ===========================================================================

async def test_rule5_approval_blocked_without_compliance(store, stream_id, session_stream_id):
    """ApplicationApproved cannot be appended if required checks are not all passed."""
    agent_id, session_id = _parse_session_ids(session_stream_id)
    loan = await _loan_awaiting_analysis(store, stream_id)
    session = await _open_session(store, session_stream_id)

    await session.record_credit_analysis(
        store, application_id=loan.application_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.record_credit_analysis_completed(
        store, agent_id=agent_id, session_id=session_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.request_compliance_review(store, "reg-v1", ["KYC", "AML"])
    # Only pass KYC — AML is still missing
    await loan.record_compliance_passed(store, "KYC", "reg-v1", "hash1")
    await loan.generate_decision(
        store, "orch-1", "APPROVE", 0.9,
        [session_stream_id], "good", {}
    )
    await loan.complete_human_review(store, "reviewer-1", "APPROVE")

    with pytest.raises(DomainError, match="AML"):
        await loan.approve(store, 50_000.0, 0.05, [], "reviewer-1", "2026-01-01")


async def test_rule5_approval_succeeds_when_all_checks_pass(store, stream_id, session_stream_id):
    agent_id, session_id = _parse_session_ids(session_stream_id)
    loan = await _loan_awaiting_analysis(store, stream_id)
    session = await _open_session(store, session_stream_id)

    await session.record_credit_analysis(
        store, application_id=loan.application_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.record_credit_analysis_completed(
        store, agent_id=agent_id, session_id=session_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.request_compliance_review(store, "reg-v1", ["KYC", "AML"])
    await loan.record_compliance_passed(store, "KYC", "reg-v1", "hash1")
    await loan.record_compliance_passed(store, "AML", "reg-v1", "hash2")
    await loan.generate_decision(
        store, "orch-1", "APPROVE", 0.9,
        [session_stream_id], "all clear", {}
    )
    await loan.complete_human_review(store, "reviewer-1", "APPROVE")
    await loan.approve(store, 50_000.0, 0.05, [], "reviewer-1", "2026-01-01")

    assert loan.state == ApplicationState.FINAL_APPROVED


async def test_rule5_compliance_record_aggregate_clearance_blocked(store, stream_id):
    """ComplianceRecordAggregate.issue_clearance blocked until all checks pass."""
    app_id = stream_id.removeprefix("loan-")
    compliance_stream = f"compliance-{app_id}"

    compliance = await ComplianceRecordAggregate.request_checks(
        compliance_stream, store, "reg-v1", ["KYC", "AML"]
    )
    await compliance.record_rule_passed(store, "KYC", "reg-v1", "h1")

    with pytest.raises(DomainError, match="AML"):
        await compliance.issue_clearance(store)


async def test_rule5_compliance_record_clearance_succeeds_all_passed(store, stream_id):
    app_id = stream_id.removeprefix("loan-")
    compliance_stream = f"compliance-{app_id}"

    compliance = await ComplianceRecordAggregate.request_checks(
        compliance_stream, store, "reg-v1", ["KYC", "AML"]
    )
    await compliance.record_rule_passed(store, "KYC", "reg-v1", "h1")
    await compliance.record_rule_passed(store, "AML", "reg-v1", "h2")
    await compliance.issue_clearance(store)

    assert compliance.clearance_issued is True


async def test_rule5_failed_check_overridden_by_pass(store, stream_id):
    """A rule that previously failed can be overridden by a subsequent pass."""
    app_id = stream_id.removeprefix("loan-")
    compliance_stream = f"compliance-{app_id}"

    compliance = await ComplianceRecordAggregate.request_checks(
        compliance_stream, store, "reg-v1", ["KYC"]
    )
    await compliance.record_rule_failed(store, "KYC", "reg-v1", "missing docs")
    assert "KYC" in compliance.failed_checks

    await compliance.record_rule_passed(store, "KYC", "reg-v1", "h1")
    assert "KYC" not in compliance.failed_checks
    assert "KYC" in compliance.passed_checks


# ===========================================================================
# Rule 6 — Causal Chain Enforcement
# ===========================================================================

async def test_rule6_invalid_session_reference_raises(store, stream_id, session_stream_id):
    """DecisionGenerated must reject sessions that never processed this application."""
    agent_id, session_id = _parse_session_ids(session_stream_id)
    loan = await _loan_awaiting_analysis(store, stream_id)
    session = await _open_session(store, session_stream_id)

    await session.record_credit_analysis(
        store, application_id=loan.application_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.record_credit_analysis_completed(
        store, agent_id=agent_id, session_id=session_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.request_compliance_review(store, "reg-v1", ["KYC"])
    await loan.record_compliance_passed(store, "KYC", "reg-v1", "h1")

    # Reference a session that never processed this application
    ghost_session = "agent-ghost-999"
    with pytest.raises(DomainError, match="ghost"):
        await loan.generate_decision(
            store, "orch-1", "APPROVE", 0.9,
            [ghost_session], "bad reference", {}
        )


async def test_rule6_valid_session_reference_accepted(store, stream_id, session_stream_id):
    """Session that processed the application is a valid contributing session."""
    agent_id, session_id = _parse_session_ids(session_stream_id)
    loan = await _loan_awaiting_analysis(store, stream_id)
    session = await _open_session(store, session_stream_id)

    await session.record_credit_analysis(
        store, application_id=loan.application_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.record_credit_analysis_completed(
        store, agent_id=agent_id, session_id=session_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.request_compliance_review(store, "reg-v1", ["KYC"])
    await loan.record_compliance_passed(store, "KYC", "reg-v1", "h1")

    # session_stream_id was registered when credit analysis was recorded
    await loan.generate_decision(
        store, "orch-1", "APPROVE", 0.9,
        [session_stream_id], "valid", {}
    )
    assert loan.state == ApplicationState.PENDING_DECISION


async def test_rule6_empty_contributing_sessions_accepted(store, stream_id, session_stream_id):
    """Empty contributing_sessions list is valid (no sessions to validate)."""
    agent_id, session_id = _parse_session_ids(session_stream_id)
    loan = await _loan_awaiting_analysis(store, stream_id)
    session = await _open_session(store, session_stream_id)

    await session.record_credit_analysis(
        store, application_id=loan.application_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.record_credit_analysis_completed(
        store, agent_id=agent_id, session_id=session_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.request_compliance_review(store, "reg-v1", ["KYC"])
    await loan.record_compliance_passed(store, "KYC", "reg-v1", "h1")

    await loan.generate_decision(
        store, "orch-1", "APPROVE", 0.9, [], "orchestrator only", {}
    )
    assert loan.state == ApplicationState.PENDING_DECISION


# ===========================================================================
# AuditLedgerAggregate
# ===========================================================================

async def test_audit_ledger_records_entries(store, stream_id):
    app_id = stream_id.removeprefix("loan-")
    audit = AuditLedgerAggregate(f"audit-loan-{app_id}")
    await store.append(
        stream_id=audit.stream_id,
        events=[], expected_version=-1,
        aggregate_type="AuditLedger"
    )
    # Manually create the stream first via a real event
    from src.event_store import NewEvent
    await store.append(
        stream_id=audit.stream_id,
        events=[NewEvent("AuditEntryRecorded", {
            "entity_type": "loan", "entity_id": app_id,
            "source_stream_id": stream_id, "event_type": "ApplicationSubmitted",
            "summary": "Application submitted"
        })],
        expected_version=0,
    )
    reloaded = await AuditLedgerAggregate.load(store, "loan", app_id)
    assert stream_id in reloaded.linked_stream_ids


async def test_audit_ledger_integrity_check_stored(store, stream_id):
    app_id = stream_id.removeprefix("loan-")
    audit_stream = f"audit-loan-{app_id}"
    from src.event_store import NewEvent

    await store.append(
        stream_id=audit_stream,
        events=[NewEvent("AuditIntegrityCheckRun", {
            "entity_id": app_id,
            "events_verified_count": 5,
            "integrity_hash": "abc123",
            "previous_hash": None,
            "chain_valid": True,
        })],
        expected_version=-1,
        aggregate_type="AuditLedger",
    )
    reloaded = await AuditLedgerAggregate.load(store, "loan", app_id)
    assert reloaded.last_integrity_hash == "abc123"
    assert reloaded.events_verified_count == 5


# ===========================================================================
# Human Review — override requires reason
# ===========================================================================

async def test_human_review_override_requires_reason(store, stream_id, session_stream_id):
    agent_id, session_id = _parse_session_ids(session_stream_id)
    loan = await _loan_awaiting_analysis(store, stream_id)
    session = await _open_session(store, session_stream_id)

    await session.record_credit_analysis(
        store, application_id=loan.application_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.record_credit_analysis_completed(
        store, agent_id=agent_id, session_id=session_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.request_compliance_review(store, "reg-v1", ["KYC"])
    await loan.record_compliance_passed(store, "KYC", "reg-v1", "h1")
    await loan.generate_decision(
        store, "orch-1", "APPROVE", 0.9, [session_stream_id], "good", {}
    )

    with pytest.raises(DomainError, match="override_reason"):
        await loan.complete_human_review(
            store, "reviewer-1", "APPROVE", override=True, override_reason=""
        )


async def test_human_review_override_with_reason_succeeds(store, stream_id, session_stream_id):
    agent_id, session_id = _parse_session_ids(session_stream_id)
    loan = await _loan_awaiting_analysis(store, stream_id)
    session = await _open_session(store, session_stream_id)

    await session.record_credit_analysis(
        store, application_id=loan.application_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.record_credit_analysis_completed(
        store, agent_id=agent_id, session_id=session_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
        recommended_limit_usd=50_000.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.request_compliance_review(store, "reg-v1", ["KYC"])
    await loan.record_compliance_passed(store, "KYC", "reg-v1", "h1")
    await loan.generate_decision(
        store, "orch-1", "APPROVE", 0.9, [session_stream_id], "good", {}
    )
    await loan.complete_human_review(
        store, "reviewer-1", "APPROVE", override=True,
        override_reason="Senior approval granted"
    )
    assert loan.state == ApplicationState.APPROVED_PENDING_HUMAN


# ===========================================================================
# Decline path
# ===========================================================================

async def test_decline_path_final_declined(store, stream_id, session_stream_id):
    agent_id, session_id = _parse_session_ids(session_stream_id)
    loan = await _loan_awaiting_analysis(store, stream_id)
    session = await _open_session(store, session_stream_id)

    await session.record_credit_analysis(
        store, application_id=loan.application_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="HIGH",
        recommended_limit_usd=0.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.record_credit_analysis_completed(
        store, agent_id=agent_id, session_id=session_id,
        model_version="v2.0", confidence_score=0.9, risk_tier="HIGH",
        recommended_limit_usd=0.0, analysis_duration_ms=100, input_data_hash="h"
    )
    await loan.request_compliance_review(store, "reg-v1", ["KYC"])
    await loan.record_compliance_passed(store, "KYC", "reg-v1", "h1")
    await loan.generate_decision(
        store, "orch-1", "DECLINE", 0.9, [session_stream_id], "high risk", {}
    )
    await loan.complete_human_review(store, "reviewer-1", "DECLINE")
    await loan.decline(store, ["high risk score"], "reviewer-1")

    assert loan.state == ApplicationState.FINAL_DECLINED
