from __future__ import annotations

import uuid

from src.aggregates.base import Aggregate, DomainError
from src.event_store import EventStore, RecordedEvent

# Forward-only transitions. Any target not in the allowed set raises DomainError.
_TRANSITIONS: dict[str, set[str]] = {
    "New":         {"UnderReview"},
    "UnderReview": {"Approved", "Rejected"},
    "Approved":    {"Disbursed"},   # Approved → UnderReview is intentionally absent
    "Rejected":    set(),
    "Disbursed":   set(),
}


class LoanApplicationAggregate(Aggregate):
    """
    State Machine Invariant
    -----------------------
    Loans follow a strict forward-only lifecycle:

        New → UnderReview → Approved → Disbursed
                         ↘ Rejected  (terminal)

    Any backward or illegal transition raises DomainError.
    """

    AGGREGATE_TYPE = "LoanApplication"

    def __init__(self, stream_id: uuid.UUID) -> None:
        super().__init__(stream_id)
        self.status: str = "New"
        self.applicant_id: str | None = None

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

    def _transition(self, target: str, event_type: str, payload: dict) -> None:
        if target not in _TRANSITIONS.get(self.status, set()):
            raise DomainError(
                f"Cannot transition LoanApplication from '{self.status}' to '{target}'."
            )
        self._stage(event_type, payload)
        self.status = target

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @classmethod
    async def submit(
        cls,
        stream_id: uuid.UUID,
        store: EventStore,
        applicant_id: str,
        amount: float,
    ) -> "LoanApplicationAggregate":
        agg = cls(stream_id)
        agg._transition("UnderReview", "ApplicationSubmitted",
                        {"applicant_id": applicant_id, "amount": amount})
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
