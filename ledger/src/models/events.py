from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# Re-export so consumers import from one place.
from src.event_store import OptimisticConcurrencyError  # noqa: F401


# ---------------------------------------------------------------------------
# Structured DomainError
# ---------------------------------------------------------------------------

class DomainError(Exception):
    """
    Raised for all domain invariant violations.

    Carries structured fields so callers can inspect the violation
    programmatically rather than parsing the message string.

    aggregate_type: which aggregate raised (e.g. "LoanApplication")
    stream_id:      the stream where the violation occurred
    rule:           short machine-readable rule name (e.g. "state_transition",
                    "gas_town", "confidence_floor", "compliance_dependency")
    detail:         human-readable explanation
    """

    def __init__(
        self,
        detail: str,
        *,
        aggregate_type: str = "",
        stream_id: str = "",
        rule: str = "",
    ) -> None:
        self.detail = detail
        self.aggregate_type = aggregate_type
        self.stream_id = stream_id
        self.rule = rule
        super().__init__(detail)

    def __repr__(self) -> str:
        return (
            f"DomainError(rule={self.rule!r}, aggregate={self.aggregate_type!r}, "
            f"stream={self.stream_id!r}, detail={self.detail!r})"
        )


# ---------------------------------------------------------------------------
# Base envelope
# ---------------------------------------------------------------------------

class BaseEvent(BaseModel):
    """
    Minimum envelope every domain event must carry before being appended.
    event_id:      client-supplied for idempotent retry safety.
    event_version: schema version; drives the upcaster chain on load.
    """
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_version: int = 1


class StoredEvent(BaseModel):
    """A domain event as it exists in the store after being persisted."""
    event_id: uuid.UUID
    stream_id: str
    stream_position: int
    global_position: int
    event_type: str
    event_version: int
    payload: dict[str, Any]
    metadata: dict[str, Any]
    recorded_at: datetime


class StreamMetadata(BaseModel):
    """Mirrors the event_streams table row."""
    stream_id: str
    aggregate_type: str
    current_version: int
    created_at: datetime
    archived_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Concrete event models — typed payload fields
# Each class maps 1-to-1 to an event_type written to the events table.
# ---------------------------------------------------------------------------

class ApplicationSubmitted(BaseModel):
    """LoanApplication stream — first event; creates the stream."""
    application_id: str
    applicant_id: str
    requested_amount_usd: float
    loan_purpose: str = ""
    submission_channel: str = "api"


class CreditAnalysisRequested(BaseModel):
    """LoanApplication stream — transitions Submitted → AwaitingAnalysis."""
    application_id: str
    assigned_agent_id: str
    priority: str = "normal"


class CreditAnalysisCompleted(BaseModel):
    """
    Written to both AgentSession stream and LoanApplication stream.
    model_version: v1 payload lacks confidence_score and regulatory_basis
                   (added by upcaster chain in Phase 4).
    """
    application_id: str
    agent_id: str
    session_id: str
    model_version: str
    confidence_score: float
    risk_tier: str                    # "LOW" | "MEDIUM" | "HIGH"
    recommended_limit_usd: float
    analysis_duration_ms: int
    input_data_hash: str              # SHA-256 of input_data for audit trail


class FraudScreeningCompleted(BaseModel):
    """AgentSession stream — fraud risk assessment result."""
    application_id: str
    agent_id: str
    fraud_score: float                # 0.0 (clean) – 1.0 (high risk)
    anomaly_flags: list[str] = Field(default_factory=list)
    screening_model_version: str
    input_data_hash: str


class ComplianceCheckRequested(BaseModel):
    """LoanApplication + ComplianceRecord streams — initiates compliance phase."""
    application_id: str
    regulation_set_version: str
    checks_required: list[str]        # e.g. ["KYC", "AML", "SANCTIONS"]


class ComplianceRulePassed(BaseModel):
    """ComplianceRecord stream — one mandatory check cleared."""
    application_id: str
    rule_id: str                      # e.g. "KYC"
    rule_version: str                 # regulation version evaluated against
    evidence_hash: str                # SHA-256 of supporting evidence


class ComplianceRuleFailed(BaseModel):
    """ComplianceRecord stream — one mandatory check failed."""
    application_id: str
    rule_id: str
    rule_version: str
    failure_reason: str
    remediation_required: bool = False


class DecisionGenerated(BaseModel):
    """
    LoanApplication stream — orchestrator's final recommendation.
    forced_refer: True when confidence_score < 0.6 overrode recommendation.
    """
    application_id: str
    orchestrator_agent_id: str
    recommendation: str               # "APPROVE" | "DECLINE" | "REFER"
    confidence_score: float
    contributing_agent_sessions: list[str]
    decision_basis_summary: str
    model_versions: dict[str, str] = Field(default_factory=dict)
    forced_refer: bool = False


class HumanReviewCompleted(BaseModel):
    """LoanApplication stream — human reviewer's final decision."""
    application_id: str
    reviewer_id: str
    final_decision: str               # "APPROVE" | "DECLINE"
    override: bool = False
    override_reason: str = ""


class ApplicationApproved(BaseModel):
    """LoanApplication stream — terminal approval event."""
    application_id: str
    approved_amount_usd: float
    interest_rate: float
    conditions: list[str] = Field(default_factory=list)
    approved_by: str
    effective_date: str


class ApplicationDeclined(BaseModel):
    """LoanApplication stream — terminal decline event."""
    application_id: str
    decline_reasons: list[str] = Field(default_factory=list)
    declined_by: str
    adverse_action_notice_required: bool = False


class AgentContextLoaded(BaseModel):
    """
    AgentSession stream — Gas Town invariant: must be the first event.
    Declares the context source so every agent action is traceable.
    """
    agent_id: str
    session_id: str
    context_source: str               # "event_replay" | "snapshot" | "cold_start"
    event_replay_from_position: int = 0
    context_token_count: int = 0
    model_version: str
    context: dict[str, Any] = Field(default_factory=dict)


class SessionClosed(BaseModel):
    """AgentSession stream — session explicitly closed; no further events allowed."""
    agent_id: str
    session_id: str


class ComplianceClearanceIssued(BaseModel):
    """ComplianceRecord stream — all mandatory checks passed; clearance granted."""
    application_id: str


class AuditEntryRecorded(BaseModel):
    """AuditLedger stream — cross-stream audit link."""
    entity_type: str
    entity_id: str
    source_stream_id: str
    event_type: str
    summary: str


class AuditIntegrityCheckRun(BaseModel):
    """AuditLedger stream — SHA-256 hash chain node for tamper detection."""
    entity_id: str
    events_verified_count: int
    integrity_hash: str
    previous_hash: str | None
    chain_valid: bool


# ---------------------------------------------------------------------------
# Catalogue — maps event_type string → typed model class
# Used by projections and upcasters to deserialise payloads with type safety.
# ---------------------------------------------------------------------------

EVENT_CATALOGUE: dict[str, type[BaseModel]] = {
    "ApplicationSubmitted":      ApplicationSubmitted,
    "CreditAnalysisRequested":   CreditAnalysisRequested,
    "CreditAnalysisCompleted":   CreditAnalysisCompleted,
    "FraudScreeningCompleted":   FraudScreeningCompleted,
    "ComplianceCheckRequested":  ComplianceCheckRequested,
    "ComplianceRulePassed":      ComplianceRulePassed,
    "ComplianceRuleFailed":      ComplianceRuleFailed,
    "DecisionGenerated":         DecisionGenerated,
    "HumanReviewCompleted":      HumanReviewCompleted,
    "ApplicationApproved":       ApplicationApproved,
    "ApplicationDeclined":       ApplicationDeclined,
    "AgentContextLoaded":        AgentContextLoaded,
    "SessionClosed":             SessionClosed,
    "ComplianceClearanceIssued": ComplianceClearanceIssued,
    "AuditEntryRecorded":        AuditEntryRecorded,
    "AuditIntegrityCheckRun":    AuditIntegrityCheckRun,
}


def parse_event(event_type: str, payload: dict[str, Any]) -> BaseModel:
    """
    Deserialise a raw payload dict into the typed event model.
    Falls back to a plain dict wrapper if the event_type is not in the catalogue
    (e.g. future event types not yet registered).
    """
    model_cls = EVENT_CATALOGUE.get(event_type)
    if model_cls is None:
        return BaseEvent(event_type=event_type, payload=payload)
    return model_cls(**payload)
