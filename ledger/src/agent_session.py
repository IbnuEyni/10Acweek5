from __future__ import annotations

import uuid
from src.aggregate import Aggregate
from src.event_store import EventStore, RecordedEvent

CONFIDENCE_FLOOR = 0.6


class AgentContextNotLoadedError(Exception):
    """Raised when a decision is attempted before AgentContextLoaded."""


class InvalidStateTransitionError(Exception):
    """Raised when the session is in a state that forbids the command."""


class AgentSessionAggregate(Aggregate):
    """
    Invariant 1 — Gas Town Pattern
    --------------------------------
    AgentContextLoaded MUST be the first event in every session stream.
    Any decision command issued before context is loaded raises
    AgentContextNotLoadedError. This mirrors the real-world requirement
    that an AI agent must have its context window populated before it
    can make decisions — no context, no action.

    Invariant 2 — Confidence Floor
    --------------------------------
    If an AI decision carries confidence_score < 0.6, the aggregate
    overrides the recommendation to 'REFER' regardless of what the
    caller passed. The override is recorded in the event payload so
    the audit trail is transparent. This keeps the safety rule in the
    domain, not scattered across API handlers or agent prompts.
    """

    AGGREGATE_TYPE = "AgentSession"

    def __init__(self, stream_id: uuid.UUID) -> None:
        super().__init__(stream_id)
        self.context_loaded: bool = False
        self.closed: bool = False
        self.agent_id: str | None = None

    # ------------------------------------------------------------------
    # State reconstruction
    # ------------------------------------------------------------------

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
        """
        Open a new agent session. AgentContextLoaded is always the first event —
        this is the Gas Town gate that all subsequent commands check against.
        """
        agg = cls(stream_id)
        agg._stage("AgentContextLoaded", {"agent_id": agent_id, "context": context})
        agg.context_loaded = True
        agg.agent_id = agent_id
        await agg.save(store)
        return agg

    async def record_decision(
        self,
        store: EventStore,
        decision_type: str,
        recommendation: str,
        confidence_score: float,
        rationale: str = "",
    ) -> None:
        """
        Record an AI decision.

        Gas Town check: context must be loaded first.
        Confidence Floor: scores below 0.6 force recommendation to 'REFER'.
        """
        # Invariant 1 — Gas Town
        if not self.context_loaded:
            raise AgentContextNotLoadedError(
                f"AgentContextLoaded must be the first event in session {self.stream_id}. "
                "Load context before recording decisions."
            )

        if self.closed:
            raise InvalidStateTransitionError(
                f"Session {self.stream_id} is closed; no further decisions can be recorded."
            )

        # Invariant 2 — Confidence Floor
        effective_recommendation = recommendation
        forced = False
        if confidence_score < CONFIDENCE_FLOOR:
            effective_recommendation = "REFER"
            forced = True

        self._stage(
            decision_type,
            {
                "recommendation": effective_recommendation,
                "confidence_score": confidence_score,
                "rationale": rationale,
                "forced_refer": forced,
            },
        )
        await self.save(store)

    async def close(self, store: EventStore) -> None:
        """Close the session. Requires context to have been loaded."""
        if not self.context_loaded:
            raise AgentContextNotLoadedError(
                f"Cannot close session {self.stream_id} — context was never loaded."
            )
        if self.closed:
            raise InvalidStateTransitionError(f"Session {self.stream_id} is already closed.")
        self._stage("SessionClosed", {})
        self.closed = True
        await self.save(store)
