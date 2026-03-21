from __future__ import annotations

from src.aggregates.base import Aggregate, DomainError
from src.event_store import EventStore, RecordedEvent

CONFIDENCE_FLOOR = 0.6


class AgentSessionAggregate(Aggregate):
    """
    Enforces two invariants from the brief:

    Gas Town (Business Rule 2):
        AgentContextLoaded MUST be the first event. Any decision attempted
        before context is loaded raises DomainError. This ensures every agent
        action is traceable to a declared context source — the persistent ledger
        pattern that prevents catastrophic memory loss on process restart.

    Model Version Locking (Business Rule 3):
        Once a CreditAnalysisCompleted event is appended for an application,
        no further CreditAnalysisCompleted events may be appended for the same
        application unless the first was superseded by a HumanReviewOverride.

    stream_id format: "agent-{agent_id}-{session_id}"
    """

    AGGREGATE_TYPE = "AgentSession"

    def __init__(self, stream_id: str) -> None:
        super().__init__(stream_id)
        self.context_loaded: bool = False
        self.closed: bool = False
        self.agent_id: str | None = None
        self.session_id: str | None = None
        self.model_version: str | None = None
        self.context_source: str | None = None
        self.event_replay_from_position: int = 0

        # Business Rule 3: track which application_ids have a locked credit analysis
        # Maps application_id → model_version that produced the analysis
        self._credit_analyses: dict[str, str] = {}
        self._superseded_analyses: set[str] = set()

    # ------------------------------------------------------------------
    # Event application (replay) — one method per event type
    # ------------------------------------------------------------------

    def _apply(self, event: RecordedEvent) -> None:
        """Dispatch to the dedicated per-event handler."""
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler is not None:
            handler(event)

    def _on_AgentContextLoaded(self, event: RecordedEvent) -> None:
        self.context_loaded = True
        self.agent_id = event.payload.get("agent_id")
        self.session_id = event.payload.get("session_id")
        self.model_version = event.payload.get("model_version")
        self.context_source = event.payload.get("context_source")
        self.event_replay_from_position = event.payload.get(
            "event_replay_from_position", 0
        )

    def _on_CreditAnalysisCompleted(self, event: RecordedEvent) -> None:
        app_id = event.payload.get("application_id", "")
        mv = event.payload.get("model_version", "")
        self._credit_analyses[app_id] = mv

    def _on_HumanReviewOverride(self, event: RecordedEvent) -> None:
        app_id = event.payload.get("application_id", "")
        self._superseded_analyses.add(app_id)
        self._credit_analyses.pop(app_id, None)

    def _on_FraudScreeningCompleted(self, event: RecordedEvent) -> None:
        pass  # no state change needed beyond version tracking

    def _on_DecisionGenerated(self, event: RecordedEvent) -> None:
        pass  # recorded on LoanApplication stream; session just tracks it happened

    def _on_SessionClosed(self, event: RecordedEvent) -> None:
        self.closed = True

    # ------------------------------------------------------------------
    # Assertion helpers (called by command handlers — brief pattern)
    # ------------------------------------------------------------------

    def assert_context_loaded(self) -> None:
        """Business Rule 2 (Gas Town): context must be loaded before any decision."""
        if not self.context_loaded:
            raise DomainError(
                "AgentContextLoaded must be the first event before any decisions. "
                f"Session '{self.stream_id}' has no loaded context."
            )

    def assert_not_closed(self) -> None:
        if self.closed:
            raise DomainError(f"Session '{self.stream_id}' is already closed.")

    def assert_model_version_current(self, required_version: str) -> None:
        """
        Business Rule 3: the session's declared model_version must match
        the version the command claims to be using. Prevents stale-model decisions.
        """
        if self.model_version and self.model_version != required_version:
            raise DomainError(
                f"Session '{self.stream_id}' was opened with model_version "
                f"'{self.model_version}', but command specifies '{required_version}'. "
                "Start a new session with the current model version."
            )

    def assert_no_credit_analysis_locked(self, application_id: str) -> None:
        """
        Business Rule 3: reject duplicate credit analysis for the same application
        unless the previous one was superseded by a HumanReviewOverride.
        """
        if (
            application_id in self._credit_analyses
            and application_id not in self._superseded_analyses
        ):
            raise DomainError(
                f"Session '{self.stream_id}' already has a credit analysis for "
                f"application '{application_id}'. A HumanReviewOverride is required "
                "before re-analysis."
            )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @classmethod
    async def load(
        cls, store: EventStore, agent_id: str, session_id: str
    ) -> "AgentSessionAggregate":
        stream_id = f"agent-{agent_id}-{session_id}"
        return await super().load(stream_id, store)  # type: ignore[return-value]

    @classmethod
    async def open(
        cls,
        stream_id: str,
        store: EventStore,
        agent_id: str,
        context: dict | None = None,
        context_source: str = "event_replay",
        event_replay_from_position: int = 0,
        context_token_count: int = 0,
        model_version: str = "",
        session_id: str = "",
    ) -> "AgentSessionAggregate":
        """
        Gas Town: AgentContextLoaded is always the first event.
        No agent may make a decision without first declaring its context source.
        """
        agg = cls(stream_id)
        agg._stage(
            "AgentContextLoaded",
            {
                "agent_id": agent_id,
                "session_id": session_id or stream_id.split("-", 2)[-1],
                "context_source": context_source,
                "event_replay_from_position": event_replay_from_position,
                "context_token_count": context_token_count,
                "model_version": model_version,
                "context": context or {},
            },
        )
        # Do NOT set state directly here — save() calls _apply which sets
        # context_loaded, agent_id, model_version, context_source.
        await agg.save(store)
        return agg

    async def record_credit_analysis(
        self,
        store: EventStore,
        application_id: str,
        model_version: str,
        confidence_score: float,
        risk_tier: str,
        recommended_limit_usd: float,
        analysis_duration_ms: int,
        input_data_hash: str,
    ) -> None:
        """
        Business Rules 2 + 3:
        - Context must be loaded (Gas Town).
        - Model version must match session's declared version.
        - No duplicate credit analysis for the same application.
        """
        self.assert_context_loaded()
        self.assert_not_closed()
        self.assert_model_version_current(model_version)
        self.assert_no_credit_analysis_locked(application_id)

        self._stage(
            "CreditAnalysisCompleted",
            {
                "application_id": application_id,
                "agent_id": self.agent_id,
                "session_id": self.session_id,
                "model_version": model_version,
                "confidence_score": confidence_score,
                "risk_tier": risk_tier,
                "recommended_limit_usd": recommended_limit_usd,
                "analysis_duration_ms": analysis_duration_ms,
                "input_data_hash": input_data_hash,
            },
        )
        # Do NOT set self._credit_analyses here — _apply is the single source of truth.
        await self.save(store)

    async def record_fraud_screening(
        self,
        store: EventStore,
        application_id: str,
        fraud_score: float,
        anomaly_flags: list[str],
        screening_model_version: str,
        input_data_hash: str,
    ) -> None:
        """Business Rules 2 + domain: context must be loaded; fraud_score in [0.0, 1.0]."""
        self.assert_context_loaded()
        self.assert_not_closed()

        if not (0.0 <= fraud_score <= 1.0):
            raise DomainError(
                f"fraud_score must be in [0.0, 1.0], got {fraud_score}."
            )

        self._stage(
            "FraudScreeningCompleted",
            {
                "application_id": application_id,
                "agent_id": self.agent_id,
                "fraud_score": fraud_score,
                "anomaly_flags": anomaly_flags,
                "screening_model_version": screening_model_version,
                "input_data_hash": input_data_hash,
            },
        )
        await self.save(store)

    async def record_decision(
        self,
        store: EventStore,
        recommendation: str,
        confidence_score: float,
        rationale: str = "",
    ) -> None:
        """
        Business Rules 2 + 4:
        - Context must be loaded (Gas Town).
        - Confidence floor: score < 0.6 forces REFER.
        """
        self.assert_context_loaded()
        self.assert_not_closed()

        forced = confidence_score < CONFIDENCE_FLOOR
        effective_recommendation = "REFER" if forced else recommendation

        self._stage(
            "DecisionGenerated",
            {
                "recommendation": effective_recommendation,
                "confidence_score": confidence_score,
                "rationale": rationale,
                "forced_refer": forced,
            },
        )
        await self.save(store)

    async def close(self, store: EventStore) -> None:
        self.assert_context_loaded()
        self.assert_not_closed()
        self._stage("SessionClosed", {})
        # Do NOT set self.closed here — _apply is the single source of truth.
        await self.save(store)
