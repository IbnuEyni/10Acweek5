from __future__ import annotations

from src.aggregates.base import Aggregate, DomainError
from src.event_store import EventStore, RecordedEvent


class ComplianceRecordAggregate(Aggregate):
    """
    Tracks regulatory checks, rule evaluations, and compliance verdicts.

    Key invariants:
    - Cannot issue a compliance clearance without all mandatory checks passed.
    - Every check must reference the specific regulation version evaluated against.
    - Append-only: no events may be removed or modified.

    Kept separate from LoanApplication to avoid coupling:
    - Compliance checks can be added/modified independently of loan state.
    - Concurrent writes to compliance vs. loan streams don't block each other.
    - Regulatory teams can query compliance without loading loan state.

    stream_id format: "compliance-{application_id}"
    """

    AGGREGATE_TYPE = "ComplianceRecord"

    def __init__(self, stream_id: str) -> None:
        super().__init__(stream_id)
        self.application_id: str = stream_id.removeprefix("compliance-")
        self.regulation_set_version: str | None = None
        self.required_checks: set[str] = set()
        self.passed_checks: dict[str, str] = {}   # rule_id → rule_version
        self.failed_checks: dict[str, str] = {}   # rule_id → failure_reason
        self.clearance_issued: bool = False

    # ------------------------------------------------------------------
    # Event application (replay) — one method per event type
    # ------------------------------------------------------------------

    def _apply(self, event: RecordedEvent) -> None:
        """Dispatch to the dedicated per-event handler."""
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler is not None:
            handler(event)

    def _on_ComplianceCheckRequested(self, event: RecordedEvent) -> None:
        self.regulation_set_version = event.payload.get("regulation_set_version")
        for check in event.payload.get("checks_required", []):
            self.required_checks.add(check)

    def _on_ComplianceRulePassed(self, event: RecordedEvent) -> None:
        rule_id = event.payload.get("rule_id", "")
        rule_version = event.payload.get("rule_version", "")
        self.passed_checks[rule_id] = rule_version
        self.failed_checks.pop(rule_id, None)  # override a prior failure

    def _on_ComplianceRuleFailed(self, event: RecordedEvent) -> None:
        rule_id = event.payload.get("rule_id", "")
        self.failed_checks[rule_id] = event.payload.get("failure_reason", "")
        self.passed_checks.pop(rule_id, None)

    def _on_ComplianceClearanceIssued(self, event: RecordedEvent) -> None:
        self.clearance_issued = True

    # ------------------------------------------------------------------
    # Assertion helpers
    # ------------------------------------------------------------------

    def assert_all_checks_passed(self) -> None:
        """Cannot issue clearance without all mandatory checks passed."""
        missing = self.required_checks - set(self.passed_checks)
        failed = self.required_checks & set(self.failed_checks)
        if missing or failed:
            raise DomainError(
                f"ComplianceRecord '{self.stream_id}' cannot issue clearance. "
                f"Missing: {sorted(missing)}. Failed: {sorted(failed)}."
            )

    def assert_not_cleared(self) -> None:
        if self.clearance_issued:
            raise DomainError(
                f"ComplianceRecord '{self.stream_id}' has already issued clearance."
            )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @classmethod
    async def load(
        cls, store: EventStore, application_id: str
    ) -> "ComplianceRecordAggregate":
        stream_id = f"compliance-{application_id}"
        return await super().load(stream_id, store)  # type: ignore[return-value]

    @classmethod
    async def request_checks(
        cls,
        stream_id: str,
        store: EventStore,
        regulation_set_version: str,
        checks_required: list[str],
    ) -> "ComplianceRecordAggregate":
        agg = cls(stream_id)
        agg._stage(
            "ComplianceCheckRequested",
            {
                "application_id": agg.application_id,
                "regulation_set_version": regulation_set_version,
                "checks_required": checks_required,
            },
        )
        await agg.save(store)
        return agg

    async def record_rule_passed(
        self,
        store: EventStore,
        rule_id: str,
        rule_version: str,
        evidence_hash: str,
    ) -> None:
        """Every check must reference the specific regulation version evaluated against."""
        if not self.regulation_set_version:
            raise DomainError(
                f"ComplianceRecord '{self.stream_id}' has no active check request."
            )
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

    async def record_rule_failed(
        self,
        store: EventStore,
        rule_id: str,
        rule_version: str,
        failure_reason: str,
        remediation_required: bool = False,
    ) -> None:
        if not self.regulation_set_version:
            raise DomainError(
                f"ComplianceRecord '{self.stream_id}' has no active check request."
            )
        self._stage(
            "ComplianceRuleFailed",
            {
                "application_id": self.application_id,
                "rule_id": rule_id,
                "rule_version": rule_version,
                "failure_reason": failure_reason,
                "remediation_required": remediation_required,
            },
        )
        await self.save(store)

    async def issue_clearance(self, store: EventStore) -> None:
        """Cannot issue clearance without all mandatory checks passed."""
        self.assert_all_checks_passed()
        self.assert_not_cleared()
        self._stage(
            "ComplianceClearanceIssued",
            {"application_id": self.application_id},
        )
        await self.save(store)
