from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from src.aggregates.loan_application import LoanApplicationAggregate
from src.aggregates.agent_session import AgentSessionAggregate
from src.aggregates.compliance_record import ComplianceRecordAggregate
from src.aggregates.base import DomainError
from src.event_store import EventStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_inputs(data: dict) -> str:
    """Deterministic SHA-256 of a JSON-serialised dict — used for input_data_hash."""
    return hashlib.sha256(
        json.dumps(data, sort_keys=True).encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# Command dataclasses — plain value objects, no behaviour
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StartAgentSessionCommand:
    """Gas Town: must be called before any agent decision tool."""
    agent_id: str
    session_id: str
    context_source: str = "event_replay"
    event_replay_from_position: int = 0
    context_token_count: int = 0
    model_version: str = ""
    correlation_id: str | None = None
    causation_id: str | None = None

    @property
    def stream_id(self) -> str:
        return f"agent-{self.agent_id}-{self.session_id}"


@dataclass(frozen=True)
class SubmitApplicationCommand:
    application_id: str
    applicant_id: str
    requested_amount_usd: float
    loan_purpose: str = ""
    submission_channel: str = "api"
    correlation_id: str | None = None
    causation_id: str | None = None

    @property
    def stream_id(self) -> str:
        return f"loan-{self.application_id}"


@dataclass(frozen=True)
class CreditAnalysisCompletedCommand:
    """
    Precondition: an active AgentSession must exist with context loaded
    (start_agent_session must have been called first — Gas Town pattern).
    """
    application_id: str
    agent_id: str
    session_id: str
    model_version: str
    confidence_score: float
    risk_tier: str
    recommended_limit_usd: float
    duration_ms: int
    input_data: dict = field(default_factory=dict)
    correlation_id: str | None = None
    causation_id: str | None = None

    @property
    def loan_stream_id(self) -> str:
        return f"loan-{self.application_id}"

    @property
    def session_stream_id(self) -> str:
        return f"agent-{self.agent_id}-{self.session_id}"


@dataclass(frozen=True)
class FraudScreeningCompletedCommand:
    """
    Precondition: same agent session validation as credit analysis.
    fraud_score must be 0.0–1.0.
    """
    application_id: str
    agent_id: str
    session_id: str
    fraud_score: float
    anomaly_flags: tuple = field(default_factory=tuple)
    screening_model_version: str = ""
    input_data: dict = field(default_factory=dict)
    correlation_id: str | None = None
    causation_id: str | None = None

    @property
    def session_stream_id(self) -> str:
        return f"agent-{self.agent_id}-{self.session_id}"


@dataclass(frozen=True)
class ComplianceCheckCommand:
    """Record a single compliance rule result (pass or fail)."""
    application_id: str
    rule_id: str
    rule_version: str
    passed: bool
    failure_reason: str = ""
    remediation_required: bool = False
    evidence_data: dict = field(default_factory=dict)
    regulation_set_version: str = "v1.0"
    checks_required: tuple = field(default_factory=tuple)
    correlation_id: str | None = None
    causation_id: str | None = None

    @property
    def compliance_stream_id(self) -> str:
        return f"compliance-{self.application_id}"


@dataclass(frozen=True)
class GenerateDecisionCommand:
    """
    Preconditions:
    - All required analyses must be present.
    - Confidence floor enforced: score < 0.6 → REFER.
    - Causal chain: contributing_agent_sessions must reference sessions
      that processed this application.
    """
    application_id: str
    orchestrator_agent_id: str
    recommendation: str          # "APPROVE" | "DECLINE" | "REFER"
    confidence_score: float
    contributing_agent_sessions: tuple = field(default_factory=tuple)
    decision_basis_summary: str = ""
    model_versions: dict = field(default_factory=dict)
    correlation_id: str | None = None
    causation_id: str | None = None

    @property
    def loan_stream_id(self) -> str:
        return f"loan-{self.application_id}"


@dataclass(frozen=True)
class RequestCreditAnalysisCommand:
    """Advances LoanApplication from Submitted → AwaitingAnalysis."""
    application_id: str
    assigned_agent_id: str
    priority: str = "normal"
    correlation_id: str | None = None
    causation_id: str | None = None

    @property
    def loan_stream_id(self) -> str:
        return f"loan-{self.application_id}"


@dataclass(frozen=True)
class HumanReviewCompletedCommand:
    """
    reviewer_id authentication is the caller's responsibility.
    If override=True, override_reason is required.
    """
    application_id: str
    reviewer_id: str
    final_decision: str          # "APPROVE" | "DECLINE"
    override: bool = False
    override_reason: str = ""
    correlation_id: str | None = None
    causation_id: str | None = None

    @property
    def loan_stream_id(self) -> str:
        return f"loan-{self.application_id}"


@dataclass(frozen=True)
class ApproveApplicationCommand:
    """
    Preconditions:
    - LoanApplication must be in ApprovedPendingHuman.
    - ComplianceRecord stream must have all required checks passed (cross-stream Rule 5).
    """
    application_id: str
    approved_amount_usd: float
    interest_rate: float
    conditions: list = None
    approved_by: str = ""
    effective_date: str = ""
    correlation_id: str | None = None
    causation_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "conditions", self.conditions or [])

    @property
    def loan_stream_id(self) -> str:
        return f"loan-{self.application_id}"


@dataclass(frozen=True)
class DeclineApplicationCommand:
    """Precondition: LoanApplication must be in DeclinedPendingHuman."""
    application_id: str
    decline_reasons: list = None
    declined_by: str = ""
    adverse_action_notice_required: bool = False
    correlation_id: str | None = None
    causation_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "decline_reasons", self.decline_reasons or [])

    @property
    def loan_stream_id(self) -> str:
        return f"loan-{self.application_id}"


# ---------------------------------------------------------------------------
# Handlers — load → validate → determine → append
# ---------------------------------------------------------------------------

async def handle_start_agent_session(
    cmd: StartAgentSessionCommand,
    store: EventStore,
) -> AgentSessionAggregate:
    """
    Gas Town: required before any agent decision tools.
    Writes AgentContextLoaded as the first event in the session stream.
    """
    existing = await store.stream_version(cmd.stream_id)
    if existing > 0:
        raise DomainError(
            f"AgentSession '{cmd.stream_id}' already exists at version {existing}. "
            "Use a new session_id for a new session."
        )

    return await AgentSessionAggregate.open(
        cmd.stream_id,
        store,
        agent_id=cmd.agent_id,
        context_source=cmd.context_source,
        event_replay_from_position=cmd.event_replay_from_position,
        context_token_count=cmd.context_token_count,
        model_version=cmd.model_version,
        session_id=cmd.session_id,
    )


async def handle_request_credit_analysis(
    cmd: RequestCreditAnalysisCommand,
    store: EventStore,
) -> LoanApplicationAggregate:
    """
    Advances LoanApplication from Submitted → AwaitingAnalysis.
    Business Rule 1: must be in Submitted state.
    """
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    await app.request_credit_analysis(store, cmd.assigned_agent_id, cmd.priority)
    return app


async def handle_submit_application(
    cmd: SubmitApplicationCommand,
    store: EventStore,
) -> LoanApplicationAggregate:
    """
    Creates a new LoanApplication stream and records ApplicationSubmitted.
    Raises DomainError if the stream already exists (duplicate application_id).
    """
    existing = await store.stream_version(cmd.stream_id)
    if existing > 0:
        raise DomainError(
            f"LoanApplication '{cmd.stream_id}' already exists at version {existing}."
        )

    return await LoanApplicationAggregate.submit(
        cmd.stream_id,
        store,
        applicant_id=cmd.applicant_id,
        requested_amount_usd=cmd.requested_amount_usd,
        loan_purpose=cmd.loan_purpose,
        submission_channel=cmd.submission_channel,
    )


async def handle_credit_analysis_completed(
    cmd: CreditAnalysisCompletedCommand,
    store: EventStore,
) -> None:
    """
    Exact brief pattern: load → validate → determine → append.

    1. Reconstruct current aggregate state from event history.
    2. Validate — all business rules checked BEFORE any state change.
    3. Determine new events — pure logic, no I/O.
    4. Append atomically — optimistic concurrency enforced by store.

    Business Rules enforced:
    - Rule 1: LoanApplication must be in AwaitingAnalysis.
    - Rule 2 (Gas Town): AgentSession must have context loaded.
    - Rule 3: No duplicate credit analysis unless superseded.
    - Model version must match session's declared version.
    """
    # 1. Reconstruct
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    agent = await AgentSessionAggregate.load(store, cmd.agent_id, cmd.session_id)

    # 2. Validate
    app.assert_awaiting_credit_analysis()          # Rule 1
    agent.assert_context_loaded()                  # Rule 2 (Gas Town)
    agent.assert_model_version_current(cmd.model_version)  # Rule 3
    agent.assert_no_credit_analysis_locked(cmd.application_id)  # Rule 3

    # 3. Determine
    input_data_hash = _hash_inputs(cmd.input_data)

    # 4. Append — session stream first, then loan stream
    await agent.record_credit_analysis(
        store,
        application_id=cmd.application_id,
        model_version=cmd.model_version,
        confidence_score=cmd.confidence_score,
        risk_tier=cmd.risk_tier,
        recommended_limit_usd=cmd.recommended_limit_usd,
        analysis_duration_ms=cmd.duration_ms,
        input_data_hash=input_data_hash,
    )

    await app.record_credit_analysis_completed(
        store,
        agent_id=cmd.agent_id,
        session_id=cmd.session_id,
        model_version=cmd.model_version,
        confidence_score=cmd.confidence_score,
        risk_tier=cmd.risk_tier,
        recommended_limit_usd=cmd.recommended_limit_usd,
        analysis_duration_ms=cmd.duration_ms,
        input_data_hash=input_data_hash,
    )


async def handle_fraud_screening_completed(
    cmd: FraudScreeningCompletedCommand,
    store: EventStore,
) -> None:
    """
    Business Rules:
    - Rule 2 (Gas Town): AgentSession must have context loaded.
    - fraud_score must be 0.0–1.0.
    """
    agent = await AgentSessionAggregate.load(store, cmd.agent_id, cmd.session_id)

    # Validate
    agent.assert_context_loaded()
    agent.assert_not_closed()

    if not (0.0 <= cmd.fraud_score <= 1.0):
        raise DomainError(
            f"fraud_score must be in [0.0, 1.0], got {cmd.fraud_score}."
        )

    input_data_hash = _hash_inputs(cmd.input_data)

    await agent.record_fraud_screening(
        store,
        application_id=cmd.application_id,
        fraud_score=cmd.fraud_score,
        anomaly_flags=list(cmd.anomaly_flags),
        screening_model_version=cmd.screening_model_version,
        input_data_hash=input_data_hash,
    )


async def handle_compliance_check(
    cmd: ComplianceCheckCommand,
    store: EventStore,
) -> ComplianceRecordAggregate:
    """
    Records a compliance rule result (pass or fail).
    Initialises the ComplianceRecord stream if it doesn't exist yet.
    """
    existing = await store.stream_version(cmd.compliance_stream_id)

    if existing == 0:
        # First check — initialise the compliance record
        compliance = await ComplianceRecordAggregate.request_checks(
            cmd.compliance_stream_id,
            store,
            regulation_set_version=cmd.regulation_set_version,
            checks_required=list(cmd.checks_required) or [cmd.rule_id],
        )
    else:
        compliance = await ComplianceRecordAggregate.load(store, cmd.application_id)

    if cmd.passed:
        evidence_hash = _hash_inputs(cmd.evidence_data)
        await compliance.record_rule_passed(
            store,
            rule_id=cmd.rule_id,
            rule_version=cmd.rule_version,
            evidence_hash=evidence_hash,
        )
    else:
        await compliance.record_rule_failed(
            store,
            rule_id=cmd.rule_id,
            rule_version=cmd.rule_version,
            failure_reason=cmd.failure_reason,
            remediation_required=cmd.remediation_required,
        )

    return compliance


async def handle_generate_decision(
    cmd: GenerateDecisionCommand,
    store: EventStore,
) -> LoanApplicationAggregate:
    """
    Business Rules:
    - Rule 1: LoanApplication must be in ComplianceReview.
    - Rule 4: Confidence floor — score < 0.6 forces REFER.
    - Rule 5 (cross-stream): ComplianceRecord stream must have all required
              checks passed before a decision can be generated.
    - Rule 6: Causal chain — contributing_agent_sessions must reference
              sessions that processed this application.
    """
    app = await LoanApplicationAggregate.load(store, cmd.application_id)

    # Rule 5: cross-stream compliance check — load ComplianceRecord stream
    compliance = await ComplianceRecordAggregate.load(store, cmd.application_id)
    compliance.assert_all_checks_passed()

    # Rule 6: validate causal chain before any state change
    app.assert_contributing_sessions_valid(list(cmd.contributing_agent_sessions))

    await app.generate_decision(
        store,
        orchestrator_agent_id=cmd.orchestrator_agent_id,
        recommendation=cmd.recommendation,
        confidence_score=cmd.confidence_score,
        contributing_agent_sessions=list(cmd.contributing_agent_sessions),
        decision_basis_summary=cmd.decision_basis_summary,
        model_versions=cmd.model_versions,
    )

    return app


async def handle_human_review_completed(
    cmd: HumanReviewCompletedCommand,
    store: EventStore,
) -> LoanApplicationAggregate:
    """
    Business Rule 1: LoanApplication must be in PendingDecision.
    If override=True, override_reason is required.
    """
    app = await LoanApplicationAggregate.load(store, cmd.application_id)

    await app.complete_human_review(
        store,
        reviewer_id=cmd.reviewer_id,
        final_decision=cmd.final_decision,
        override=cmd.override,
        override_reason=cmd.override_reason,
    )

    return app


async def handle_approve_application(
    cmd: ApproveApplicationCommand,
    store: EventStore,
) -> LoanApplicationAggregate:
    """
    Business Rules 1 + 5 (cross-stream):
    - LoanApplication must be in ApprovedPendingHuman.
    - ComplianceRecord stream must have all required checks passed.
    """
    app = await LoanApplicationAggregate.load(store, cmd.application_id)

    # Rule 5: cross-stream — verify ComplianceRecord stream directly
    compliance = await ComplianceRecordAggregate.load(store, cmd.application_id)
    compliance.assert_all_checks_passed()

    await app.approve(
        store,
        approved_amount_usd=cmd.approved_amount_usd,
        interest_rate=cmd.interest_rate,
        conditions=list(cmd.conditions),
        approved_by=cmd.approved_by,
        effective_date=cmd.effective_date,
    )
    return app


async def handle_decline_application(
    cmd: DeclineApplicationCommand,
    store: EventStore,
) -> LoanApplicationAggregate:
    """Business Rule 1: LoanApplication must be in DeclinedPendingHuman."""
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    await app.decline(
        store,
        decline_reasons=list(cmd.decline_reasons),
        declined_by=cmd.declined_by,
        adverse_action_notice_required=cmd.adverse_action_notice_required,
    )
    return app
