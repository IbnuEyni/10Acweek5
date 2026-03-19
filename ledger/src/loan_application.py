from __future__ import annotations

import uuid
from src.aggregate import Aggregate
from src.event_store import EventStore, RecordedEvent


class InvalidStateTransitionError(Exception):
    """Raised when a command would cause an illegal state machine transition."""


# Valid forward transitions only — no backward moves allowed.
# Approved is a terminal state; nothing may follow it except Disbursed.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "New":         {"UnderReview"},
    "UnderReview": {"Approved", "Rejected"},
    "Approved":    {"Disbursed"},
    "Rejected":    set(),
    "Disbursed":   set(),
}


class LoanApplicationAggregate(Aggregate):
    """
    Invariant — State Machine
    -------------------------
    A loan follows a strict forward-only state machine:

        New → UnderReview → Approved → Disbursed
                         ↘ Rejected

    Any attempt to move backwards (e.g. Approved → UnderReview) or to
    re-enter a terminal state raises InvalidStateTransitionError.
    All transition logic lives here; the API only calls commands.
    """

    AGGREGATE_TYPE = "LoanApplication"

    def __init__(self, stream_id: uuid.UUID) -> None:
        super().__init__(stream_id)
        self.status: str = "New"
        self.applicant_id: str | None = None

    # ------------------------------------------------------------------
    # State reconstruction
    # ------------------------------------------------------------------

    def _apply(self, event: RecordedEvent) -> None:
        match event.event_type:
            case "ApplicationSubmitted":
                self.status = "UnderReview"
                self.applicant_id = event.payload.get("applicant_id")
            case "ApplicationApproved":
                self.status = "Approved"
            case "ApplicationRejected":
                self.status = "Rejected"
            case "LoanDisbursed":
                self.status = "Disbursed"

    # ------------------------------------------------------------------
    # Commands — enforce invariants before staging events
    # ------------------------------------------------------------------

    def _transition(self, target: str, event_type: str, payload: dict) -> None:
        """Validate the transition is allowed, then stage the event."""
        allowed = _ALLOWED_TRANSITIONS.get(self.status, set())
        if target not in allowed:
            raise InvalidStateTransitionError(
                f"Cannot transition from '{self.status}' to '{target}' "
                f"for stream {self.stream_id}"
            )
        self._stage(event_type, payload)
        self.status = target

    @classmethod
    async def submit(
        cls,
        stream_id: uuid.UUID,
        store: EventStore,
        applicant_id: str,
        amount: float,
    ) -> "LoanApplicationAggregate":
        """Create a new loan application stream."""
        agg = cls(stream_id)
        agg._transition(
            "UnderReview",
            "ApplicationSubmitted",
            {"applicant_id": applicant_id, "amount": amount},
        )
        await agg.save(store)
        return agg

    async def approve(self, store: EventStore, approved_by: str) -> None:
        self._transition("Approved", "ApplicationApproved", {"approved_by": approved_by})
        await self.save(store)

    async def reject(self, store: EventStore, reason: str) -> None:
        self._transition("Rejected", "ApplicationRejected", {"reason": reason})
        await self.save(store)

    async def disburse(self, store: EventStore, amount: float) -> None:
        self._transition("Disbursed", "LoanDisbursed", {"amount": amount})
        await self.save(store)
