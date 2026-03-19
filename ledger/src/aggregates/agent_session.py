from __future__ import annotations

import uuid

from src.aggregates.base import Aggregate, DomainError
from src.event_store import EventStore, RecordedEvent

CONFIDENCE_FLOOR = 0.6


class AgentSessionAggregate(Aggregate):
    """
    Gas Town Invariant
    ------------------
    AgentContextLoaded MUST be the first event. Any command issued on a
    session where context has not been loaded raises DomainError.

    Confidence Floor Invariant
    --------------------------
    A DecisionGenerated event with confidence_score < 0.6 has its
    recommendation automatically overridden to 'REFER'. The override is
    recorded in the event payload for a transparent audit trail.
    """

    AGGREGATE_TYPE = "AgentSession"

    def __init__(self, stream_id: uuid.UUID) -> None:
        super().__init__(stream_id)
        self.context_loaded: bool = False
        self.closed: bool = False
        self.agent_id: str | None = None

    def _apply(self, event: RecordedEvent) -> None:
        match event.event_type:
            case "AgentContextLoaded":
                self.context_loaded = True
                self.agent_id = event.payload.get("agent_id")
            case "SessionClosed":
                self.closed = True

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @classmethod
    async def open(
        cls,
        stream_id: uuid.UUID,
        store: EventStore,
        agent_id: str,
        context: dict,
    ) -> "AgentSessionAggregate":
        """Open a session. AgentContextLoaded is always the first event."""
        agg = cls(stream_id)
        agg._stage("AgentContextLoaded", {"agent_id": agent_id, "context": context})
        agg.context_loaded = True
        agg.agent_id = agent_id
        await agg.save(store)
        return agg

    async def record_decision(
        self,
        store: EventStore,
        recommendation: str,
        confidence_score: float,
        rationale: str = "",
    ) -> None:
        """
        Emit a DecisionGenerated event.

        Gas Town: raises DomainError if context has not been loaded.
        Confidence Floor: overrides recommendation to 'REFER' when score < 0.6.
        """
        if not self.context_loaded:
            raise DomainError(
                "AgentContextLoaded must be the first event before any decisions. "
                f"Session {self.stream_id} has no loaded context."
            )
        if self.closed:
            raise DomainError(f"Session {self.stream_id} is already closed.")

        # Confidence Floor enforcement
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
        if not self.context_loaded:
            raise DomainError(
                f"Cannot close session {self.stream_id} — context was never loaded."
            )
        if self.closed:
            raise DomainError(f"Session {self.stream_id} is already closed.")
        self._stage("SessionClosed", {})
        self.closed = True
        await self.save(store)
