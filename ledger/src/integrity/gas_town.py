"""
Gas Town Agent Memory Pattern — reconstruct_agent_context().

Prevents catastrophic memory loss: an agent that crashes mid-session can
restart and reconstruct its exact context from the event store, then continue
where it left off without repeating completed work.

NEEDS_RECONCILIATION is flagged when the agent's last event was a partial
decision (no corresponding completion event) — the agent must resolve the
partial state before proceeding.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.event_store import EventStore, RecordedEvent

# Event types that indicate a decision was started but not completed
_PARTIAL_DECISION_TYPES = {"CreditAnalysisCompleted"}
_COMPLETION_TYPES = {"ApplicationApproved", "ApplicationDeclined", "FraudScreeningCompleted"}

# Event types always preserved verbatim (never summarised)
_ALWAYS_VERBATIM = {"AgentContextLoaded", "SessionClosed"}


@dataclass
class AgentContext:
    agent_id: str
    session_id: str
    context_text: str                          # token-efficient prose summary
    last_event_position: int                   # stream_position of last event
    pending_work: list[str] = field(default_factory=list)
    session_health_status: str = "OK"          # "OK" | "NEEDS_RECONCILIATION" | "CLOSED"
    last_3_events: list[dict[str, Any]] = field(default_factory=list)
    model_version: str | None = None


async def reconstruct_agent_context(
    store: EventStore,
    agent_id: str,
    session_id: str,
    token_budget: int = 8000,
) -> AgentContext:
    """
    1. Load full AgentSession stream for agent_id + session_id.
    2. Identify: last completed action, pending work items, current application state.
    3. Summarise old events into prose (token-efficient).
    4. Preserve verbatim: last 3 events, any PENDING or ERROR state events.
    5. Return AgentContext with context_text, last_event_position,
       pending_work[], session_health_status.

    CRITICAL: if the agent's last event was a partial decision (no corresponding
    completion event), flag the context as NEEDS_RECONCILIATION.
    """
    stream_id = f"agent-{agent_id}-{session_id}"
    events = await store.load_stream(stream_id)

    if not events:
        return AgentContext(
            agent_id=agent_id,
            session_id=session_id,
            context_text="No events found for this session.",
            last_event_position=0,
            session_health_status="OK",
        )

    last_event = events[-1]
    last_3 = [
        {"event_type": e.event_type, "payload": e.payload, "position": e.stream_position}
        for e in events[-3:]
    ]

    # Determine model_version from AgentContextLoaded
    model_version = None
    for ev in events:
        if ev.event_type == "AgentContextLoaded":
            model_version = ev.payload.get("model_version")
            break

    # Detect NEEDS_RECONCILIATION: last substantive event is a partial decision
    health = "OK"
    if last_event.event_type == "SessionClosed":
        health = "CLOSED"
    else:
        # Check if the last decision-type event has a corresponding completion
        decision_apps: set[str] = set()
        completed_apps: set[str] = set()
        for ev in events:
            if ev.event_type in _PARTIAL_DECISION_TYPES:
                decision_apps.add(ev.payload.get("application_id", ""))
            if ev.event_type in _COMPLETION_TYPES:
                completed_apps.add(ev.payload.get("application_id", ""))
        pending = decision_apps - completed_apps
        if pending:
            health = "NEEDS_RECONCILIATION"

    # Build pending work list
    pending_work: list[str] = []
    if health == "NEEDS_RECONCILIATION":
        for ev in events:
            if ev.event_type in _PARTIAL_DECISION_TYPES:
                app_id = ev.payload.get("application_id", "")
                if app_id not in completed_apps:
                    pending_work.append(f"Reconcile partial decision for application {app_id}")

    # Build token-efficient prose summary
    # Summarise all but the last 3 events; preserve last 3 verbatim
    summary_events = events[:-3] if len(events) > 3 else []
    verbatim_events = events[-3:]

    summary_lines: list[str] = []
    for ev in summary_events:
        summary_lines.append(
            f"[pos={ev.stream_position}] {ev.event_type}: "
            + ", ".join(f"{k}={v}" for k, v in list(ev.payload.items())[:3])
        )

    context_parts = []
    if summary_lines:
        context_parts.append("Session history (summarised):\n" + "\n".join(summary_lines))
    context_parts.append(
        "Recent events (verbatim):\n"
        + "\n".join(
            f"[pos={e.stream_position}] {e.event_type}: {e.payload}"
            for e in verbatim_events
        )
    )
    if pending_work:
        context_parts.append("PENDING WORK:\n" + "\n".join(pending_work))
    if health == "NEEDS_RECONCILIATION":
        context_parts.append(
            "WARNING: Session requires reconciliation before proceeding. "
            "A partial decision was started but not completed."
        )

    context_text = "\n\n".join(context_parts)

    # Truncate to token_budget (rough approximation: 1 token ≈ 4 chars)
    max_chars = token_budget * 4
    if len(context_text) > max_chars:
        context_text = context_text[:max_chars] + "\n[...truncated to token budget]"

    return AgentContext(
        agent_id=agent_id,
        session_id=session_id,
        context_text=context_text,
        last_event_position=last_event.stream_position,
        pending_work=pending_work,
        session_health_status=health,
        last_3_events=last_3,
        model_version=model_version,
    )
