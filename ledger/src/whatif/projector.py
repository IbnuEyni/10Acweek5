"""
What-If Projector — counterfactual scenario engine.

Answers: "What would the outcome have been if event X had different data?"

Design
──────
The core challenge is causal dependency: after the branch point, some real events
are causally downstream of the branched event (e.g. a DecisionGenerated that
references the CreditAnalysisCompleted we replaced) and must be SKIPPED in the
counterfactual timeline. Other events are causally independent (e.g. a
FraudScreeningCompleted that references a different application) and must be
INCLUDED so the counterfactual is as realistic as possible.

Causal dependency is tracked via the metadata.causation_id chain:
  - An event is DEPENDENT if its causation_id is the event_id of any event at
    or after the branch point, OR if its causation_id traces back to such an event
    through the chain.
  - An event is INDEPENDENT if it has no causation_id, or its causation_id traces
    back only to events BEFORE the branch point.

When causation_id is absent (most events in this codebase), we fall back to a
semantic dependency model: events whose event_type is in the CAUSALLY_DEPENDENT_TYPES
set for the branched event_type are treated as dependent. This is the pragmatic
approach for systems that don't thread causation_id through every command.

NEVER writes to the real store.
Projections are applied to an in-memory asyncpg connection (via asyncpg's
connection.transaction() on a real pool connection that is rolled back at the end).
This gives us real SQL semantics without polluting the production read models.

WhatIfResult
────────────
  real_outcome:            dict — projection state under the real event sequence
  counterfactual_outcome:  dict — projection state under the counterfactual sequence
  divergence_events:       list[str] — event_types that differ between timelines
  branch_point:            event_type where the timelines diverge
  skipped_real_events:     list[str] — real event_types skipped as causally dependent
  counterfactual_events_injected: list[str] — event_types injected in their place
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from src.event_store import EventStore, NewEvent, RecordedEvent
from src.projections.base import Projection

# ---------------------------------------------------------------------------
# Semantic causal dependency map.
#
# When causation_id threading is absent, we use event-type semantics:
# if event_type X is branched, the following downstream event_types are
# considered causally dependent and will be skipped in the counterfactual.
#
# This map encodes the Apex domain's causal structure:
#   CreditAnalysisCompleted → DecisionGenerated (the decision references the analysis)
#   DecisionGenerated       → HumanReviewCompleted, ApplicationApproved, ApplicationDeclined
#   ComplianceCheckRequested → ComplianceRulePassed, ComplianceRuleFailed
# ---------------------------------------------------------------------------
_SEMANTIC_DEPENDENTS: dict[str, frozenset[str]] = {
    "CreditAnalysisCompleted": frozenset({
        "DecisionGenerated",
        "HumanReviewCompleted",
        "ApplicationApproved",
        "ApplicationDeclined",
    }),
    "DecisionGenerated": frozenset({
        "HumanReviewCompleted",
        "ApplicationApproved",
        "ApplicationDeclined",
    }),
    "ComplianceCheckRequested": frozenset({
        "ComplianceRulePassed",
        "ComplianceRuleFailed",
        "ComplianceClearanceIssued",
    }),
    "FraudScreeningCompleted": frozenset(),
}


@dataclass
class WhatIfResult:
    """
    The outcome of a counterfactual scenario.

    real_outcome and counterfactual_outcome are dicts keyed by projection name,
    each containing the projection's read-model state after replaying the
    respective event sequence.

    divergence_events: event_types present in one timeline but not the other,
    or present in both but with different payloads. Empty = no material difference.

    skipped_real_events: real events that were causally dependent on the branch
    point and were excluded from the counterfactual timeline.
    """
    branch_point: str                              # event_type where timelines diverge
    real_outcome: dict[str, Any]                   # projection_name → state dict
    counterfactual_outcome: dict[str, Any]         # projection_name → state dict
    divergence_events: list[str] = field(default_factory=list)
    skipped_real_events: list[str] = field(default_factory=list)
    counterfactual_events_injected: list[str] = field(default_factory=list)


def _build_causation_index(events: list[RecordedEvent]) -> dict[str, str]:
    """
    Build a map of event_id (str) → causation_id (str) for all events.
    Used to walk the causation chain when causation_id is threaded.
    """
    return {
        str(e.event_id): e.metadata.get("causation_id", "")
        for e in events
        if e.metadata.get("causation_id")
    }


def _find_dependent_event_ids(
    branch_event_ids: set[str],
    causation_index: dict[str, str],
) -> set[str]:
    """
    Walk the causation chain forward from branch_event_ids.
    Returns the full set of event_ids that are causally downstream.

    This is a BFS over the causation graph: if event B has causation_id = A,
    and A is in the dependent set, then B is also dependent.
    """
    dependent: set[str] = set(branch_event_ids)
    changed = True
    while changed:
        changed = False
        for eid, cid in causation_index.items():
            if cid in dependent and eid not in dependent:
                dependent.add(eid)
                changed = True
    return dependent


def _make_synthetic_recorded(
    new_event: NewEvent,
    stream_id: str,
    stream_position: int,
    global_position: int,
) -> RecordedEvent:
    """Wrap a NewEvent as a RecordedEvent for in-memory projection replay."""
    from datetime import datetime, timezone
    return RecordedEvent(
        event_id=new_event.event_id,
        stream_id=stream_id,
        stream_position=stream_position,
        global_position=global_position,
        event_type=new_event.event_type,
        event_version=new_event.event_version,
        payload=new_event.payload,
        metadata=new_event.metadata,
        recorded_at=datetime.now(timezone.utc),
    )


async def _apply_to_projections_in_memory(
    events: list[RecordedEvent],
    projections: list[Projection],
    pool: asyncpg.Pool,
) -> dict[str, Any]:
    """
    Replay events through projections inside a transaction that is always
    rolled back — the real read models are never touched.

    Pattern: acquire connection → start transaction → apply events → read state
    → rollback. The rollback is unconditional: we raise _Rollback(outcome) to
    carry the extracted state out of the transaction block, which causes asyncpg
    to roll back the transaction cleanly.

    We do NOT use a savepoint here because asyncpg savepoints require an outer
    transaction, and rolling back the outer transaction after extracting state
    is the simplest correct approach.
    """
    app_id = _extract_application_id(events)
    outcome: dict[str, Any] = {}

    async with pool.acquire() as conn:
        async with conn.transaction():
            for event in events:
                for projection in projections:
                    if projection.event_types and event.event_type not in projection.event_types:
                        continue
                    try:
                        await projection.handle(event, conn)
                    except Exception:
                        # Non-fatal: counterfactual may produce constraint-violating states
                        pass

            # Extract state while writes are still visible (inside transaction)
            for projection in projections:
                state = await _read_projection_state(projection, app_id, conn)
                outcome[projection.name] = state

            # Unconditional rollback: carry state out via sentinel exception.
            # asyncpg rolls back the transaction cleanly when an exception
            # propagates out of `async with conn.transaction()`.
            raise _Rollback(outcome)

    return outcome  # unreachable — _Rollback always raised above


class _Rollback(Exception):
    """Carries extracted projection state out of the rolled-back transaction."""
    def __init__(self, outcome: dict[str, Any]) -> None:
        self.outcome = outcome


async def run_what_if(
    store: EventStore,
    application_id: str,
    branch_at_event_type: str,
    counterfactual_events: list[NewEvent],
    projections: list[Projection],
) -> WhatIfResult:
    """
    Run a counterfactual scenario for a loan application.

    1. Load all events for the application's primary stream.
    2. Split at the first occurrence of branch_at_event_type:
       - pre_branch: events before the branch point (shared by both timelines)
       - branch_event: the real event being replaced
       - post_branch: events after the branch point
    3. Determine which post-branch events are causally dependent on the branch
       event (via causation_id chain or semantic dependency map).
    4. Build two event sequences:
       - real_sequence:            pre_branch + branch_event + post_branch
       - counterfactual_sequence:  pre_branch + counterfactual_events +
                                   post_branch_independent_only
    5. Replay each sequence through the projections in a rolled-back transaction.
    6. Return WhatIfResult with both outcomes and the divergence analysis.

    NEVER writes counterfactual events to the real store.
    """
    stream_id = f"loan-{application_id}"
    all_events = await store.load_stream(stream_id)

    # ── Step 1: Find the branch point ────────────────────────────────────────
    branch_index: int | None = None
    for i, ev in enumerate(all_events):
        if ev.event_type == branch_at_event_type:
            branch_index = i
            break

    if branch_index is None:
        # Branch point not found — counterfactual is the full real sequence
        # plus the injected events appended at the end.
        pre_branch = all_events
        branch_event = None
        post_branch: list[RecordedEvent] = []
    else:
        pre_branch = all_events[:branch_index]
        branch_event = all_events[branch_index]
        post_branch = all_events[branch_index + 1:]

    # ── Step 2: Identify causally dependent post-branch events ───────────────
    causation_index = _build_causation_index(all_events)

    branch_event_ids: set[str] = set()
    if branch_event is not None:
        branch_event_ids.add(str(branch_event.event_id))
    # Also include the counterfactual event IDs as branch roots
    for ce in counterfactual_events:
        branch_event_ids.add(str(ce.event_id))

    dependent_ids = _find_dependent_event_ids(branch_event_ids, causation_index)

    # Semantic fallback: if causation_id is not threaded, use the domain map
    semantic_dependents = _SEMANTIC_DEPENDENTS.get(branch_at_event_type, frozenset())

    post_branch_independent: list[RecordedEvent] = []
    post_branch_dependent: list[RecordedEvent] = []
    for ev in post_branch:
        is_causally_dependent = str(ev.event_id) in dependent_ids
        is_semantically_dependent = ev.event_type in semantic_dependents
        if is_causally_dependent or is_semantically_dependent:
            post_branch_dependent.append(ev)
        else:
            post_branch_independent.append(ev)

    # ── Step 3: Build the two event sequences ────────────────────────────────
    real_sequence: list[RecordedEvent] = list(all_events)  # unchanged

    # Wrap counterfactual NewEvents as RecordedEvents for projection replay
    cf_start_position = (pre_branch[-1].stream_position if pre_branch else 0) + 1
    cf_start_global = (pre_branch[-1].global_position if pre_branch else 0) + 1
    synthetic: list[RecordedEvent] = [
        _make_synthetic_recorded(ce, stream_id, cf_start_position + i, cf_start_global + i)
        for i, ce in enumerate(counterfactual_events)
    ]

    counterfactual_sequence: list[RecordedEvent] = (
        pre_branch + synthetic + post_branch_independent
    )

    # ── Step 4: Replay both sequences through projections ────────────────────
    pool = store._pool

    real_outcome: dict[str, Any] = {}
    try:
        await _apply_to_projections_in_memory(real_sequence, projections, pool)
    except _Rollback as rb:
        real_outcome = rb.outcome

    counterfactual_outcome: dict[str, Any] = {}
    try:
        await _apply_to_projections_in_memory(counterfactual_sequence, projections, pool)
    except _Rollback as rb:
        counterfactual_outcome = rb.outcome

    # ── Step 5: Compute divergence ───────────────────────────────────────────
    divergence_events = _compute_divergence(real_sequence, counterfactual_sequence)

    return WhatIfResult(
        branch_point=branch_at_event_type,
        real_outcome=real_outcome,
        counterfactual_outcome=counterfactual_outcome,
        divergence_events=divergence_events,
        skipped_real_events=[e.event_type for e in post_branch_dependent],
        counterfactual_events_injected=[ce.event_type for ce in counterfactual_events],
    )


def _extract_application_id(events: list[RecordedEvent]) -> str | None:
    """Extract application_id from the first event that carries it."""
    for ev in events:
        app_id = ev.payload.get("application_id")
        if app_id:
            return app_id
    return None


async def _read_projection_state(
    projection: Projection,
    application_id: str | None,
    conn: asyncpg.Connection,
) -> Any:
    """
    Extract the current state from a projection after replay.
    Uses the projection's public query interface if available.
    """
    if application_id is None:
        return None

    # ApplicationSummaryProjection
    if hasattr(projection, "get_current"):
        try:
            return await projection.get_current(application_id, conn)
        except Exception:
            return None

    # ComplianceAuditViewProjection
    if hasattr(projection, "get_current_compliance"):
        try:
            state = await projection.get_current_compliance(application_id, conn)
            from dataclasses import asdict
            return asdict(state)
        except Exception:
            return None

    return None


def _compute_divergence(
    real: list[RecordedEvent],
    counterfactual: list[RecordedEvent],
) -> list[str]:
    """
    Return event_types that appear in one timeline but not the other,
    or appear in both but with different payloads at the same position.
    """
    real_types = [e.event_type for e in real]
    cf_types = [e.event_type for e in counterfactual]

    real_set = set(real_types)
    cf_set = set(cf_types)

    diverged = list((real_set - cf_set) | (cf_set - real_set))

    # Also flag events present in both but with different payloads
    real_by_type: dict[str, list[dict]] = {}
    for e in real:
        real_by_type.setdefault(e.event_type, []).append(e.payload)
    cf_by_type: dict[str, list[dict]] = {}
    for e in counterfactual:
        cf_by_type.setdefault(e.event_type, []).append(e.payload)

    for etype in real_set & cf_set:
        if real_by_type.get(etype) != cf_by_type.get(etype):
            if etype not in diverged:
                diverged.append(etype)

    return sorted(diverged)
