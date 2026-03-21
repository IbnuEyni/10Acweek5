from __future__ import annotations

from src.aggregates.base import Aggregate, DomainError
from src.event_store import EventStore, RecordedEvent


class AuditLedgerAggregate(Aggregate):
    """
    Cross-cutting audit trail linking events across all aggregates for a
    single business entity.

    Key invariants:
    - Append-only: no events may be removed.
    - Must maintain cross-stream causal ordering via correlation_id chains.
    - AuditIntegrityCheckRun events form a hash chain for tamper detection.

    Kept separate from all other aggregates because:
    - It spans multiple aggregate streams (loan + agent + compliance).
    - Compliance officers query it independently of business state.
    - Hash chain integrity requires a dedicated, uncontested stream.

    stream_id format: "audit-{entity_type}-{entity_id}"
    """

    AGGREGATE_TYPE = "AuditLedger"

    def __init__(self, stream_id: str) -> None:
        super().__init__(stream_id)
        # Parse entity_type and entity_id from stream_id
        parts = stream_id.removeprefix("audit-").split("-", 1)
        self.entity_type: str = parts[0] if parts else ""
        self.entity_id: str = parts[1] if len(parts) > 1 else ""

        self.last_integrity_hash: str | None = None
        self.events_verified_count: int = 0
        self.linked_stream_ids: set[str] = set()
        self.last_correlation_id: str | None = None

    # ------------------------------------------------------------------
    # Event application (replay) — one method per event type
    # ------------------------------------------------------------------

    def _apply(self, event: RecordedEvent) -> None:
        """Dispatch to the dedicated per-event handler."""
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler is not None:
            handler(event)

    def _on_AuditEntryRecorded(self, event: RecordedEvent) -> None:
        stream_id = event.payload.get("source_stream_id", "")
        if stream_id:
            self.linked_stream_ids.add(stream_id)
        self.last_correlation_id = event.metadata.get("correlation_id")

    def _on_AuditIntegrityCheckRun(self, event: RecordedEvent) -> None:
        self.last_integrity_hash = event.payload.get("integrity_hash")
        self.events_verified_count = event.payload.get("events_verified_count", 0)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @classmethod
    async def load(
        cls, store: EventStore, entity_type: str, entity_id: str
    ) -> "AuditLedgerAggregate":
        stream_id = f"audit-{entity_type}-{entity_id}"
        return await super().load(stream_id, store)  # type: ignore[return-value]

    async def record_entry(
        self,
        store: EventStore,
        source_stream_id: str,
        event_type: str,
        summary: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> None:
        self._stage(
            "AuditEntryRecorded",
            {
                "entity_type": self.entity_type,
                "entity_id": self.entity_id,
                "source_stream_id": source_stream_id,
                "event_type": event_type,
                "summary": summary,
            },
        )
        await self.save(store, correlation_id=correlation_id, causation_id=causation_id)

    async def record_integrity_check(
        self,
        store: EventStore,
        events_verified_count: int,
        integrity_hash: str,
        previous_hash: str | None,
        chain_valid: bool,
    ) -> None:
        self._stage(
            "AuditIntegrityCheckRun",
            {
                "entity_id": self.entity_id,
                "events_verified_count": events_verified_count,
                "integrity_hash": integrity_hash,
                "previous_hash": previous_hash,
                "chain_valid": chain_valid,
            },
        )
        await self.save(store)
