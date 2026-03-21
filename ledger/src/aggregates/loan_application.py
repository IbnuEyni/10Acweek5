from __future__ import annotations

from enum import Enum

from src.aggregates.base import Aggregate, DomainError
from src.event_store import EventStore, RecordedEvent


class ApplicationState(str, Enum):
    """
    Strict forward-only state machine per the brief.

    Submitted → AwaitingAnalysis → AnalysisComplete → ComplianceReview
              → PendingDecision → ApprovedPendingHuman | DeclinedPendingHuman
              → FinalApproved | FinalDeclined
    """
    SUBMITTED              = "Submitted"
    AWAITING_ANALYSIS      = "AwaitingAnalysis"
    ANALYSIS_COMPLETE      = "AnalysisComplete"
    COMPLIANCE_REVIEW      = "ComplianceReview"
    PENDING_DECISION       = "PendingDecision"
    APPROVED_PENDING_HUMAN = "ApprovedPendingHuman"
    DECLINED_PENDING_HUMAN = "DeclinedPendingHuman"
    FINAL_APPROVED         = "FinalApproved"
    FINAL_DECLINED         = "FinalDeclined"


# Allowed forward transitions — any other target raises DomainError.
_TRANSITIONS: dict[ApplicationState, set[ApplicationState]] = {
    ApplicationState.SUBMITTED:              {ApplicationState.AWAITING_ANALYSIS},
    ApplicationState.AWAITING_ANALYSIS:      {ApplicationState.ANALYSIS_COMPLETE},
    ApplicationState.ANALYSIS_COMPLETE:      {ApplicationState.COMPLIANCE_REVIEW},
    ApplicationState.COMPLIANCE_REVIEW:      {ApplicationState.PENDING_DECISION},
    ApplicationState.PENDING_DECISION:       {ApplicationState.APPROVED_PENDING_HUMAN,
                                              ApplicationState.DECLINED_PENDING_HUMAN},
    ApplicationState.APPROVED_PENDING_HUMAN: {ApplicationState.FINAL_APPROVED,
                                              ApplicationState.FINAL_DECLINED},
    ApplicationState.DECLINED_PENDING_HUMAN: {ApplicationState.FINAL_APPROVED,
                                              ApplicationState.FINAL_DECLINED},
    ApplicationState.FINAL_APPROVED:         set(),
    ApplicationState.FINAL_DECLINED:         set(),
}

CONFIDENCE_FLOOR = 0.6


class LoanApplicationAggregate(Aggregate):
    """
    Enforces all 6 business rules from the brief:

    1. State machine — strict forward-only transitions.
    2. Agent context requirement (Gas Town) — enforced in AgentSessionAggregate;
       the handler validates before calling here.
    3. Model version locking — once CreditAnalysisCompleted is recorded, no
       further credit analysis may be appended unless superseded by HumanReviewOverride.
    4. Confidence floor — DecisionGenerated with confidence_score < 0.6 forces
       recommendation = 'REFER'.
    5. Compliance dependency — ApplicationApproved cannot be appended unless
       all required compliance checks are passed.
    6. Causal chain enforcement — DecisionGenerated.contributing_agent_sessions
       must reference only sessions that contain a decision for this application.

    stream_id format: "loan-{application_id}"
    """

    AGGREGATE_TYPE = "LoanApplication"

    def __init__(self, stream_id: str) -> None:
        super().__init__(stream_id)
        self.state: ApplicationState = ApplicationState.SUBMITTED
        self.applicant_id: str | None = None
        self.requested_amount: float | None = None
        self.approved_amount: float | None = None

        # Business Rule 3: model version locking
        self.credit_analysis_recorded: bool = False
        self.credit_analysis_superseded: bool = False

        # Business Rule 5: compliance dependency
        self.required_checks: set[str] = set()
        self.passed_checks: set[str] = set()

        # Business Rule 6: causal chain — sessions that produced a decision for this app
        self.contributing_sessions: set[str] = set()

        # Track the application_id for stream lookups
        self.application_id: str = stream_id.removeprefix("loan-")

    # ------------------------------------------------------------------
    # Event application (replay)
    # ------------------------------------------------------------------

    def _apply(self, event: RecordedEvent) -> None:
        match event.event_type:
            case "ApplicationSubmitted":
                self.state = ApplicationState.SUBMITTED
                self.applicant_id = event.payload.get("applicant_id")
                self.requested_amount = event.payload.get("requested_amount_usd")

            case "CreditAnalysisRequested":
                self.state = ApplicationState.AWAITING_ANALYSIS

            case "CreditAnalysisCompleted":
                self.state = ApplicationState.ANALYSIS_COMPLETE
                self.credit_analysis_recorded = True
                # Register the session as a valid contributing session (Rule 6)
                agent_id = event.payload.get("agent_id", "")
                session_id = event.payload.get("session_id", "")
                if agent_id and session_id:
                    self.contributing_sessions.add(f"agent-{agent_id}-{session_id}")

            case "HumanReviewOverride":
                # Supersedes the credit analysis lock — allows re-analysis
                self.credit_analysis_superseded = True
                self.credit_analysis_recorded = False

            case "ComplianceCheckRequested":
                self.state = ApplicationState.COMPLIANCE_REVIEW
                for check in event.payload.get("checks_required", []):
                    self.required_checks.add(check)

            case "ComplianceRulePassed":
                self.passed_checks.add(event.payload.get("rule_id", ""))

            case "DecisionGenerated":
                self.state = ApplicationState.PENDING_DECISION
                for session_id in event.payload.get("contributing_agent_sessions", []):
                    self.contributing_sessions.add(session_id)

            case "ApplicationApproved":
                self.state = ApplicationState.FINAL_APPROVED
                self.approved_amount = event.payload.get("approved_amount_usd")

            case "ApplicationDeclined":
                self.state = ApplicationState.FINAL_DECLINED

            case "HumanReviewCompleted":
                override = event.payload.get("override", False)
                final = event.payload.get("final_decision", "")
                if final == "APPROVE":
                    self.state = ApplicationState.APPROVED_PENDING_HUMAN
                elif final == "DECLINE":
                    self.state = ApplicationState.DECLINED_PENDING_HUMAN

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assert_state(self, *allowed: ApplicationState) -> None:
        if self.state not in allowed:
            raise DomainError(
                f"LoanApplication '{self.stream_id}' is in state '{self.state.value}'; "
                f"expected one of {[s.value for s in allowed]}."
            )

    def _transition(
        self,
        target: ApplicationState,
        event_type: str,
        payload: dict,
    ) -> None:
        allowed = _TRANSITIONS.get(self.state, set())
        if target not in allowed:
            raise DomainError(
                f"Cannot transition LoanApplication '{self.stream_id}' "
                f"from '{self.state.value}' to '{target.value}'."
            )
        self._stage(event_type, payload)
        # Do NOT set self.state here — _apply is the single source of truth.
        # save() will call _apply on the staged event, which sets the state.

    # ------------------------------------------------------------------
    # Assertion helpers (called by command handlers — brief pattern)
    # ------------------------------------------------------------------

    def assert_awaiting_credit_analysis(self) -> None:
        """Business Rule 1: must be in AwaitingAnalysis to accept credit analysis."""
        self._assert_state(ApplicationState.AWAITING_ANALYSIS)

    def assert_no_credit_analysis_locked(self) -> None:
        """Business Rule 3: reject duplicate credit analysis unless superseded."""
        if self.credit_analysis_recorded and not self.credit_analysis_superseded:
            raise DomainError(
                f"LoanApplication '{self.stream_id}' already has a credit analysis. "
                "A HumanReviewOverride is required before re-analysis."
            )

    def assert_compliance_complete(self) -> None:
        """Business Rule 5: all required checks must be passed before approval."""
        missing = self.required_checks - self.passed_checks
        if missing:
            raise DomainError(
                f"LoanApplication '{self.stream_id}' cannot be approved: "
                f"compliance checks not yet passed: {sorted(missing)}."
            )

    def assert_contributing_sessions_valid(
        self, contributing_sessions: list[str]
    ) -> None:
        """
        Business Rule 6: every session in contributing_agent_sessions must have
        previously produced a decision event for this application.
        """
        invalid = set(contributing_sessions) - self.contributing_sessions
        if invalid:
            raise DomainError(
                f"DecisionGenerated references sessions that never processed "
                f"application '{self.application_id}': {sorted(invalid)}."
            )

    # ------------------------------------------------------------------
    # Commands (load → validate → determine → append via save())
    # ------------------------------------------------------------------

    @classmethod
    async def load(cls, store: EventStore, application_id: str) -> "LoanApplicationAggregate":
        stream_id = f"loan-{application_id}"
        return await super().load(stream_id, store)  # type: ignore[return-value]

    @classmethod
    async def submit(
        cls,
        stream_id: str,
        store: EventStore,
        applicant_id: str,
        requested_amount_usd: float,
        loan_purpose: str = "",
        submission_channel: str = "api",
    ) -> "LoanApplicationAggregate":
        agg = cls(stream_id)
        agg._stage(
            "ApplicationSubmitted",
            {
                "application_id": agg.application_id,
                "applicant_id": applicant_id,
                "requested_amount_usd": requested_amount_usd,
                "loan_purpose": loan_purpose,
                "submission_channel": submission_channel,
            },
        )
        await agg.save(store)  # save() applies events via _apply — single source of truth
        return agg

    async def request_credit_analysis(
        self,
        store: EventStore,
        assigned_agent_id: str,
        priority: str = "normal",
    ) -> None:
        """Business Rule 1: only valid from Submitted state."""
        self._transition(
            ApplicationState.AWAITING_ANALYSIS,
            "CreditAnalysisRequested",
            {
                "application_id": self.application_id,
                "assigned_agent_id": assigned_agent_id,
                "priority": priority,
            },
        )
        await self.save(store)

    async def record_credit_analysis_completed(
        self,
        store: EventStore,
        agent_id: str,
        session_id: str,
        model_version: str,
        confidence_score: float,
        risk_tier: str,
        recommended_limit_usd: float,
        analysis_duration_ms: int,
        input_data_hash: str,
    ) -> None:
        """
        Business Rules 1 + 3:
        - Must be in AwaitingAnalysis.
        - No duplicate credit analysis unless superseded.
        Records the session as a contributing session for Rule 6.
        """
        self.assert_awaiting_credit_analysis()
        self.assert_no_credit_analysis_locked()
        self._transition(
            ApplicationState.ANALYSIS_COMPLETE,
            "CreditAnalysisCompleted",
            {
                "application_id": self.application_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "model_version": model_version,
                "confidence_score": confidence_score,
                "risk_tier": risk_tier,
                "recommended_limit_usd": recommended_limit_usd,
                "analysis_duration_ms": analysis_duration_ms,
                "input_data_hash": input_data_hash,
            },
        )
        await self.save(store)

    async def request_compliance_review(
        self,
        store: EventStore,
        regulation_set_version: str,
        checks_required: list[str],
    ) -> None:
        """Business Rule 1: must be in AnalysisComplete."""
        self._transition(
            ApplicationState.COMPLIANCE_REVIEW,
            "ComplianceCheckRequested",
            {
                "application_id": self.application_id,
                "regulation_set_version": regulation_set_version,
                "checks_required": checks_required,
            },
        )
        await self.save(store)

    async def record_compliance_passed(
        self,
        store: EventStore,
        rule_id: str,
        rule_version: str,
        evidence_hash: str,
    ) -> None:
        """Must be in ComplianceReview. Does not advance state — accumulates passes."""
        self._assert_state(ApplicationState.COMPLIANCE_REVIEW)
        self._stage(
            "ComplianceRulePassed",
            {
                "application_id": self.application_id,
                "rule_id": rule_id,
                "rule_version": rule_version,
                "evidence_hash": evidence_hash,
            },
        )
        await self.save(store)

    async def generate_decision(
        self,
        store: EventStore,
        orchestrator_agent_id: str,
        recommendation: str,
        confidence_score: float,
        contributing_agent_sessions: list[str],
        decision_basis_summary: str,
        model_versions: dict,
    ) -> None:
        """
        Business Rules 1 + 4 + 6:
        - Must be in ComplianceReview.
        - Confidence floor: score < 0.6 forces REFER.
        - Causal chain: all contributing sessions must have processed this app.
        """
        self._assert_state(ApplicationState.COMPLIANCE_REVIEW)
        self.assert_contributing_sessions_valid(contributing_agent_sessions)

        # Business Rule 4: confidence floor
        forced_refer = confidence_score < CONFIDENCE_FLOOR
        effective_recommendation = "REFER" if forced_refer else recommendation

        self._transition(
            ApplicationState.PENDING_DECISION,
            "DecisionGenerated",
            {
                "application_id": self.application_id,
                "orchestrator_agent_id": orchestrator_agent_id,
                "recommendation": effective_recommendation,
                "confidence_score": confidence_score,
                "contributing_agent_sessions": contributing_agent_sessions,
                "decision_basis_summary": decision_basis_summary,
                "model_versions": model_versions,
                "forced_refer": forced_refer,
            },
        )
        await self.save(store)

    async def complete_human_review(
        self,
        store: EventStore,
        reviewer_id: str,
        final_decision: str,
        override: bool = False,
        override_reason: str = "",
    ) -> None:
        """
        Business Rule 1: must be in PendingDecision.
        Advances to ApprovedPendingHuman or DeclinedPendingHuman.
        """
        self._assert_state(ApplicationState.PENDING_DECISION)
        if override and not override_reason:
            raise DomainError("override_reason is required when override=True.")

        target = (
            ApplicationState.APPROVED_PENDING_HUMAN
            if final_decision == "APPROVE"
            else ApplicationState.DECLINED_PENDING_HUMAN
        )
        self._transition(
            target,
            "HumanReviewCompleted",
            {
                "application_id": self.application_id,
                "reviewer_id": reviewer_id,
                "override": override,
                "final_decision": final_decision,
                "override_reason": override_reason,
            },
        )
        await self.save(store)

    async def approve(
        self,
        store: EventStore,
        approved_amount_usd: float,
        interest_rate: float,
        conditions: list[str],
        approved_by: str,
        effective_date: str,
    ) -> None:
        """
        Business Rules 1 + 5:
        - Must be in ApprovedPendingHuman.
        - All required compliance checks must be passed (loan-stream check).
        """
        self._assert_state(ApplicationState.APPROVED_PENDING_HUMAN)
        self.assert_compliance_complete()
        self._transition(
            ApplicationState.FINAL_APPROVED,
            "ApplicationApproved",
            {
                "application_id": self.application_id,
                "approved_amount_usd": approved_amount_usd,
                "interest_rate": interest_rate,
                "conditions": conditions,
                "approved_by": approved_by,
                "effective_date": effective_date,
            },
        )
        await self.save(store)

    async def decline(
        self,
        store: EventStore,
        decline_reasons: list[str],
        declined_by: str,
        adverse_action_notice_required: bool = False,
    ) -> None:
        """Business Rule 1: must be in DeclinedPendingHuman."""
        self._assert_state(ApplicationState.DECLINED_PENDING_HUMAN)
        self._transition(
            ApplicationState.FINAL_DECLINED,
            "ApplicationDeclined",
            {
                "application_id": self.application_id,
                "decline_reasons": decline_reasons,
                "declined_by": declined_by,
                "adverse_action_notice_required": adverse_action_notice_required,
            },
        )
        await self.save(store)


